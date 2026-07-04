"""Extracted verbatim from NexusStationDashboard app.py (Stage 1 split).
Routes converted @app.route -> @bp.route; logic unchanged."""
import os
import re
import json
import time
import hmac
import socket
import hashlib
import secrets
import shutil
import threading
import subprocess
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, jsonify, request, session, g, Response
from werkzeug.security import generate_password_hash, check_password_hash
from .config import *
from .runcmd import run, run_safe, err, _size_to_bytes, _human_bytes, _num
from .validators import *
from .services import (SYSTEM_SERVICES, SERVICE_OVERRIDES, resolve_service,
                             _unit_present, RE_SERVICE, LLAMA_SERVICE, LLAMA_CONF,
                             LLAMA_MODELS_DIR, LLAMA_DEFAULT_BIN, LLAMA_URL)
from .registry import load_disabled_modules, MODULES, MODULE_IDS
from .auth import _is_admin, _hash_token, RE_USERNAME
from ..modules.disks import _smart_health_ok, disk_usage
from ..modules.iscsi import parse_targets, parse_backstores
from ..modules.nfs import parse_exports
from ..modules.smb import smbconf_parse
from .tasks import _task_alerts
from ..modules.lvm import _lvm_report
from ..modules.disks import _mdadm_conf_arrays, _zpool_disk_map

bp = Blueprint('summary', __name__)

@bp.route('/api/status')
def api_status():
    services = {}
    for key, svc in SYSTEM_SERVICES.items():
        r = run(['systemctl', 'is-active', svc['service']])
        e = run(['systemctl', 'is-enabled', svc['service']])
        services[key] = {
            'name': svc['name'],
            'active': r[0].strip() if r[0] else 'inactive',
            'enabled': e[0].strip() if e[0] else 'disabled',
            'installed': Path(svc['binary']).exists() or _unit_present(svc['service'])
        }
    return jsonify(services)

# Pool fill level (percent) at which a capacity alert fires.
ALERT_FULL_PCT = 90

# Pseudo / virtual / read-only filesystem types that are never "full" in a way
# worth alerting on (and zfs, which is covered by the dedicated pool alert).
ALERT_SKIP_FSTYPES = {
    'tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'iso9660', 'proc', 'sysfs',
    'cgroup', 'cgroup2', 'devpts', 'mqueue', 'debugfs', 'tracefs', 'fusectl',
    'configfs', 'pstore', 'bpf', 'autofs', 'ramfs', 'efivarfs', 'securityfs',
    'binfmt_misc', 'hugetlbfs', 'nsfs', 'zfs',
}


def _df_use_pct(blocks, bfree, bavail):
    """Filesystem use% the way df reports it (accounts for root-reserved blocks)."""
    used = blocks - bfree
    denom = used + bavail
    return round(used * 100 / denom) if denom > 0 else 0


def _real_mounts(proc_mounts_text):
    """[(mountpoint, fstype, options)] for real, non-pseudo filesystems."""
    out, seen = [], set()
    for line in proc_mounts_text.split('\n'):
        parts = line.split()
        if len(parts) < 4:
            continue
        _dev, mnt, fstype, opts = parts[0], parts[1], parts[2], parts[3]
        if fstype in ALERT_SKIP_FSTYPES or not mnt.startswith('/'):
            continue
        mnt = mnt.replace('\\040', ' ')   # /proc/mounts octal-escapes spaces
        if mnt in seen:
            continue
        seen.add(mnt)
        out.append((mnt, fstype, opts.split(',')))
    return out


def _fs_alerts():
    """Real filesystems at or above the fill threshold (covers LVM/plain mounts)."""
    try:
        with open('/proc/mounts') as f:
            text = f.read()
    except OSError:
        return []
    alerts = []
    for mnt, _fstype, opts in _real_mounts(text):
        if 'ro' in opts:               # read-only (e.g. snap, image) can't fill
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        if st.f_blocks <= 0:
            continue
        pct = _df_use_pct(st.f_blocks, st.f_bfree, st.f_bavail)
        if pct >= ALERT_FULL_PCT:
            alerts.append({'key': 'fs_full:' + mnt,
                           'message': f'Filesystem {mnt} is {pct}% full'})
    return alerts


def _lvm_alerts():
    """LVM volume groups with a missing PV (a failed/removed disk).

    Capacity is intentionally NOT measured here: a fully-allocated VG is the
    normal default (the Ubuntu installer assigns 100% of the VG to the root LV),
    so "VG % allocated" cries wolf. Running-out-of-space shows up as the
    filesystem filling, which `_fs_alerts` catches."""
    alerts = []
    for g in _lvm_report('vgs', 'vg_name,vg_missing_pv_count'):
        name = g.get('vg_name')
        if not name:
            continue
        try:
            missing = int(g.get('vg_missing_pv_count', 0) or 0)
        except (TypeError, ValueError):
            continue
        if missing > 0:
            alerts.append({'key': 'lvm_pv:' + name,
                           'message': f'LVM volume group {name} has {missing} missing PV(s)'})
    return alerts


def _parse_mdstat(text):
    """Parse /proc/mdstat into [{name, degraded}]. Degraded if the array has a
    failed/missing member ('_' in the [UU] map, fewer active than total, or (F))."""
    arrays, cur = [], None
    for line in text.split('\n'):
        m = re.match(r'^(md\d+)\s*:', line)
        if m:
            cur = {'name': m.group(1), 'degraded': '(F)' in line}
            arrays.append(cur)
        elif cur is not None:
            mm = re.search(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]', line)
            if mm:
                total, active = int(mm.group(1)), int(mm.group(2))
                if active < total or '_' in mm.group(3):
                    cur['degraded'] = True
    return arrays


def _md_alerts():
    """MD RAID arrays running degraded (failed/missing member disk)."""
    try:
        with open('/proc/mdstat') as f:
            text = f.read()
    except OSError:
        return []
    return [{'key': 'md_degraded:' + a['name'],
             'message': f"MD RAID array {a['name']} is degraded"}
            for a in _parse_mdstat(text) if a['degraded']]


def _compute_alerts():
    """The single source of truth for health alerts — used by both the dashboard
    summary and the background notifier. Returns [{key, message}] where `key` is
    stable per condition (so the notifier can de-duplicate)."""
    alerts = []
    disabled_modules = load_disabled_modules()
    for key, svc in SYSTEM_SERVICES.items():
        if not svc.get('alert', True):
            continue
        # A feature turned off on the Modules page is intentional — not an issue.
        if key in disabled_modules:
            continue
        active = (run(['systemctl', 'is-active', svc['service']])[0] or '').strip() or 'inactive'
        if active != 'active':
            # A unit intentionally disabled/masked at boot is also intentional.
            enabled = (run(['systemctl', 'is-enabled', svc['service']])[0] or '').strip()
            if enabled in ('disabled', 'masked'):
                continue
            alerts.append({'key': 'service:' + key, 'message': f"{svc['name']} service is {active}"})
    zout, _, zrc = run(['zpool', 'list', '-Hp', '-o', 'name,size,alloc,free,health'])
    if zrc == 0:
        for line in zout.strip().split('\n'):
            p = line.split('\t')
            if len(p) >= 5 and p[0]:
                if p[4] != 'ONLINE':
                    alerts.append({'key': 'zfs_health:' + p[0],
                                   'message': f"ZFS pool {p[0]} is {p[4]}"})
                size, alloc = int(p[1]), int(p[2])
                pctp = round(alloc / size * 100) if size else 0
                if pctp >= ALERT_FULL_PCT:
                    alerts.append({'key': 'zfs_full:' + p[0],
                                   'message': f"ZFS pool {p[0]} is {pctp}% full"})
    if _smart_health_ok() is False:
        alerts.append({'key': 'smart', 'message': 'A disk reports SMART failure'})
    # LVM and MD alerts follow their module toggles (off = intentional).
    if 'lvm' not in disabled_modules:
        alerts.extend(_lvm_alerts())
    if 'mdraid' not in disabled_modules:
        alerts.extend(_md_alerts())
    # Filesystem-full is a general operational risk — always checked.
    alerts.extend(_fs_alerts())
    # A scheduled task whose last run failed.
    alerts.extend(_task_alerts())
    # Module-contributed alerts (registry hooks; enabled modules only). The
    # subsystem checks above are the legacy inline aggregation — NEW modules
    # contribute here instead.
    from . import registry as _registry
    for _mid, _hook in _registry.module_hooks('alerts'):
        try:
            alerts.extend(_hook() or [])
        except Exception:
            pass  # a broken module hook must never take down alerting
    return alerts


def _primary_ipv4():
    """The host's primary LAN IPv4 — the source address the kernel uses for
    egress. This deliberately picks the default-route interface so it ignores
    docker0/bridges/veth/VPN interfaces (which otherwise win when we just take the
    last non-loopback address — e.g. a Docker host reporting 172.17.0.1). No
    packets are sent: a UDP connect() only resolves the route locally."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1', 9))       # TEST-NET-1 placeholder (unrouted)
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and ip != '0.0.0.0' and not ip.startswith('127.'):
            return ip
    except OSError:
        pass
    # Fallback: the inet address on the default-route interface.
    try:
        routes = json.loads(run(['ip', '-j', 'route', 'show', 'default'])[0] or '[]')
        dev = routes[0].get('dev') if routes else None
        if dev:
            for itf in json.loads(run(['ip', '-j', 'addr', 'show', dev])[0] or '[]'):
                for a in itf.get('addr_info', []):
                    if a.get('family') == 'inet' and a.get('local'):
                        return a['local']
    except (json.JSONDecodeError, IndexError, AttributeError, KeyError):
        pass
    # Last resort: first non-loopback, non-virtual interface address.
    try:
        for itf in json.loads(run(['ip', '-j', 'addr', 'show'])[0] or '[]'):
            name = itf.get('ifname', '')
            if name == 'lo' or name.startswith(('docker', 'br-', 'veth', 'virbr', 'tap', 'tun')):
                continue
            for a in itf.get('addr_info', []):
                if a.get('family') == 'inet' and a.get('local') != '127.0.0.1':
                    return a['local']
    except json.JSONDecodeError:
        pass
    return '-'


@bp.route('/api/summary')
def api_summary():
    """Aggregated overview for the dashboard front page (one call)."""
    services = {}
    # A module turned off on the Modules page hides its card and suppresses its
    # alerts; hide its service line on the front page too (the dedicated Services
    # page still lists everything for management). Service keys are module ids.
    disabled = load_disabled_modules()
    for key, svc in SYSTEM_SERVICES.items():
        if key in disabled:
            continue
        active = (run(['systemctl', 'is-active', svc['service']])[0] or '').strip() or 'inactive'
        enabled = (run(['systemctl', 'is-enabled', svc['service']])[0] or '').strip() or 'disabled'
        services[key] = {'name': svc['name'], 'active': active, 'enabled': enabled}

    # System
    try:
        with open('/proc/uptime') as f:
            uptime_days = round(float(f.read().split()[0]) / 86400, 1)
    except (OSError, ValueError):
        uptime_days = 0
    system = {'hostname': socket.gethostname(), 'uptime_days': uptime_days,
              'ip': _primary_ipv4()}

    # ZFS
    pools = size = alloc = 0
    online = True
    zout, _, zrc = run(['zpool', 'list', '-Hp', '-o', 'name,size,alloc,free,health'])
    if zrc == 0:
        for line in zout.strip().split('\n'):
            p = line.split('\t')
            if len(p) >= 5 and p[0]:
                pools += 1
                size += int(p[1]); alloc += int(p[2])
                if p[4] != 'ONLINE':
                    online = False
    pct = round(alloc / size * 100) if size else 0
    scanning = 'in progress' in (run(['zpool', 'status'])[0] or '')
    zfs = {'pools': pools, 'online': online, 'used': _human_bytes(alloc),
           'size': _human_bytes(size), 'pct': pct, 'scanning': scanning}

    # iSCSI
    iout = run(['targetcli', '/iscsi', 'ls'])[0] or ''
    bs = parse_backstores(run(['targetcli', '/backstores', 'ls'])[0] or '')
    sess = [l for l in (run([HELPER_PREFIX + '-iscsi-sessions'])[0] or '').split('\n') if l.strip()]
    iscsi = {
        'targets': len(parse_targets(iout)),
        'luns': sum(int(x) for x in re.findall(r'\[LUNs: (\d+)\]', iout)),
        'backstores': len(bs),
        'provisioned': _human_bytes(sum(_size_to_bytes(b.get('size', '')) for b in bs)),
        'sessions': len(sess),
    }

    # NFS
    mounts = [l for l in (run(['showmount', '-a', '--no-headers'], no_sudo=True)[0] or '').split('\n') if l.strip()]
    nfs = {'exports': len(parse_exports()), 'clients': len(mounts)}

    # SMB
    users = [l for l in (run(['pdbedit', '-L'])[0] or '').split('\n') if l.strip()]
    conns = [l for l in (run(['smbstatus', '-b'])[0] or '').split('\n') if re.match(r'^\d+\s', l.strip())]
    share_count = len([n for n in smbconf_parse() if n.lower() not in ('global', 'homes')])
    smb = {'shares': share_count, 'users': len(users), 'connections': len(conns)}

    # Disks
    total = free = 0
    try:
        defined_md, pmap = _mdadm_conf_arrays(), _zpool_disk_map()
        for d in json.loads(run(['lsblk', '-J', '-o', 'NAME,TYPE,FSTYPE,MOUNTPOINT'])[0] or '{}').get('blockdevices', []):
            if (d.get('type') or '') == 'disk':
                total += 1
                if disk_usage(d, pmap, defined_md) == 'Free':
                    free += 1
    except json.JSONDecodeError:
        pass
    smart_ok = _smart_health_ok()
    disks = {'total': total, 'free': free, 'smart_ok': smart_ok}

    alerts = [a['message'] for a in _compute_alerts()]
    payload = {'system': system, 'services': services, 'zfs': zfs, 'iscsi': iscsi,
               'nfs': nfs, 'smb': smb, 'disks': disks, 'alerts': alerts}
    # Module-contributed summary blocks (registry hooks; enabled modules only).
    # Legacy subsystems above stay inline; NEW modules add their card data here
    # under their module id.
    from . import registry as _registry
    for _mid, _hook in _registry.module_hooks('summary'):
        try:
            block = _hook()
            if block is not None:
                payload[_mid] = block
        except Exception:
            pass  # a broken module hook must never take down the dashboard
    return jsonify(payload)


# ─── System resources (CPU / memory / load / uptime) ──────────────────
# All read from /proc — no sudo, no external tools. The parsers below are pure
# (text in, numbers out) so they are unit-tested.

def _parse_meminfo(text):
    """/proc/meminfo -> {key: bytes}. meminfo reports kB; convert to bytes."""
    out = {}
    for line in text.split('\n'):
        m = re.match(r'^(\w+):\s+(\d+)(?:\s+kB)?', line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def _parse_loadavg(text):
    """/proc/loadavg -> (load1, load5, load15) as floats."""
    parts = text.split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return 0.0, 0.0, 0.0


def _parse_cpu_stat(text):
    """First 'cpu ' aggregate line of /proc/stat -> (idle_jiffies, total_jiffies).
    idle counts idle+iowait."""
    for line in text.split('\n'):
        if line.startswith('cpu '):
            vals = [int(x) for x in line.split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return idle, sum(vals)
    return 0, 0


def _cpu_percent(prev, cur):
    """Busy % between two (idle, total) /proc/stat samples."""
    didle = cur[0] - prev[0]
    dtotal = cur[1] - prev[1]
    if dtotal <= 0:
        return 0.0
    return round((1 - didle / dtotal) * 100, 1)


def _cpu_usage():
    try:
        with open('/proc/stat') as f:
            a = _parse_cpu_stat(f.read())
        time.sleep(0.1)
        with open('/proc/stat') as f:
            b = _parse_cpu_stat(f.read())
        return _cpu_percent(a, b)
    except OSError:
        return 0.0


def _system_resources():
    try:
        with open('/proc/uptime') as f:
            uptime = int(float(f.read().split()[0]))
    except (OSError, ValueError):
        uptime = 0
    try:
        with open('/proc/loadavg') as f:
            l1, l5, l15 = _parse_loadavg(f.read())
    except OSError:
        l1 = l5 = l15 = 0.0
    try:
        with open('/proc/meminfo') as f:
            mem = _parse_meminfo(f.read())
    except OSError:
        mem = {}
    total = mem.get('MemTotal', 0)
    avail = mem.get('MemAvailable', 0)
    swap_total = mem.get('SwapTotal', 0)
    swap_free = mem.get('SwapFree', 0)
    return {
        'uptime_seconds': uptime,
        'load': {'1': l1, '5': l5, '15': l15},
        'cpus': os.cpu_count() or 1,
        'cpu_pct': _cpu_usage(),
        'memory': {'total': total, 'available': avail, 'used': max(0, total - avail),
                   'pct': round((total - avail) / total * 100, 1) if total else 0},
        'swap': {'total': swap_total, 'used': max(0, swap_total - swap_free),
                 'pct': round((swap_total - swap_free) / swap_total * 100, 1) if swap_total else 0},
    }


@bp.route('/api/system/resources')
def system_resources():
    return jsonify(_system_resources())


