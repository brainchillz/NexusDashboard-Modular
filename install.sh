#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="/opt/nexus-dashboard"
DASHBOARD_USER="dashboard"
DASHBOARD_PORT="${DASHBOARD_PORT:-8443}"

echo "=== Nexus Dashboard Installer (Debian/Ubuntu) ==="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    error "Please run as root or with sudo"
    exit 1
fi

info "Installing prerequisite packages..."
if [ -f "$SCRIPT_DIR/install-prerequisites.sh" ]; then
    SD_SKIP_NEXT_STEP=1 bash "$SCRIPT_DIR/install-prerequisites.sh"
else
    error "install-prerequisites.sh not found next to install.sh."
    error "Run the prerequisite installer first, then re-run install.sh."
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
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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
# NOTE: sudo matches the fully-resolved binary path, so each command is listed
# at every location it may live across Ubuntu releases (merged-/usr and not).

# Service control & logs
dashboard ALL=(ALL) NOPASSWD: /usr/bin/systemctl
dashboard ALL=(ALL) NOPASSWD: /bin/systemctl
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/systemctl
dashboard ALL=(ALL) NOPASSWD: /usr/bin/journalctl
dashboard ALL=(ALL) NOPASSWD: /bin/journalctl

# Disk / system inventory
dashboard ALL=(ALL) NOPASSWD: /usr/bin/lsblk
dashboard ALL=(ALL) NOPASSWD: /bin/lsblk
dashboard ALL=(ALL) NOPASSWD: /sbin/lsblk
dashboard ALL=(ALL) NOPASSWD: /usr/bin/lsscsi
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/lsscsi
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/ip
dashboard ALL=(ALL) NOPASSWD: /usr/bin/ip
dashboard ALL=(ALL) NOPASSWD: /sbin/ip
dashboard ALL=(ALL) NOPASSWD: /bin/ip
dashboard ALL=(ALL) NOPASSWD: /usr/bin/dpkg-query
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/smartctl
dashboard ALL=(ALL) NOPASSWD: /usr/bin/smartctl
# Host firewall (Firewall module). The app refuses rules that would block its
# own port; ufw only ever gets fixed keywords + validated port/CIDR arguments.
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/ufw
# Disk wipe (blank a free/stale disk: stop stale md, zero superblocks, clear
# signatures + partition table). Eligibility is enforced in app.py.
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/mdadm, /sbin/mdadm
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/wipefs, /sbin/wipefs
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/sgdisk, /sbin/sgdisk
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/partprobe, /sbin/partprobe
# Disk locate: enclosure LED + read-only activity. The read goes through a
# fixed root-owned wrapper that only ever reads a device into /dev/null, so it
# can never write a disk (sudo forbids wildcards in command arguments).
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/ledctl, /sbin/ledctl
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-locate-read
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-iscsi-sessions
# Snapshot browser / single-file restore. Root-owned helper that does its own
# realpath confinement (reads inside .zfs/snapshot, writes inside the live
# dataset) — it is the trust boundary, so it must not be writable by dashboard.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-snap-fs
# Network module: hostname + a root-owned helper that writes the dashboard's
# netplan file and runs `netplan generate`/`apply` (validates before applying;
# restores on failure). The helper is the trust boundary — not writable by dashboard.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-netplan
dashboard ALL=(ALL) NOPASSWD: /usr/bin/hostnamectl, /usr/sbin/hostnamectl
# Caddy module: a root-owned helper that validates a candidate Caddyfile with
# `caddy validate` BEFORE writing it, then reloads the service (restoring the
# previous file if the reload is refused). The helper is the trust boundary —
# not writable by dashboard.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-caddy
# Plain-disk mount: a root-owned helper that mounts/unmounts under /mnt|/media
# and edits its own block in /etc/fstab (always `nofail`). It validates every
# argument and confines the mount point — it is the trust boundary, so it must
# not be writable by dashboard. (mount/umount/tee /etc/fstab are deliberately
# NOT granted directly — that would be a root-escalation primitive.)
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-mount
# llama.cpp model download: a root-owned helper that pulls a GGUF from Hugging
# Face into the models dir (re-validates repo/filename, confines output to that
# dir, atomic rename). The helper is the trust boundary — not writable by dashboard.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-model-fetch
# MiniDLNA DB rebuild: a root-owned helper that stops the service, deletes files.db
# (confined to the hard-coded cache dir), and starts (minidlna rebuilds from a full
# scan when files.db is missing). Deleting the db as root is escalation-sensitive,
# so it goes through this wrapper -- never a bare rm grant. Trust boundary; not
# writable by dashboard.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-dlna-rescan
# MiniDLNA library stats: a root-owned read helper that opens files.db read-only
# (the cache dir is minidlna-only on some distros) and prints fixed COUNT queries
# as JSON. Read-only, no arbitrary SQL. Trust boundary; not writable by dashboard.
dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-dlna-stats

# LVM (read + manage; destructive ops are guarded in app.py)
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/pvs, /usr/sbin/vgs, /usr/sbin/lvs
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/pvcreate, /usr/sbin/pvremove, /usr/sbin/pvresize, /usr/sbin/pvmove
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/vgcreate, /usr/sbin/vgremove, /usr/sbin/vgextend, /usr/sbin/vgreduce
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/lvcreate, /usr/sbin/lvremove, /usr/sbin/lvextend, /usr/sbin/lvresize
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/mkfs.ext4, /usr/sbin/mkfs.xfs
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/mkfs.vfat, /sbin/mkfs.vfat
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/mkfs.exfat, /sbin/mkfs.exfat

# ZFS
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/zpool
dashboard ALL=(ALL) NOPASSWD: /sbin/zpool
dashboard ALL=(ALL) NOPASSWD: /bin/zpool
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/zfs
dashboard ALL=(ALL) NOPASSWD: /sbin/zfs
dashboard ALL=(ALL) NOPASSWD: /bin/zfs

# iSCSI (LIO / targetcli-fb)
dashboard ALL=(ALL) NOPASSWD: /usr/bin/targetcli
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/targetcli

# NFS
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/exportfs
dashboard ALL=(ALL) NOPASSWD: /sbin/exportfs

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

# Config writers - restricted to the exact files/forms app.py invokes, so the
# grant cannot be abused to write arbitrary files or set arbitrary modes as root.
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/exports, /bin/tee /etc/exports
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/samba/smb.conf, /bin/tee /etc/samba/smb.conf
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/hosts, /bin/tee /etc/hosts
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/mdadm/mdadm.conf, /bin/tee /etc/mdadm/mdadm.conf
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/llama.conf, /bin/tee /etc/llama.conf
dashboard ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/minidlna.conf, /bin/tee /etc/minidlna.conf
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/update-initramfs
# Load RAID personalities for array creation (exact modules only, no wildcard).
dashboard ALL=(ALL) NOPASSWD: /usr/sbin/modprobe raid0, /usr/sbin/modprobe raid1, /usr/sbin/modprobe raid456, /usr/sbin/modprobe raid10
dashboard ALL=(ALL) NOPASSWD: /usr/bin/mkdir -p -- *, /bin/mkdir -p -- *
dashboard ALL=(ALL) NOPASSWD: /usr/bin/rmdir *, /bin/rmdir *
dashboard ALL=(ALL) NOPASSWD: /usr/bin/chmod 2775 -- *, /bin/chmod 2775 -- *
# dnsmasq (DNS & DHCP) module: the DHCP-conflict probe binds privileged UDP
# port 68 to detect a second DHCP server before the DHCP feature is enabled.
# Best-effort — the toggle still works without this line (it just skips the
# warning). systemctl/journalctl for dnsmasq are already covered by the
# blanket lines above; the module renders its own config into a
# dashboard-owned conf-dir, so no root helper is needed.
dashboard ALL=(ALL) NOPASSWD: /opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py dhcp-probe*
SUDOERS

chmod 440 $SUDOERS_FILE
info "Sudoers configured at $SUDOERS_FILE"

info "Installing disk-locate read helper..."
# Root-owned (NOT writable by the dashboard user) so granting it via sudo is
# safe. It only ever reads a validated device into /dev/null.
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
chown root:root "$LOCATE_HELPER"
chmod 755 "$LOCATE_HELPER"

info "Installing iSCSI sessions helper..."
# Root-owned read-only helper: reports connected iSCSI initiators per target
# from configfs (which targetcli's `sessions` misses for demo-mode sessions).
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
chown root:root "$SESSIONS_HELPER"
chmod 755 "$SESSIONS_HELPER"

info "Installing snapshot browse/restore helper..."
# Root-owned helper that resolves & confines snapshot/live paths (realpath) and
# does the read/copy as root. It is the security boundary — must be root-owned
# and NOT writable by the dashboard user.
SNAPFS_HELPER="/usr/local/sbin/nexus-dashboard-snap-fs"
cat > "$SNAPFS_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard snapshot browser / file restore.
# Resolves a dataset's snapshot dir (<mountpoint>/.zfs/snapshot/<snap>) and the
# live dataset root, and performs read-only listing or a confined copy.
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
chown root:root "$SNAPFS_HELPER"
chmod 755 "$SNAPFS_HELPER"

info "Installing network (netplan) helper..."
# Root-owned: writes the dashboard's netplan file, validates with `netplan
# generate` (restores on failure so a bad config never gets applied), then
# `netplan apply`. Only ever writes one fixed path. NOT writable by dashboard.
NETPLAN_HELPER="/usr/local/sbin/nexus-dashboard-netplan"
cat > "$NETPLAN_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard network module.
#   apply  (netplan YAML on stdin): back up the managed file, write the new one
#          (0600), `netplan generate` to validate (restore + non-zero exit on
#          failure so a bad config never reaches apply), then `netplan apply`.
import os
import sys
import subprocess

MANAGED = '/etc/netplan/90-nexus-dashboard.yaml'
BACKUP = '/run/nexus-dashboard-netplan.prev'


def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(path, 0o600)


def main():
    if len(sys.argv) < 2 or sys.argv[1] != 'apply':
        sys.stderr.write('usage: nexus-dashboard-netplan apply  (netplan YAML on stdin)\n')
        sys.exit(2)
    new = sys.stdin.read()
    if 'network:' not in new:
        sys.stderr.write('refusing: input does not look like netplan YAML\n')
        sys.exit(2)
    had = os.path.exists(MANAGED)
    prev = ''
    if had:
        with open(MANAGED) as f:
            prev = f.read()
        _write(BACKUP, prev)
    _write(MANAGED, new)
    g = subprocess.run(['netplan', 'generate'], capture_output=True, text=True)
    if g.returncode != 0:
        if had:
            _write(MANAGED, prev)
        else:
            os.remove(MANAGED)
        sys.stderr.write('netplan generate rejected the config:\n' + (g.stderr or g.stdout))
        sys.exit(1)
    a = subprocess.run(['netplan', 'apply'], capture_output=True, text=True)
    if a.returncode != 0:
        sys.stderr.write('netplan apply failed:\n' + (a.stderr or a.stdout))
        sys.exit(1)
    print('applied')


if __name__ == '__main__':
    main()
HELPER
chown root:root "$NETPLAN_HELPER"
chmod 755 "$NETPLAN_HELPER"

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

info "Installing disk mount helper..."
# Root-owned: the trust boundary for plain-disk mounting. It confines every
# mount point to /mnt or /media, forces a safe fstab option set (always
# `nofail`, so a missing/yanked disk can NEVER block boot), and only ever edits
# its own delimited block in /etc/fstab. NOT writable by the dashboard user.
MOUNT_HELPER="/usr/local/sbin/nexus-dashboard-mount"
cat > "$MOUNT_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard plain-disk mount feature.
#   mount <part> <name> <base>   mount /dev/<part> at <base>/<name>
#   umount <part>                unmount /dev/<part> (must be under a base)
#   fstab-add <uuid> <mp> <fst>  add a UUID-based, nofail fstab entry
#   fstab-remove <uuid>          remove the managed fstab entry for <uuid>
# Every argument is validated here; this helper does NOT trust its caller.
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
    # Defence in depth: the resolved path must stay directly under the base.
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
    """Return (lines_before, {uuid:(mp,fst)}, lines_after) for the managed block."""
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
chown root:root "$MOUNT_HELPER"
chmod 755 "$MOUNT_HELPER"

info "Installing llama.cpp model-fetch helper..."
# Root-owned: the trust boundary for downloading a GGUF into the root-owned
# models dir. It re-validates the repo id + filename, confines output to the
# models dir, downloads to a .partial then atomically renames. The optional HF
# token is read from stdin and passed to curl via an inline config so it never
# lands on the process command line. NOT writable by the dashboard user.
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
    # Confinement: dest must resolve to a file directly inside MODELS.
    if os.path.dirname(os.path.realpath(dest)) != os.path.realpath(MODELS):
        die('path escapes models dir')
    if os.path.exists(dest):
        die('already exists', 1)
    os.makedirs(MODELS, exist_ok=True)
    part = dest + '.partial'
    url = 'https://huggingface.co/%s/resolve/main/%s' % (repo, fn)
    # curl reads options (incl. the auth header) from stdin so the token is never
    # visible in the process list.
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
chown root:root "$MODEL_FETCH_HELPER"
chmod 755 "$MODEL_FETCH_HELPER"

info "Installing MiniDLNA rebuild helper..."
# Root-owned (NOT writable by the dashboard user) so granting it via sudo is not
# an escalation. Forces a full MiniDLNA database rebuild: stop the service, delete
# files.db (confined to the hard-coded cache dir), run `minidlnad -R`, start. The
# cache dir is a constant here (never taken from argv) so the grant can't be abused
# to delete arbitrary files as root.
DLNA_RESCAN_HELPER="/usr/local/sbin/nexus-dashboard-dlna-rescan"
cat > "$DLNA_RESCAN_HELPER" << 'HELPER'
#!/usr/bin/env python3
# Root-owned helper for the Nexus Dashboard MiniDLNA database rebuild.
#   nexus-dashboard-dlna-rescan
# Forces a full rebuild: stop minidlna, delete files.db (confined to CACHE), start.
# MiniDLNA rebuilds the database from a full media scan when files.db is missing on
# startup, so this is a version-safe "rebuild from scratch" that leaves the daemon
# managed by systemd (a standalone `minidlnad -R` would daemonize and collide with
# the unit).
import os, sys, subprocess

CACHE = '/var/cache/minidlna'   # the ONLY directory this helper will touch
SERVICE = 'minidlna'


def die(m, c=1):
    sys.stderr.write(str(m).rstrip() + '\n')
    sys.exit(c)


def main():
    db = os.path.join(CACHE, 'files.db')
    # Confinement: db must resolve to a file directly inside CACHE.
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
chown root:root "$DLNA_RESCAN_HELPER"
chmod 755 "$DLNA_RESCAN_HELPER"

info "Installing MiniDLNA stats helper..."
# Root-owned read helper: the minidlna db/cache dir is minidlna-only (0750) on some
# distros, so the dashboard user can't read files.db directly. This opens it
# read-only and prints fixed media-library COUNTs as JSON — no writes, no arbitrary
# SQL. NOT writable by the dashboard user.
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
chown root:root "$DLNA_STATS_HELPER"
chmod 755 "$DLNA_STATS_HELPER"

info "Setting up log directory..."
mkdir -p /var/log/nexus-dashboard
chown $DASHBOARD_USER:$DASHBOARD_USER /var/log/nexus-dashboard

info "Setting up dnsmasq (DNS & DHCP) module directories..."
# The module is OFF by default; these dirs are its workspace when enabled. The
# render tree is where dnsmasq reads config from (root) and writes leases to.
mkdir -p "$DASHBOARD_DIR/dnsmasq/render/dnsmasq.d" "$DASHBOARD_DIR/dnsmasq/render/hosts.d" \
         "$DASHBOARD_DIR/dnsmasq/state" "$DASHBOARD_DIR/dnsmasq/leases"
if command -v dnsmasq >/dev/null 2>&1; then
    mkdir -p /etc/dnsmasq.d
    cat > /etc/dnsmasq.d/zz-nexus-dashboard.conf << DROPIN
# Managed by Nexus Dashboard (dnsmasq module). Pulls in the dashboard-rendered
# DNS/DHCP config. The module is DISABLED by default — until it is enabled on
# the Modules page and configured, the conf-dir below is empty (a no-op).
conf-dir=$DASHBOARD_DIR/dnsmasq/render/dnsmasq.d,*.conf
DROPIN
    info "dnsmasq conf-dir drop-in written (enable the DNS module to use it)"
    # Make sure /etc/dnsmasq.d is actually read (Debian enables this by default;
    # some derivatives ship it commented). Idempotent — only act if no active
    # conf-dir for that dir already exists.
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
    warn "dnsmasq not installed — skipping the conf-dir drop-in. To use the DNS module later:"
    warn "  apt install dnsmasq, then create /etc/dnsmasq.d/zz-nexus-dashboard.conf with:"
    warn "    conf-dir=$DASHBOARD_DIR/dnsmasq/render/dnsmasq.d,*.conf"
fi

info "Seeding default module state (fresh install only)..."
# A fresh node starts as a clean storage/sharing box: the AI Tools, LXD/Incus,
# Docker, and Caddy Proxy module groups are DISABLED by default. The operator
# enables whatever the node needs from the Modules page — it takes effect live,
# no restart. Written only if absent, so re-running the installer (or an
# in-place upgrade) never clobbers operator toggles. Ids map to the categories:
# AI Tools=gpu,llamacpp  LXD/Incus=instances,images,ctnetworks,portforward
# Docker=docker,compose  Caddy Proxy=caddy.
if [ ! -e "$DASHBOARD_DIR/modules.json" ]; then
    cat > "$DASHBOARD_DIR/modules.json" << 'MODULES'
{"disabled": ["caddy", "compose", "ctnetworks", "docker", "gpu", "images", "instances", "llamacpp", "portforward"], "enabled": []}
MODULES
    info "Default-off groups: AI Tools, LXD/Incus, Docker, Caddy Proxy"
else
    info "modules.json already present — leaving module state untouched"
fi

info "Setting file ownership..."
chown -R $DASHBOARD_USER:$DASHBOARD_USER $DASHBOARD_DIR

info "Creating systemd service..."
cat > /etc/systemd/system/nexus-dashboard.service << 'SERVICE'
[Unit]
Description=Nexus Dashboard
After=network.target zfs.target nfs-kernel-server.service smbd.service
Wants=zfs.target nfs-kernel-server.service smbd.service

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

info "Creating automatic-snapshot timer (installed disabled; the dashboard"
info "enables it only when you create an enabled snapshot schedule)..."
cat > /etc/systemd/system/nexus-dashboard-autosnap.service << 'SERVICE'
[Unit]
Description=Nexus Dashboard automatic ZFS snapshots
[Service]
Type=oneshot
User=dashboard
Group=dashboard
WorkingDirectory=/opt/nexus-dashboard
Environment=DASHBOARD_UNIT_PREFIX=nexus-dashboard
ExecStart=/opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py autosnap-tick
SERVICE
cat > /etc/systemd/system/nexus-dashboard-autosnap.timer << 'TIMER'
[Unit]
Description=Nexus Dashboard automatic ZFS snapshot timer
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target
TIMER

info "Creating ZFS replication timer (installed disabled; the dashboard enables"
info "it only when you create an enabled replication job)..."
cat > /etc/systemd/system/nexus-dashboard-replicate.service << 'SERVICE'
[Unit]
Description=Nexus Dashboard ZFS replication (send/receive)
[Service]
Type=oneshot
User=dashboard
Group=dashboard
WorkingDirectory=/opt/nexus-dashboard
Environment=DASHBOARD_UNIT_PREFIX=nexus-dashboard
ExecStart=/opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py replicate-tick
SERVICE
cat > /etc/systemd/system/nexus-dashboard-replicate.timer << 'TIMER'
[Unit]
Description=Nexus Dashboard ZFS replication timer
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target
TIMER

info "Creating alerting timer (installed disabled; the dashboard enables it when"
info "you turn on email/webhook notifications)..."
cat > /etc/systemd/system/nexus-dashboard-alerts.service << 'SERVICE'
[Unit]
Description=Nexus Dashboard health-alert notifier
[Service]
Type=oneshot
User=dashboard
Group=dashboard
WorkingDirectory=/opt/nexus-dashboard
Environment=DASHBOARD_UNIT_PREFIX=nexus-dashboard
ExecStart=/opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py alerts-tick
SERVICE
cat > /etc/systemd/system/nexus-dashboard-alerts.timer << 'TIMER'
[Unit]
Description=Nexus Dashboard health-alert timer
[Timer]
OnCalendar=*:0/15
Persistent=true
[Install]
WantedBy=timers.target
TIMER

info "Creating maintenance timer (installed disabled; the dashboard enables it"
info "when you add a scrub or SMART-test schedule)..."
cat > /etc/systemd/system/nexus-dashboard-maintenance.service << 'SERVICE'
[Unit]
Description=Nexus Dashboard scheduled maintenance (scrubs + SMART self-tests)
[Service]
Type=oneshot
User=dashboard
Group=dashboard
WorkingDirectory=/opt/nexus-dashboard
Environment=DASHBOARD_UNIT_PREFIX=nexus-dashboard
ExecStart=/opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py maintenance-tick
SERVICE
cat > /etc/systemd/system/nexus-dashboard-maintenance.timer << 'TIMER'
[Unit]
Description=Nexus Dashboard maintenance timer
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target
TIMER

# History sampler — ON BY DEFAULT (cheap; feeds trend sparklines + capacity
# forecast). Disk is hard-bounded in-app (rollups + auto_vacuum + size cap).
cat > /etc/systemd/system/nexus-dashboard-history.service << 'SERVICE'
[Unit]
Description=Nexus Dashboard metrics history sampler
[Service]
Type=oneshot
User=dashboard
Group=dashboard
WorkingDirectory=/opt/nexus-dashboard
Environment=DASHBOARD_UNIT_PREFIX=nexus-dashboard
ExecStart=/opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py history-tick
SERVICE
cat > /etc/systemd/system/nexus-dashboard-history.timer << 'TIMER'
[Unit]
Description=Nexus Dashboard metrics history timer
[Timer]
OnCalendar=*:0/5
Persistent=true
[Install]
WantedBy=timers.target
TIMER

info "Enabling and starting services..."
systemctl daemon-reload

# History sampler is on by default (the feature timers below stay opt-in and are
# enabled by the app when their feature is configured).
systemctl enable --now nexus-dashboard-history.timer 2>/dev/null || true

# Enable boot-time services
systemctl enable zfs.target 2>/dev/null || true
systemctl enable target 2>/dev/null || true
systemctl enable nfs-kernel-server 2>/dev/null || true
systemctl enable smbd 2>/dev/null || true

# Start services
systemctl start target 2>/dev/null || true
systemctl start nfs-kernel-server 2>/dev/null || true
systemctl start smbd 2>/dev/null || true

# Enable the dashboard to start on boot
systemctl enable nexus-dashboard.service

info "Configuring firewall..."
ufw allow $DASHBOARD_PORT/tcp comment 'Nexus Dashboard' 2>/dev/null || true
ufw allow 3260/tcp comment 'iSCSI Target' 2>/dev/null || true
ufw allow 2049/tcp comment 'NFS' 2>/dev/null || true
ufw allow 445/tcp comment 'SMB' 2>/dev/null || true
ufw allow 139/tcp comment 'SMB NetBIOS' 2>/dev/null || true
ufw allow 111/tcp comment 'NFS RPC' 2>/dev/null || true
ufw allow 8200/tcp comment 'DLNA' 2>/dev/null || true
ufw allow 1900/udp comment 'DLNA SSDP' 2>/dev/null || true

info "Installation complete!"
echo ""
echo "Starting dashboard..."
systemctl start nexus-dashboard.service || true

echo ""
echo "=== Summary ==="
echo "Dashboard URL:    https://$(hostname -I | awk '{print $1}'):$DASHBOARD_PORT"
echo "                  (self-signed cert by default - your browser will warn once;"
echo "                   install your own cert via the Settings page or DASHBOARD_TLS_CERT)"
echo "Log file:         /var/log/nexus-dashboard/app.log"
echo "Status command:   sudo systemctl status nexus-dashboard.service"
echo ""
echo "Login:            On first start an 'admin' account is created with a"
echo "                  random password, printed to the log file above."
echo "                  Retrieve it with:"
echo "                    sudo grep -A2 'initial admin account' /var/log/nexus-dashboard/app.log"
echo "                  Change it from the UI, or:"
echo "                    sudo -u $DASHBOARD_USER $DASHBOARD_DIR/venv/bin/python $DASHBOARD_DIR/app.py set-password admin"
echo ""
echo "Services managed by this dashboard:"
echo "  - ZFS Storage Pools (zfsutils-linux)"
echo "  - iSCSI Targets (LIO/targetcli-fb)"
echo "  - NFS Exports (nfs-kernel-server)"
echo "  - SMB/CIFS Shares (samba)"
echo ""
echo "All services are configured to start on boot."
