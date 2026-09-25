"""Diagnostics — every prerequisite an ENABLED module needs on this node, with
pass/fail and the fix. System > Diagnostics (admin-only page).

The fleet's recurring by-hand probing, turned into one page: a root helper
missing under this node's prefix, a sudoers grant that never landed, the
service user added to `docker`/`render`/`models`/`lxd` but the process not
restarted since (supplementary groups are read at exec — the single most
common "it should work" trap here), a timer that is not running, a backup
mount that is a bare directory, the dnsmasq drop-in absent, a certificate
about to expire, a state directory the service user cannot write.

Each check is {module, id, name, state, detail, fix}. `state` is 'ok',
'warn' (works, but something is off or will be), 'fail' (the feature is
broken until fixed) or 'skip' (not applicable here — e.g. sudo grants when
the process already runs as root). Checks never raise: a probe that blows
up is itself a 'warn' with the exception text.
"""
import os
import grp
import pwd
import time
import shutil
import datetime
from flask import Blueprint, jsonify

from .config import APP_DIR, UNIT_PREFIX, HELPER_PREFIX, TLS_CERT
from .runcmd import run, err
from .auth import _is_admin
from .registry import load_disabled_modules, MODULE_IDS
from .services import SYSTEM_SERVICES
from . import tls as _tls

bp = Blueprint('diagnostics', __name__)

# ─── The catalog ──────────────────────────────────────────────────────
# helper: /usr/local/sbin/<prefix>-<name> must exist and be executable.
HELPERS = {
    'disks': ['locate-read', 'mount'], 'iscsi': ['iscsi-sessions'], 'schedules': ['snap-fs'],
    'caddy': ['caddy'], 'updates': ['updates'], 'gpu': ['gpu-tune'],
    'nut': ['nut'], 'upsmon': ['nut'], 'minidlna': ['dlna-rescan', 'dlna-stats'],
    'network': ['netplan'],                    # core page, always on
}
# Supplementary groups the service user needs (any one of a tuple suffices).
GROUPS = {
    'docker': [('docker',)], 'compose': [('docker',)], 'halogen': [('docker',)],
    'instances': [('lxd', 'incus-admin')], 'images': [('lxd', 'incus-admin')],
    'ctnetworks': [('lxd', 'incus-admin')], 'portforward': [('lxd', 'incus-admin')],
    'gpu': [('render',)], 'llamacpp': [('models',)],
}
# Binaries a module's sudoers grant must cover (basename match against
# `sudo -n -l`). Modules whose grant is the blanket systemctl are omitted.
SUDO_BINARIES = {
    'zfs': ['zpool', 'zfs'], 'disks': ['smartctl', 'lsblk', 'wipefs'], 'lvm': ['lvs', 'vgs', 'pvs', 'lvextend'],
    'mdraid': ['mdadm'], 'iscsi': ['targetcli'], 'nfs': ['exportfs'], 'smb': ['testparm', 'smbpasswd', 'smbstatus', 'net'],
    'firewall': ['ufw'], 'dnsmasq': ['dhcp-probe'],
}
# Timers keyed by the module that needs them (core ones under 'core'). The
# history timer must always run; the others are only enabled once something
# is scheduled (the modules sync them), so an inactive one is a 'skip' when
# there is nothing for it to do — not a warning.
TIMERS = {                       # unit = <prefix>-<name>.timer, names as install.sh writes them
    'core': ['history', 'alerts'], 'schedules': ['autosnap'], 'replication': ['replicate'],
    'maintenance': ['maintenance'],
}


def _timer_wanted(name):
    """(wanted, why_not) — whether anything is configured for this timer."""
    try:
        if name == 'history':
            return True, ''
        if name == 'alerts':
            from . import alerts as _al
            return bool(_al._notifications_enabled(_al.load_notifications())), 'no notification target configured'
        if name == 'autosnap':
            from ..modules import schedules as _s
            return bool((_s.load_schedules() or {}).get('schedules')), 'no snapshot schedule configured'
        if name == 'replicate':
            from ..modules import replication as _r
            return bool((_r.load_replication() or {}).get('jobs')), 'no replication job configured'
        if name == 'maintenance':
            from ..modules import maintenance as _m
            cfg = _m.load_maintenance() or {}
            return bool(cfg.get('scrubs') or cfg.get('smart')), 'nothing scheduled'
    except Exception as e:
        return True, 'could not read config: %s' % e
    return True, ''
CERT_WARN_DAYS = 30

# Pointer text for the two ways a helper/grant gets onto a node.
FIX_HELPERS = ('run the installer with --helpers-only on this node, or '
               '`deploy/fleet-deploy.sh --helpers -n <node>` (legacy-prefixed nodes by hand)')


def _c(module, cid, name, state, detail='', fix=''):
    return {'module': module, 'id': cid, 'name': name, 'state': state, 'detail': detail, 'fix': fix}


def _service_user():
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def _etc_group_members(user):
    """Groups /etc/group says the user belongs to (what a RESTART would give)."""
    out = set()
    try:
        for g in grp.getgrall():
            if user in g.gr_mem:
                out.add(g.gr_name)
        out.add(grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
    except (KeyError, OSError):
        pass
    return out


def _process_groups():
    """Groups the running process actually has (fixed at exec)."""
    out = set()
    for gid in os.getgroups():
        try:
            out.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return out


def _sudo_grants():
    """Set of command basenames `sudo -n -l` lists, or None if sudo itself
    refuses (no grant at all / no sudo)."""
    out, _, rc = run(['sudo', '-n', '-l'], no_sudo=True, timeout=15)
    if rc != 0:
        return None
    names = set()
    for line in (out or '').splitlines():
        line = line.strip()
        if 'NOPASSWD' not in line and not line.startswith('/'):
            continue
        cmds = line.split(':', 1)[1] if ':' in line else line
        for c in cmds.split(','):
            path = c.strip().split(' ')[0]
            if path.startswith('/'):
                names.add(os.path.basename(path))
    return names


def _unit_state(unit):
    out, _, _ = run(['systemctl', 'is-active', unit], no_sudo=True, timeout=10)
    return (out or '').strip() or 'unknown'


def _cert_days_left(cert_path=TLS_CERT):
    info = _tls.cert_info(cert_path)
    exp = info.get('expires')
    if not exp:
        return None, info
    for fmt in ('%b %d %H:%M:%S %Y %Z', '%b %d %H:%M:%S %Y'):
        try:
            dt = datetime.datetime.strptime(exp.replace('  ', ' '), fmt)
            return (dt - datetime.datetime.utcnow()).days, info
        except ValueError:
            continue
    return None, info


# ─── Checks ───────────────────────────────────────────────────────────

def run_checks(enabled=None, euid=None):
    enabled = set(MODULE_IDS) - set(load_disabled_modules()) if enabled is None else set(enabled)
    euid = os.geteuid() if euid is None else euid
    user = _service_user()
    checks = []

    # State dir: every JSON write goes through write_json_atomic next to app.py.
    checks.append(_c('core', 'state_dir', 'State directory writable',
                     'ok' if os.access(APP_DIR, os.W_OK) else 'fail', APP_DIR,
                     '' if os.access(APP_DIR, os.W_OK) else 'chown the app directory to the service user (%s)' % user))

    # Certificate.
    try:
        days, info = _cert_days_left()
        if not info.get('present'):
            checks.append(_c('core', 'cert', 'TLS certificate', 'warn', 'no certificate file', 'the app generates one at start; check TLS settings'))
        elif days is None:
            checks.append(_c('core', 'cert', 'TLS certificate', 'warn', 'expiry unreadable: %s' % info.get('expires')))
        else:
            st = 'fail' if days < 0 else 'warn' if days < CERT_WARN_DAYS else 'ok'
            checks.append(_c('core', 'cert', 'TLS certificate', st,
                             '%s, %s' % ('expired %d days ago' % -days if days < 0 else '%d days left' % days,
                                         'self-signed' if info.get('self_signed') else info.get('issuer', '')),
                             'renew on the Certificate page (or re-upload the wildcard pair)' if st != 'ok' else ''))
    except Exception as e:
        checks.append(_c('core', 'cert', 'TLS certificate', 'warn', 'probe failed: %s' % e))

    # Timers.
    for mod, names in TIMERS.items():
        if mod != 'core' and mod not in enabled:
            continue
        for n in names:
            unit = '%s-%s.timer' % (UNIT_PREFIX, n)
            st = _unit_state(unit)
            wanted, why = _timer_wanted(n)
            if st != 'active' and not wanted:
                checks.append(_c(mod, 'timer:' + n, 'Timer %s' % unit, 'skip', '%s (%s)' % (st, why)))
                continue
            checks.append(_c(mod, 'timer:' + n, 'Timer %s' % unit,
                             'ok' if st == 'active' else 'warn', st,
                             'systemctl enable --now %s' % unit if st != 'active' else ''))

    # Helpers.
    for mod, names in HELPERS.items():
        if mod != 'network' and mod not in enabled:
            continue
        for n in names:
            path = '%s-%s' % (HELPER_PREFIX, n)
            ok = os.path.isfile(path) and os.access(path, os.X_OK)
            checks.append(_c(mod, 'helper:' + n, 'Root helper %s' % os.path.basename(path),
                             'ok' if ok else 'fail', path if ok else 'missing: ' + path,
                             '' if ok else FIX_HELPERS))

    # Groups: present in the process, or only in /etc/group (restart needed), or absent.
    proc_groups, etc_groups = _process_groups(), _etc_group_members(user)
    for mod, alts in GROUPS.items():
        if mod not in enabled:
            continue
        for alt in alts:
            if euid == 0:
                checks.append(_c(mod, 'group:' + alt[0], 'Group %s' % '/'.join(alt), 'skip', 'process runs as root'))
                continue
            in_proc = any(g in proc_groups for g in alt)
            in_etc = any(g in etc_groups for g in alt)
            exists = any(True for g in alt if _group_exists(g))
            if in_proc:
                checks.append(_c(mod, 'group:' + alt[0], 'Group %s' % '/'.join(alt), 'ok', 'member (effective)'))
            elif in_etc:
                checks.append(_c(mod, 'group:' + alt[0], 'Group %s' % '/'.join(alt), 'warn',
                                 'added in /etc/group but not effective in the running process',
                                 'systemctl restart %s (supplementary groups are read at start)' % UNIT_PREFIX))
            elif not exists:
                checks.append(_c(mod, 'group:' + alt[0], 'Group %s' % '/'.join(alt), 'warn',
                                 'no such group on this host', 'install the software that creates it (or ignore if unused)'))
            else:
                checks.append(_c(mod, 'group:' + alt[0], 'Group %s' % '/'.join(alt), 'fail', '%s is not a member' % user,
                                 'usermod -aG %s %s && systemctl restart %s' % (alt[0], user, UNIT_PREFIX)))

    # Sudo grants.
    if euid == 0:
        checks.append(_c('core', 'sudo', 'Sudo grants', 'skip', 'process runs as root'))
    else:
        try:
            grants = _sudo_grants()
        except Exception:
            grants = None
        if grants is None:
            checks.append(_c('core', 'sudo', 'Sudo grants', 'fail', 'sudo -n -l refused — no sudoers file for %s' % user,
                             'reinstall the sudoers file: ' + FIX_HELPERS))
        else:
            has_systemctl = 'systemctl' in grants
            checks.append(_c('core', 'sudo', 'Sudo grants', 'ok' if has_systemctl else 'fail',
                             '%d commands granted' % len(grants) if has_systemctl else 'systemctl is not granted',
                             '' if has_systemctl else 'reinstall the sudoers file: ' + FIX_HELPERS))
            for mod, bins in SUDO_BINARIES.items():
                if mod not in enabled:
                    continue
                missing = [b for b in bins if b not in grants and not any(g.startswith(b) for g in grants)]
                checks.append(_c(mod, 'sudo:' + mod, 'Sudo grant for %s' % ', '.join(bins),
                                 'ok' if not missing else 'fail',
                                 'granted' if not missing else 'missing: ' + ', '.join(missing),
                                 '' if not missing else 'reinstall the sudoers file: ' + FIX_HELPERS))

    # Binaries behind each enabled module's service (the Services page nags too).
    for key, svc in SYSTEM_SERVICES.items():
        if key not in enabled or not svc.get('binary'):
            continue
        present = os.path.exists(svc['binary']) or bool(shutil.which(os.path.basename(svc['binary'])))
        checks.append(_c(key, 'binary', '%s installed' % svc.get('name', key), 'ok' if present else 'warn',
                         svc['binary'] if present else 'not found: ' + svc['binary'],
                         '' if present else 'install %s, or disable the module on this node' % (svc.get('pkg') or svc.get('name', key))))

    # Module-specific probes, each guarded.
    for mod, fn in _MODULE_PROBES.items():
        if mod not in enabled:
            continue
        try:
            checks.extend(fn())
        except Exception as e:
            checks.append(_c(mod, 'probe', '%s probe' % mod, 'warn', 'probe failed: %s' % e))

    counts = {'ok': 0, 'warn': 0, 'fail': 0, 'skip': 0}
    for c in checks:
        counts[c['state']] = counts.get(c['state'], 0) + 1
    return {'user': user, 'euid': euid, 'unit_prefix': UNIT_PREFIX, 'helper_prefix': HELPER_PREFIX,
            'checked_at': int(time.time()), 'counts': counts, 'checks': checks}


def _group_exists(name):
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _probe_dnsmasq():
    from ..modules import dnsmasq
    st = dnsmasq.module_status()
    out = []
    if not st.get('installed'):
        out.append(_c('dnsmasq', 'dnsmasq:installed', 'dnsmasq present', 'warn', 'not installed', 'apt install dnsmasq'))
        return out
    out.append(_c('dnsmasq', 'dnsmasq:dropin', 'conf-dir drop-in', 'ok' if st.get('dropin_present') else 'fail',
                  'present' if st.get('dropin_present') else 'missing /etc/dnsmasq.d/zz-<prefix>.conf',
                  '' if st.get('dropin_present') else 'create the drop-in pointing conf-dir at %s (installer does this)' % st.get('render_dir')))
    return out


def _probe_llama_backup():
    from ..modules import llama
    base = llama._backup_base()
    if llama._is_mountpoint(base):
        return [_c('llamacpp', 'llama:backup_mount', 'Model backup target %s' % base, 'ok', 'mount point')]
    if os.path.isdir(base):
        return [_c('llamacpp', 'llama:backup_mount', 'Model backup target %s' % base, 'warn',
                   'exists but is NOT a mount point — a backup would fill the root disk',
                   'mount the NFS export there (fstab), or set backup_allow_local deliberately')]
    return [_c('llamacpp', 'llama:backup_mount', 'Model backup target %s' % base, 'warn', 'directory absent',
               'mount the NFS export at %s' % base)]


def _probe_docker_socket():
    from ..modules import docker as dk
    sock = dk.DOCKER_SOCKET
    if not os.path.exists(sock):
        return [_c('docker', 'docker:socket', 'Docker socket', 'warn', 'no socket at %s' % sock, 'install docker, or disable the module')]
    ok = os.access(sock, os.R_OK | os.W_OK)
    return [_c('docker', 'docker:socket', 'Docker socket', 'ok' if ok else 'fail',
               sock if ok else 'no read/write access to %s' % sock,
               '' if ok else 'add the service user to the docker group and restart the unit')]


def _probe_halogen():
    from ..modules import halogen as hg
    wd = hg._stack_dir()
    if not wd:
        return [_c('halogen', 'halogen:stack', 'Halogen stack directory', 'warn', 'unknown — never seen running here',
                   'bring the stack up once (or set DASHBOARD_HALOGEN_DIR)')]
    ok = os.path.isdir(wd) and bool(hg._stack_config_files(wd))
    return [_c('halogen', 'halogen:stack', 'Halogen stack directory', 'ok' if ok else 'fail', wd,
               '' if ok else 'compose file missing under %s' % wd)]


_MODULE_PROBES = {'dnsmasq': _probe_dnsmasq, 'llamacpp': _probe_llama_backup,
                  'docker': _probe_docker_socket, 'halogen': _probe_halogen}


@bp.route('/api/diagnostics')
def api_diagnostics():
    if not _is_admin():                      # it lists sudo grants and helper paths
        return err('Admin required', 403)
    return jsonify(run_checks())
