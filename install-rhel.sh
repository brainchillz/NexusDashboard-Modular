#!/bin/bash
set -e

# Nexus Dashboard installer for RHEL / Rocky / AlmaLinux
# 9 & 10. Counterpart of install.sh (Debian/Ubuntu). It installs all module
# software (via install-prerequisites-rhel.sh), the dashboard itself, the
# root-owned privilege-boundary helpers, the systemd unit + timers, sudoers
# (RHEL binary/config paths), and firewalld rules.
#
# The Network module is netplan-based and Ubuntu-only; on RHEL only the
# hostname/domain part works and the netplan helper is intentionally not
# installed. ZFS comes from the OpenZFS repo (best-effort; see the prereq
# script) and may be unavailable on a brand-new EL release.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="/opt/nexus-dashboard"
DASHBOARD_USER="dashboard"
DASHBOARD_PORT="${DASHBOARD_PORT:-8443}"

echo "=== Nexus Dashboard Installer (RHEL / Rocky / AlmaLinux 9 & 10) ==="
echo ""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    error "Please run as root or with sudo"
    exit 1
fi

if ! command -v dnf >/dev/null 2>&1; then
    error "dnf not found. This installer targets RHEL/Rocky/AlmaLinux."
    error "For Debian/Ubuntu use install.sh instead."
    exit 1
fi

. /etc/os-release

# Service unit names differ from Debian: NFS is nfs-server, Samba is smb.
NFS_SERVICE="nfs-server"
SMB_SERVICE="smb"
ISCSI_SERVICE="target"

info "Installing prerequisite packages..."
if [ -f "$SCRIPT_DIR/install-prerequisites-rhel.sh" ]; then
    SD_SKIP_NEXT_STEP=1 bash "$SCRIPT_DIR/install-prerequisites-rhel.sh"
else
    error "install-prerequisites-rhel.sh not found next to install-rhel.sh."
    exit 1
fi

info "Creating dashboard user..."
if ! id -u $DASHBOARD_USER &>/dev/null; then
    useradd -r -s /usr/sbin/nologin -M -d $DASHBOARD_DIR $DASHBOARD_USER
fi

# Containers + Docker modules: the app talks to the local LXD/Incus/Docker
# socket, which only needs group membership (no sudo). Join whichever groups
# exist; harmless if a daemon is not installed (its pages report it
# unreachable).
for _g in lxd incus-admin docker; do
    if getent group "$_g" >/dev/null 2>&1; then
        usermod -aG "$_g" $DASHBOARD_USER
        info "Added $DASHBOARD_USER to the $_g group (container management)"
    fi
done

# Warn early if the chosen port is already taken (Cockpit commonly holds 9090,
# the LXD API often holds 9443) — the service would flap on start otherwise.
if ss -tln 2>/dev/null | grep -q ":${DASHBOARD_PORT:-8443} "; then
    echo "WARNING: port ${DASHBOARD_PORT:-8443} is already in use — set DASHBOARD_PORT to a free port in the unit." >&2
fi

info "Deploying application files to $DASHBOARD_DIR..."
mkdir -p "$DASHBOARD_DIR"
if [ "$SCRIPT_DIR" != "$DASHBOARD_DIR" ]; then
    cp -r "$SCRIPT_DIR/app.py" "$SCRIPT_DIR/nexusdash" "$SCRIPT_DIR/templates" "$SCRIPT_DIR/static" "$DASHBOARD_DIR/"
    [ -f "$SCRIPT_DIR/requirements.txt" ] && cp "$SCRIPT_DIR/requirements.txt" "$DASHBOARD_DIR/"
else
    info "  (running from $DASHBOARD_DIR — files already in place)"
fi

info "Setting up Python virtual environment..."
python3 -m venv $DASHBOARD_DIR/venv
source $DASHBOARD_DIR/venv/bin/activate
if [ -f "$DASHBOARD_DIR/requirements.txt" ]; then
    pip install -q -r "$DASHBOARD_DIR/requirements.txt"
else
    pip install -q flask
fi
deactivate

info "Setting up sudoers permissions..."
SUDOERS_FILE="/etc/sudoers.d/nexus-dashboard"
cat > $SUDOERS_FILE << 'SUDOERS'
# Nexus Dashboard - passwordless sudo for the exact commands app.py runs.
# RHEL/Rocky paths (merged /usr). sudo matches the fully-resolved binary path.

# Service control & logs
dashboard ALL=(ALL) NOPASSWD: /usr/bin/systemctl
dashboard ALL=(ALL) NOPASSWD: /usr/bin/journalctl

# Disk / system inventory
dashboard ALL=(ALL) NOPASSWD: /usr/bin/lsblk
dashboard ALL=(ALL) NOPASSWD: /usr/bin/lsscsi
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/ip
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/smartctl
# Host firewall (Firewall module) — ufw is EPEL-only on RHEL-family; the rule
# is harmless when the binary is absent and the module reports "not installed".
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/ufw
# Disk wipe (blank a free/stale disk). Eligibility is enforced in app.py.
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/mdadm
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/wipefs
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/sgdisk
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/partprobe
# Disk locate: enclosure LED + a root-owned read-only wrapper.
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/ledctl
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-locate-read
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-iscsi-sessions
# Snapshot browser / single-file restore (root-owned, self-confining helper).
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-snap-fs
# Hostname (the netplan helper is Ubuntu-only and intentionally absent here).
dashboard ALL=(ALL) NOPASSWD: /usr/bin/hostnamectl
# Caddy module: root-owned helper — `caddy validate` before write, then reload
# (restores the previous file if the reload is refused). Trust boundary.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-caddy
# Plain-disk mount: root-owned helper that mounts under /mnt|/media and edits
# its own block in /etc/fstab (always nofail). mount/umount/tee /etc/fstab are
# deliberately NOT granted directly.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-mount
# llama.cpp model download: root-owned helper that pulls a GGUF from Hugging Face
# into the models dir (re-validates repo/filename, confines output). Trust boundary.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-model-fetch
# MiniDLNA DB rebuild: root-owned helper that stops the service, deletes files.db
# (confined to the hard-coded cache dir), and starts. rm/minidlnad as root are
# escalation-sensitive, so they go through this wrapper. Trust boundary.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-dlna-rescan
# MiniDLNA library stats: root-owned read helper that opens files.db read-only and
# prints fixed COUNT queries as JSON. Read-only, no arbitrary SQL. Trust boundary.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-dlna-stats

# LVM (read + manage; destructive ops are guarded in app.py)
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/pvs, /usr/sbin/vgs, /usr/sbin/lvs
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/pvcreate, /usr/sbin/pvremove, /usr/sbin/pvresize, /usr/sbin/pvmove
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/vgcreate, /usr/sbin/vgremove, /usr/sbin/vgextend, /usr/sbin/vgreduce
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/lvcreate, /usr/sbin/lvremove, /usr/sbin/lvextend, /usr/sbin/lvresize
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/mkfs.ext4, /usr/sbin/mkfs.xfs
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/mkfs.vfat, /usr/sbin/mkfs.exfat

# ZFS (from the OpenZFS repo)
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/zpool
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/zfs

# iSCSI (LIO / targetcli)
dashboard ALL=(ALL) NOPASSWD: /usr/bin/targetcli

# NFS
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/exportfs

# SMB / Samba
dashboard ALL=(ALL) NOPASSWD: /usr/bin/testparm
dashboard ALL=(ALL) NOPASSWD: /usr/bin/smbpasswd
dashboard ALL=(ALL) NOPASSWD: /usr/bin/smbstatus
dashboard ALL=(ALL) NOPASSWD: /usr/bin/pdbedit
# Samba registry shares (Cockpit file-sharing stores shares there). Only the
# exact `net conf` verbs the app uses — deliberately NOT `net conf import`,
# whose file argument would be read as root, and no other `net` subcommands.
dashboard ALL=(ALL) NOPASSWD: /usr/bin/net conf list, /usr/bin/net conf addshare *, /usr/bin/net conf delshare *, /usr/bin/net conf setparm *, /usr/bin/net conf delparm *
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/useradd
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/groupadd
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/groupdel
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/usermod
dashboard ALL=(ALL) NOPASSWD: /usr/bin/gpasswd

# Config writers — restricted to the exact files/forms app.py invokes. Note the
# RHEL mdadm.conf path (/etc/mdadm.conf, not /etc/mdadm/mdadm.conf) and dracut
# (not update-initramfs).
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/exports
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/samba/smb.conf
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/hosts
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/mdadm.conf
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/llama.conf
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/minidlna.conf
dashboard ALL=(ALL) NOPASSWD: /usr/bin/dracut -f
# Load RAID personalities for array creation (exact modules only, no wildcard).
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/modprobe raid0, /usr/sbin/modprobe raid1, /usr/sbin/modprobe raid456, /usr/sbin/modprobe raid10
dashboard ALL=(ALL) NOPASSWD: /usr/bin/mkdir -p -- *
dashboard ALL=(ALL) NOPASSWD: /usr/bin/rmdir *
dashboard ALL=(ALL) NOPASSWD: /usr/bin/chmod 2775 -- *
# dnsmasq (DNS & DHCP) module: DHCP-conflict probe binds privileged UDP
# port 68. Best-effort. systemctl/journalctl for dnsmasq use the blanket
# lines above; the module renders its own config into a dashboard-owned
# conf-dir, so no root helper is needed.
dashboard ALL=(ALL) NOPASSWD: /opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py dhcp-probe*
SUDOERS
chmod 440 $SUDOERS_FILE
visudo -cf "$SUDOERS_FILE" >/dev/null && info "Sudoers validated at $SUDOERS_FILE" \
    || { error "Sudoers file failed validation; removing it."; rm -f "$SUDOERS_FILE"; exit 1; }

# ── Root-owned privilege-boundary helpers (distro-agnostic) ───────────
info "Installing disk-locate read helper..."
LOCATE_HELPER="/usr/local/sbin/nexus-dashboard-locate-read"
cat > "$LOCATE_HELPER" << 'HELPER'
#!/bin/sh
# Generate read-only activity on a disk so its activity LED flashes. Reads 32MB
# from a pseudo-random offset (cache-miss -> real device I/O; HDDs also seek).
# Strictly read-only (output is /dev/null), so it is safe on any disk.
dev="$1"
case "$dev" in ''|*[!a-zA-Z0-9]*) echo "invalid device" >&2; exit 2 ;; esac
[ -b "/dev/$dev" ] || { echo "not a block device" >&2; exit 3; }
bytes=$(blockdev --getsize64 "/dev/$dev" 2>/dev/null) || exit 4
count=32
max=$(( bytes / 1048576 - count ))
skip=0
if [ "$max" -gt 0 ]; then
    rnd=$(od -An -N4 -tu4 /dev/urandom | tr -d ' ')
    skip=$(( rnd % max ))
fi
exec dd if="/dev/$dev" of=/dev/null bs=1M count="$count" skip="$skip" 2>/dev/null
HELPER
chown root:root "$LOCATE_HELPER"; chmod 755 "$LOCATE_HELPER"

info "Installing iSCSI sessions helper..."
SESSIONS_HELPER="/usr/local/sbin/nexus-dashboard-iscsi-sessions"
cat > "$SESSIONS_HELPER" << 'HELPER'
#!/bin/sh
base=/sys/kernel/config/target/iscsi
[ -d "$base" ] || exit 0
for t in "$base"/iqn.*; do
    [ -d "$t" ] || continue
    tiqn=$(basename "$t")
    for tpg in "$t"/tpgt_*; do
        [ -d "$tpg" ] || continue
        if [ -f "$tpg/dynamic_sessions" ]; then
            while IFS= read -r init; do
                [ -n "$init" ] && printf '%s\t%s\tdynamic\n' "$tiqn" "$init"
            done < "$tpg/dynamic_sessions"
        fi
        for acl in "$tpg"/acls/iqn.*; do
            [ -d "$acl" ] || continue
            if grep -q 'LOGGED_IN' "$acl/info" 2>/dev/null; then
                printf '%s\t%s\tacl\n' "$tiqn" "$(basename "$acl")"
            fi
        done
    done
done
HELPER
chown root:root "$SESSIONS_HELPER"; chmod 755 "$SESSIONS_HELPER"

info "Installing caddy (reverse proxy) helper..."
# Root-owned: validates a candidate Caddyfile with `caddy validate` BEFORE the
# live file is touched, backs the previous one up, replaces it atomically, and
# reloads caddy only when it is running (restoring the previous file if the
# reload is refused, so file and running config never diverge). Also replaces
# cert/key pairs (validated + confined to /etc/caddy). NOT writable by dashboard.
CADDY_HELPER="/usr/local/sbin/nexus-dashboard-caddy"
cat > "$CADDY_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard caddy module.
#   apply  (Caddyfile on stdin): validate the candidate (written to a temp file
#          IN /etc/caddy so relative `import`s resolve; nothing live is touched
#          on failure), back up + atomically replace the managed file, then
#          `systemctl reload caddy` when the service is running (restore the
#          previous file + non-zero exit if the reload is refused).
#   cert <cert-path> <key-path>  (JSON {"cert","key"} PEMs on stdin): replace
#          an EXISTING cert/key pair referenced by the Caddyfile. Both paths
#          are confined to /etc/caddy; the pair must parse and match (openssl
#          public-key comparison) before anything is written; owner/mode are
#          preserved from the files being replaced; reload as above, restoring
#          both files if it is refused.
import json
import os
import subprocess
import sys
import tempfile

MANAGED = '/etc/caddy/Caddyfile'
BACKUP = '/run/nexus-dashboard-caddy.prev'
CERT_BACKUP = '/run/nexus-dashboard-caddy-cert.prev'
CONFINE = '/etc/caddy'


def die(msg, code=1):
    sys.stderr.write(msg if msg.endswith('\n') else msg + '\n')
    sys.exit(code)


def reload_if_active(restore):
    """`systemctl reload caddy` when it is running; on a refused reload call
    restore() and exit non-zero so file and running config never diverge."""
    act = subprocess.run(['systemctl', 'is-active', '--quiet', 'caddy'])
    if act.returncode != 0:
        print('applied (caddy is not running; the file is picked up at next start)')
        return
    r = subprocess.run(['systemctl', 'reload', 'caddy'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die('caddy reload failed (%s):\n%s' % (restore(), r.stderr or r.stdout))
    print('applied')


def do_apply():
    new = sys.stdin.read()
    if not os.path.isdir(CONFINE):
        die('%s does not exist — is caddy installed?' % CONFINE)
    prev = None
    fd, tmp = tempfile.mkstemp(prefix='.nexus-caddy.', dir=CONFINE)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(new)
        os.chmod(tmp, 0o644)
        v = subprocess.run(['caddy', 'validate', '--adapter', 'caddyfile',
                            '--config', tmp], capture_output=True, text=True)
        if v.returncode != 0:
            die('caddy validate rejected the config:\n' + (v.stderr or v.stdout))
        if os.path.exists(MANAGED):
            with open(MANAGED) as f:
                prev = f.read()
            bfd = os.open(BACKUP, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(bfd, 'w') as f:
                f.write(prev)
        os.replace(tmp, MANAGED)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)

    def restore():
        if prev is None:
            return 'previous Caddyfile was absent'
        with open(MANAGED, 'w') as f:
            f.write(prev)
        return 'previous Caddyfile restored'
    reload_if_active(restore)


def _confined(path):
    rp = os.path.realpath(path)
    return rp if rp.startswith(CONFINE + os.sep) and rp != MANAGED else None


def do_cert(cert_path, key_path):
    cp, kp = _confined(cert_path), _confined(key_path)
    if not cp or not kp or cp == kp:
        die('cert/key must be two distinct files under %s' % CONFINE, 2)
    if not (os.path.isfile(cp) and os.path.isfile(kp)):
        die('replace-only: both target files must already exist', 2)
    try:
        data = json.loads(sys.stdin.read())
        cert, key = data['cert'], data['key']
    except (ValueError, KeyError, TypeError):
        die('expected JSON {"cert": pem, "key": pem} on stdin', 2)

    def ossl(args):
        return subprocess.run(['openssl'] + args, capture_output=True, text=True)

    prev = {}
    pairs = []
    tmps = []
    try:
        for path, content in ((cp, cert), (kp, key)):
            fd, tmp = tempfile.mkstemp(prefix='.nexus-cert.',
                                       dir=os.path.dirname(path))
            tmps.append(tmp)
            with os.fdopen(fd, 'w') as f:
                f.write(content if content.endswith('\n') else content + '\n')
            pairs.append((path, tmp))
        tc, tk = pairs[0][1], pairs[1][1]
        if ossl(['x509', '-in', tc, '-noout']).returncode != 0:
            die('invalid certificate (openssl x509 rejected it)')
        if ossl(['pkey', '-in', tk, '-noout']).returncode != 0:
            die('invalid private key (openssl pkey rejected it)')
        cpub = ossl(['x509', '-in', tc, '-noout', '-pubkey']).stdout.strip()
        kpub = ossl(['pkey', '-in', tk, '-pubout']).stdout.strip()
        if not cpub or cpub != kpub:
            die('certificate and private key do not match')
        # Back up + preserve owner/mode of the files being replaced.
        for suffix, (path, tmp) in zip(('.crt', '.key'), pairs):
            st = os.stat(path)
            with open(path, 'rb') as f:
                prev[path] = (f.read(), st)
            bfd = os.open(CERT_BACKUP + suffix,
                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(bfd, 'wb') as f:
                f.write(prev[path][0])
            os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, st.st_mode & 0o777)
        for path, tmp in pairs:
            os.replace(tmp, path)
    finally:
        for tmp in tmps:
            if os.path.exists(tmp):
                os.remove(tmp)

    def restore():
        for path, (content, st) in prev.items():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(content)
            os.chown(path, st.st_uid, st.st_gid)
            os.chmod(path, st.st_mode & 0o777)
        return 'previous cert/key restored'
    reload_if_active(restore)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == 'apply':
        do_apply()
    elif len(sys.argv) == 4 and sys.argv[1] == 'cert':
        do_cert(sys.argv[2], sys.argv[3])
    else:
        die('usage: nexus-dashboard-caddy apply  (Caddyfile on stdin)\n'
            '       nexus-dashboard-caddy cert <cert-path> <key-path>  '
            '(JSON {"cert","key"} PEMs on stdin)', 2)


if __name__ == '__main__':
    main()
HELPER
chown root:root "$CADDY_HELPER"
chmod 755 "$CADDY_HELPER"

info "Installing snapshot browse/restore helper..."
SNAPFS_HELPER="/usr/local/sbin/nexus-dashboard-snap-fs"
cat > "$SNAPFS_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard snapshot browser / file restore.
# SECURITY: this script is the trust boundary and enforces its own confinement
# with realpath() — every resolved path must stay inside the snapshot root (for
# reads) or the live dataset root (for restore writes).
import os
import re
import sys
import json
import time
import shutil
import subprocess

RE_DATASET = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_./-]*$')
RE_SNAPNAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$')


def die(msg, code=2):
    sys.stderr.write(str(msg) + '\n')
    sys.exit(code)


def mountpoint(dataset):
    try:
        mp = subprocess.run(['zfs', 'get', '-H', '-o', 'value', 'mountpoint', dataset],
                            capture_output=True, text=True).stdout.strip()
    except OSError as e:
        die('zfs: %s' % e)
    if not mp or mp in ('none', 'legacy', '-') or not mp.startswith('/'):
        die('dataset has no usable mountpoint')
    if not os.path.isdir(mp):
        die('mountpoint not present')
    return mp


def confined(base, *parts):
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, *[p.lstrip('/') for p in parts if p]))
    if target != base_real and not target.startswith(base_real + os.sep):
        die('path escapes confinement')
    return target


def confined_parent(base, rel):
    base_real = os.path.realpath(base)
    dest = os.path.normpath(os.path.join(base_real, rel.lstrip('/')))
    parent_real = os.path.realpath(os.path.dirname(dest))
    if parent_real != base_real and not parent_real.startswith(base_real + os.sep):
        die('destination escapes confinement')
    return os.path.join(parent_real, os.path.basename(dest))


def cmd_browse(dataset, snap, rel):
    mp = mountpoint(dataset)
    snaproot = confined(os.path.join(mp, '.zfs', 'snapshot'), snap)
    target = confined(snaproot, rel)
    if not os.path.isdir(target):
        die('not a directory')
    entries = []
    with os.scandir(target) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
                entries.append({
                    'name': e.name,
                    'type': 'dir' if e.is_dir(follow_symlinks=False) else
                            ('link' if e.is_symlink() else 'file'),
                    'size': st.st_size,
                    'mtime': int(st.st_mtime),
                })
            except OSError:
                continue
    entries.sort(key=lambda x: (x['type'] != 'dir', x['name'].lower()))
    print(json.dumps({'path': rel, 'entries': entries}))


def cmd_restore(dataset, snap, rel, mode):
    if not rel or rel in ('.', '/'):
        die('refusing to restore the dataset root')
    mp = mountpoint(dataset)
    snaproot = confined(os.path.join(mp, '.zfs', 'snapshot'), snap)
    src = confined(snaproot, rel)
    if not os.path.exists(src):
        die('source not found in snapshot')
    dest = confined_parent(mp, rel)
    if mode == 'copy':
        base = dest + '.restored-' + time.strftime('%Y%m%d-%H%M%S')
        dest = base
        n = 1
        while os.path.exists(dest):
            dest = '%s-%d' % (base, n)
            n += 1
    elif mode == 'inplace':
        if os.path.isdir(dest) and not os.path.islink(dest):
            die('inplace restore of a directory over an existing directory is not allowed')
    else:
        die('invalid mode')
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dest, symlinks=True)
    else:
        if mode == 'inplace' and os.path.exists(dest):
            os.remove(dest)
        shutil.copy2(src, dest, follow_symlinks=False)
    print(json.dumps({'success': True, 'restored_to': dest}))


def main():
    if len(sys.argv) < 5:
        die('usage: snap-fs <browse|restore> <dataset> <snapshot> <relpath> [mode]')
    action, dataset, snap, rel = sys.argv[1:5]
    mode = sys.argv[5] if len(sys.argv) > 5 else 'copy'
    if not RE_DATASET.match(dataset):
        die('invalid dataset')
    if not RE_SNAPNAME.match(snap):
        die('invalid snapshot')
    if '\x00' in rel or '\n' in rel:
        die('invalid path')
    if action == 'browse':
        cmd_browse(dataset, snap, rel)
    elif action == 'restore':
        cmd_restore(dataset, snap, rel, mode)
    else:
        die('unknown action')


if __name__ == '__main__':
    main()
HELPER
chown root:root "$SNAPFS_HELPER"; chmod 755 "$SNAPFS_HELPER"

info "Installing disk mount helper..."
MOUNT_HELPER="/usr/local/sbin/nexus-dashboard-mount"
cat > "$MOUNT_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard plain-disk mount feature.
# Confines every mount point to /mnt or /media, forces a safe fstab option set
# (always nofail), and only ever edits its own delimited block in /etc/fstab.
import os
import re
import subprocess
import sys

BASES = ('/mnt', '/media')
FSTYPES = {'ext4', 'xfs', 'vfat', 'exfat'}
NON_MOUNTABLE = {'zfs_member', 'LVM2_member', 'linux_raid_member', 'swap'}
FSTAB = '/etc/fstab'
BEGIN = '# >>> nexus-dashboard managed >>>'
END = '# <<< nexus-dashboard managed <<<'
OPTS = 'defaults,nofail'

RE_PART = re.compile(r'^[a-z0-9]+\Z')
RE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
RE_UUID = re.compile(r'^[A-Za-z0-9-]{1,64}\Z')


def die(msg, code=2):
    sys.stderr.write(msg.rstrip() + '\n')
    sys.exit(code)


def fstype_of(part):
    r = subprocess.run(['lsblk', '-no', 'FSTYPE', '/dev/' + part],
                       capture_output=True, text=True)
    return (r.stdout.splitlines() or [''])[0].strip()


def target_for(name, base):
    if base not in BASES or not RE_NAME.match(name):
        die('invalid mount point')
    mp = os.path.join(base, name)
    if os.path.dirname(os.path.normpath(mp)) != base:
        die('mount point escapes base')
    return mp


def do_mount(part, name, base):
    if not RE_PART.match(part):
        die('invalid device')
    mp = target_for(name, base)
    if not os.path.exists('/dev/' + part):
        die('not a block device')
    fst = fstype_of(part)
    if not fst or fst in NON_MOUNTABLE:
        die('not a mountable filesystem')
    if subprocess.run(['findmnt', '-rno', 'TARGET', '/dev/' + part],
                      capture_output=True, text=True).stdout.strip():
        die('already mounted')
    os.makedirs(mp, exist_ok=True)
    r = subprocess.run(['mount', '/dev/' + part, mp], capture_output=True, text=True)
    if r.returncode != 0:
        die('mount failed: ' + (r.stderr or r.stdout), 1)
    print('mounted ' + mp)


def do_umount(part):
    if not RE_PART.match(part):
        die('invalid device')
    tgt = subprocess.run(['findmnt', '-rno', 'TARGET', '/dev/' + part],
                         capture_output=True, text=True).stdout.strip()
    if not tgt:
        die('not mounted')
    if not any(tgt == b or tgt.startswith(b + '/') for b in BASES):
        die('refusing to unmount %s (not under %s)' % (tgt, '/'.join(BASES)))
    r = subprocess.run(['umount', tgt], capture_output=True, text=True)
    if r.returncode != 0:
        die('umount failed: ' + (r.stderr or r.stdout), 1)
    try:
        os.rmdir(tgt)
    except OSError:
        pass
    print('unmounted ' + tgt)


def _read_managed():
    try:
        lines = open(FSTAB).read().splitlines()
    except OSError:
        return [], {}, []
    if BEGIN in lines and END in lines:
        i, j = lines.index(BEGIN), lines.index(END)
        entries = {}
        for ln in lines[i + 1:j]:
            s = ln.strip()
            if s.startswith('UUID='):
                f = s.split()
                if len(f) >= 3:
                    entries[f[0][len('UUID='):]] = (f[1], f[2])
        return lines[:i], entries, lines[j + 1:]
    return lines, {}, []


def _write_managed(entries):
    before, _, after = _read_managed()
    block = [BEGIN]
    for uuid, (mp, fst) in sorted(entries.items()):
        block.append('UUID=%s %s %s %s 0 2' % (uuid, mp, fst, OPTS))
    block.append(END)
    out = before
    if before and before[-1].strip():
        out = out + ['']
    out = out + block + after
    text = '\n'.join(out).rstrip('\n') + '\n'
    tmp = FSTAB + '.sd-tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, FSTAB)


def do_fstab_add(uuid, mp, fst):
    if not RE_UUID.match(uuid):
        die('invalid uuid')
    if fst not in FSTYPES:
        die('invalid fstype')
    if not any(mp == b or mp.startswith(b + '/') for b in BASES) or '..' in mp:
        die('mount point not under an allowed base')
    os.makedirs(mp, exist_ok=True)
    _, entries, _ = _read_managed()
    entries[uuid] = (mp, fst)
    _write_managed(entries)
    print('fstab updated')


def do_fstab_remove(uuid):
    if not RE_UUID.match(uuid):
        die('invalid uuid')
    _, entries, _ = _read_managed()
    if uuid in entries:
        del entries[uuid]
        _write_managed(entries)
    print('fstab updated')


def main():
    a = sys.argv[1:]
    if not a:
        die('usage: nexus-dashboard-mount {mount|umount|fstab-add|fstab-remove} ...')
    cmd = a[0]
    if cmd == 'mount' and len(a) == 4:
        do_mount(a[1], a[2], a[3])
    elif cmd == 'umount' and len(a) == 2:
        do_umount(a[1])
    elif cmd == 'fstab-add' and len(a) == 4:
        do_fstab_add(a[1], a[2], a[3])
    elif cmd == 'fstab-remove' and len(a) == 2:
        do_fstab_remove(a[1])
    else:
        die('bad arguments')


if __name__ == '__main__':
    main()
HELPER
chown root:root "$MOUNT_HELPER"; chmod 755 "$MOUNT_HELPER"

info "Installing llama.cpp model-fetch helper..."
# Root-owned: trust boundary for downloading a GGUF into the root-owned models
# dir. Re-validates repo/filename, confines output, atomic rename; optional HF
# token read from stdin and passed to curl via inline config (never on argv).
MODEL_FETCH_HELPER="/usr/local/sbin/nexus-dashboard-model-fetch"
cat > "$MODEL_FETCH_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard llama.cpp model download.
#   nexus-dashboard-model-fetch <repo> <filename.gguf>
# An optional Hugging Face token may be supplied on the first line of stdin.
import os, re, sys, subprocess

MODELS = '/usr/share/models'   # the ONLY directory this helper will write
RE_REPO = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$')
RE_FILE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*\.gguf$')


def die(m, c=2):
    sys.stderr.write(str(m).rstrip() + '\n')
    sys.exit(c)


def main():
    if len(sys.argv) != 3:
        die('usage: nexus-dashboard-model-fetch <repo> <filename.gguf>')
    repo, fn = sys.argv[1], sys.argv[2]
    if not RE_REPO.match(repo):
        die('invalid repo')
    if not RE_FILE.match(fn):
        die('invalid filename')
    token = ''
    try:
        if not sys.stdin.isatty():
            token = sys.stdin.readline().strip()
    except Exception:
        token = ''
    dest = os.path.join(MODELS, fn)
    if os.path.dirname(os.path.realpath(dest)) != os.path.realpath(MODELS):
        die('path escapes models dir')
    if os.path.exists(dest):
        die('already exists', 1)
    os.makedirs(MODELS, exist_ok=True)
    part = dest + '.partial'
    url = 'https://huggingface.co/%s/resolve/main/%s' % (repo, fn)
    cfg = 'url = "%s"\noutput = "%s"\nfail\nlocation\nretry = 3\n' % (url, part)
    if token:
        cfg += 'header = "Authorization: Bearer %s"\n' % token
    try:
        r = subprocess.run(['curl', '-K', '-'], input=cfg, text=True)
    except FileNotFoundError:
        die('curl not found', 1)
    if r.returncode != 0:
        try:
            os.remove(part)
        except OSError:
            pass
        die('download failed (curl exit %d)' % r.returncode, 1)
    os.replace(part, dest)
    print('ok')


if __name__ == '__main__':
    main()
HELPER
chown root:root "$MODEL_FETCH_HELPER"; chmod 755 "$MODEL_FETCH_HELPER"

info "Installing MiniDLNA rebuild helper..."
# Root-owned (NOT writable by dashboard). Forces a full MiniDLNA rebuild: stop the
# service, delete files.db (confined to the hard-coded cache dir), start (minidlna
# rebuilds from a full scan when files.db is missing). The cache dir is a constant
# here (never from argv) so the grant can't delete arbitrary files as root.
DLNA_RESCAN_HELPER="/usr/local/sbin/nexus-dashboard-dlna-rescan"
cat > "$DLNA_RESCAN_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard MiniDLNA database rebuild.
#   nexus-dashboard-dlna-rescan
# Forces a full rebuild: stop minidlna, delete files.db (confined to CACHE), start.
import os, sys, subprocess

CACHE = '/var/cache/minidlna'   # the ONLY directory this helper will touch
SERVICE = 'minidlna'


def die(m, c=1):
    sys.stderr.write(str(m).rstrip() + '\n')
    sys.exit(c)


def main():
    db = os.path.join(CACHE, 'files.db')
    if os.path.dirname(os.path.realpath(db)) != os.path.realpath(CACHE):
        die('path escapes cache dir')
    subprocess.run(['systemctl', 'stop', SERVICE])
    try:
        if os.path.exists(db):
            os.remove(db)
    except OSError as e:
        die('failed to remove db: %s' % e)
    start = subprocess.run(['systemctl', 'start', SERVICE])
    if start.returncode != 0:
        die('failed to start %s (exit %d)' % (SERVICE, start.returncode))
    print('ok')


if __name__ == '__main__':
    main()
HELPER
chown root:root "$DLNA_RESCAN_HELPER"; chmod 755 "$DLNA_RESCAN_HELPER"

info "Installing MiniDLNA stats helper..."
# Root-owned read helper: opens minidlna's files.db read-only (the cache dir is
# minidlna-only on some distros) and prints fixed media-library COUNTs as JSON.
# No writes, no arbitrary SQL. NOT writable by dashboard.
DLNA_STATS_HELPER="/usr/local/sbin/nexus-dashboard-dlna-stats"
cat > "$DLNA_STATS_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned read helper for the Nexus Dashboard MiniDLNA library stats.
#   nexus-dashboard-dlna-stats   ->  prints JSON of the files.db media counts
import json, os, sqlite3

CONF = '/etc/minidlna.conf'
DEFAULT_CACHE = '/var/cache/minidlna'


def db_dir():
    try:
        with open(CONF) as f:
            for line in f:
                s = line.strip()
                if s.startswith('db_dir') and '=' in s:
                    return s.split('=', 1)[1].strip()
    except OSError:
        pass
    return DEFAULT_CACHE


def main():
    path = os.path.join(db_dir(), 'files.db')
    out = {'available': False, 'path': path}
    if os.path.isfile(path):
        try:
            con = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=2.0)
            cur = con.cursor()

            def n(where=''):
                cur.execute('SELECT count(*) FROM DETAILS' + where)
                return cur.fetchone()[0]

            out['audio'] = n(" WHERE MIME LIKE 'audio/%'")
            out['video'] = n(" WHERE MIME LIKE 'video/%'")
            out['image'] = n(" WHERE MIME LIKE 'image/%'")
            out['objects'] = n()
            out['size'] = os.path.getsize(path)
            out['available'] = True
            con.close()
        except Exception as e:
            out['error'] = str(e)[:120]
    print(json.dumps(out))


if __name__ == '__main__':
    main()
HELPER
chown root:root "$DLNA_STATS_HELPER"; chmod 755 "$DLNA_STATS_HELPER"

info "Setting up log directory..."
mkdir -p /var/log/nexus-dashboard
chown $DASHBOARD_USER:$DASHBOARD_USER /var/log/nexus-dashboard

info "Setting file ownership..."
info "Setting up dnsmasq (DNS & DHCP) module directories..."
mkdir -p "$DASHBOARD_DIR/dnsmasq/render/dnsmasq.d" "$DASHBOARD_DIR/dnsmasq/render/hosts.d" \
         "$DASHBOARD_DIR/dnsmasq/state" "$DASHBOARD_DIR/dnsmasq/leases"
if command -v dnsmasq >/dev/null 2>&1; then
    mkdir -p /etc/dnsmasq.d
    cat > /etc/dnsmasq.d/zz-nexus-dashboard.conf << DROPIN
# Managed by Nexus Dashboard (dnsmasq module). conf-dir is empty until the
# module is enabled on the Modules page and configured (a no-op till then).
conf-dir=$DASHBOARD_DIR/dnsmasq/render/dnsmasq.d,*.conf
DROPIN
    info "dnsmasq conf-dir drop-in written (enable the DNS module to use it)"
    # RHEL/Rocky ship /etc/dnsmasq.conf with conf-dir COMMENTED, so
    # /etc/dnsmasq.d is not read by default (Debian enables it). Make sure our
    # drop-in is actually seen — idempotent: only act if no active conf-dir for
    # that dir already exists.
    if [ -f /etc/dnsmasq.conf ] && ! grep -Eq '^[[:space:]]*conf-dir=/etc/dnsmasq\.d' /etc/dnsmasq.conf; then
        if grep -Eq '^[[:space:]]*#[[:space:]]*conf-dir=/etc/dnsmasq\.d' /etc/dnsmasq.conf; then
            sed -i -E 's|^[[:space:]]*#[[:space:]]*(conf-dir=/etc/dnsmasq\.d.*)|\1|' /etc/dnsmasq.conf
            info "Enabled the commented conf-dir=/etc/dnsmasq.d line in /etc/dnsmasq.conf"
        else
            printf '\n# Added by Nexus Dashboard so /etc/dnsmasq.d drop-ins are read.\nconf-dir=/etc/dnsmasq.d,*.conf\n' >> /etc/dnsmasq.conf
            info "Appended conf-dir=/etc/dnsmasq.d to /etc/dnsmasq.conf"
        fi
    fi
else
    warn "dnsmasq not installed — skipping conf-dir drop-in (add by hand if you install dnsmasq later)"
fi

chown -R $DASHBOARD_USER:$DASHBOARD_USER $DASHBOARD_DIR

info "Creating systemd service..."
cat > /etc/systemd/system/nexus-dashboard.service << SERVICE
[Unit]
Description=Nexus Dashboard
After=network.target zfs.target ${NFS_SERVICE}.service ${SMB_SERVICE}.service
Wants=zfs.target ${NFS_SERVICE}.service ${SMB_SERVICE}.service

[Service]
Type=simple
User=dashboard
Group=dashboard
WorkingDirectory=/opt/nexus-dashboard
Environment=DASHBOARD_UNIT_PREFIX=nexus-dashboard
Environment=FLASK_ENV=production
ExecStart=/opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/var/log/nexus-dashboard/app.log
StandardError=append:/var/log/nexus-dashboard/app.log

[Install]
WantedBy=multi-user.target
SERVICE

# Background timers (installed disabled; the dashboard enables each only when
# its feature is configured — identical to the Debian/Ubuntu install).
for unit in autosnap replicate alerts maintenance history; do
    case $unit in
        autosnap)    desc="automatic ZFS snapshots"; tick="autosnap-tick"; cal="hourly" ;;
        replicate)   desc="ZFS replication (send/receive)"; tick="replicate-tick"; cal="hourly" ;;
        alerts)      desc="health-alert notifier"; tick="alerts-tick"; cal="*:0/15" ;;
        maintenance) desc="scheduled maintenance (scrubs + SMART self-tests)"; tick="maintenance-tick"; cal="hourly" ;;
        history)     desc="metrics history sampler"; tick="history-tick"; cal="*:0/5" ;;
    esac
    cat > /etc/systemd/system/nexus-dashboard-$unit.service << SERVICE
[Unit]
Description=Nexus Dashboard $desc
[Service]
Type=oneshot
User=dashboard
Group=dashboard
WorkingDirectory=/opt/nexus-dashboard
Environment=DASHBOARD_UNIT_PREFIX=nexus-dashboard
ExecStart=/opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py $tick
SERVICE
    cat > /etc/systemd/system/nexus-dashboard-$unit.timer << TIMER
[Unit]
Description=Nexus Dashboard $desc timer
[Timer]
OnCalendar=$cal
Persistent=true
[Install]
WantedBy=timers.target
TIMER
done

info "Enabling and starting services..."
systemctl daemon-reload
# History sampler is on by default (the feature timers above stay opt-in).
systemctl enable --now nexus-dashboard-history.timer 2>/dev/null || true
systemctl enable zfs.target 2>/dev/null || true
systemctl enable $ISCSI_SERVICE 2>/dev/null || true
systemctl enable $NFS_SERVICE 2>/dev/null || true
systemctl enable $SMB_SERVICE 2>/dev/null || true
systemctl start $ISCSI_SERVICE 2>/dev/null || true
systemctl start $NFS_SERVICE 2>/dev/null || true
systemctl start $SMB_SERVICE 2>/dev/null || true
systemctl enable nexus-dashboard.service

# ── SELinux ───────────────────────────────────────────────────────────
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    info "SELinux is Enforcing — applying booleans + policy for the dashboard..."
    # Let Samba/NFS export arbitrary dashboard-managed paths.
    setsebool -P samba_export_all_rw on 2>/dev/null || true
    setsebool -P nfs_export_all_rw on 2>/dev/null || true
    setsebool -P samba_export_all_ro on 2>/dev/null || true
    if [ -f "$SCRIPT_DIR/selinux/nexus-dashboard.pp" ]; then
        semodule -i "$SCRIPT_DIR/selinux/nexus-dashboard.pp" \
            && info "Installed SELinux policy module nexus-dashboard.pp" \
            || warn "Failed to install bundled SELinux policy module."
    else
        warn "No bundled SELinux policy module found. The dashboard service runs"
        warn "as the 'dashboard' user and may hit AVC denials. After exercising"
        warn "the UI, review: ausearch -m avc -ts recent | audit2allow -m mypol"
    fi
fi

# ── Firewall (firewalld) ──────────────────────────────────────────────
info "Configuring firewall (firewalld)..."
if systemctl is-active firewalld >/dev/null 2>&1; then
    for p in $DASHBOARD_PORT 3260 2049 445 139 111 8200; do
        firewall-cmd --permanent --add-port=$p/tcp >/dev/null 2>&1 || true
    done
    firewall-cmd --permanent --add-port=1900/udp >/dev/null 2>&1 || true   # DLNA SSDP
    firewall-cmd --reload >/dev/null 2>&1 || true
else
    warn "firewalld is not active; skipping firewall rules."
fi

info "Installation complete!"
echo ""
echo "Starting dashboard..."
systemctl start nexus-dashboard.service || true

echo ""
echo "=== Summary ==="
echo "Dashboard URL:    https://$(hostname -I | awk '{print $1}'):$DASHBOARD_PORT"
echo "                  (self-signed cert by default — your browser will warn once)"
echo "Log file:         /var/log/nexus-dashboard/app.log"
echo "Status command:   sudo systemctl status nexus-dashboard.service"
echo ""
echo "Login:            An 'admin' account is created on first start with a"
echo "                  random password, printed to the log file above:"
echo "                    sudo grep -A2 'initial admin account' /var/log/nexus-dashboard/app.log"
echo "                  Or set one:"
echo "                    sudo -u $DASHBOARD_USER $DASHBOARD_DIR/venv/bin/python $DASHBOARD_DIR/app.py set-password admin"
echo ""
echo "Notes for RHEL/Rocky:"
echo "  - The Network module is netplan-based (Ubuntu-only); only hostname/domain"
echo "    changes work here. Interface/bridge editing is unsupported on RHEL."
echo "  - ZFS comes from the OpenZFS repo and may be unavailable on a brand-new"
echo "    EL release (e.g. EL10 before OpenZFS publishes a matching repo)."
