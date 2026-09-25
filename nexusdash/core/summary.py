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
from ..modules.zfs import pool_space, _pct

bp = Blueprint('summary', __name__)

@bp.route('/api/status')
def api_status():
    services = {}
    # Additive flag only — entries are never omitted (the NexusController and
    # fleet automation poll this endpoint); the Services page hides flagged rows.
    disabled = load_disabled_modules()
    for key, svc in SYSTEM_SERVICES.items():
        r = run(['systemctl', 'is-active', svc['service']])
        e = run(['systemctl', 'is-enabled', svc['service']])
        services[key] = {
            'name': svc['name'],
            'active': r[0].strip() if r[0] else 'inactive',
            'enabled': e[0].strip() if e[0] else 'disabled',
            'installed': Path(svc['binary']).exists() or _unit_present(svc['service']),
            'module_disabled': key in disabled,
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


def _zpool_health_message(pool, health):
    """A SUSPENDED pool is not merely unhealthy — with failmode=wait every I/O
    to it blocks, so from outside it looks hung rather than broken. Say what it
    means and the only way out, since the key is the same one DEGRADED uses."""
    if health == 'SUSPENDED':
        return (f"ZFS pool {pool} is SUSPENDED — all I/O is blocked; reconnect the "
                f"missing device, then clear the pool")
    return f"ZFS pool {pool} is {health}"


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
    # Usable space (zfs list), not zpool's raw parity-inclusive columns — a
    # thick-provisioned pool can be 57% committed while zpool says 43%.
    for pname, sp in (pool_space() or {}).items():
        if sp['health'] != 'ONLINE':
            alerts.append({'key': 'zfs_health:' + pname,
                           'message': _zpool_health_message(pname, sp['health'])})
        pctp = _pct(sp['alloc'], sp['size'])
        if pctp >= ALERT_FULL_PCT:
            alerts.append({'key': 'zfs_full:' + pname,
                           'message': f"ZFS pool {pname} is {pctp}% full"})
    if _smart_health_ok() is False:
        alerts.append({'key': 'smart', 'message': 'A disk reports SMART failure'})
    alerts.extend(_temp_alerts())
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
              'ip': _primary_ipv4(), 'os': PLATFORM['pretty']}

    # ZFS
    pools = size = alloc = 0
    online = True
    # USABLE bytes (root-dataset used+avail), not zpool's raw columns — see
    # zfs.pool_space. The NexusController parses `used`/`size` off this block.
    for sp in (pool_space() or {}).values():
        pools += 1
        size += sp['size']; alloc += sp['alloc']
        if sp['health'] != 'ONLINE':
            online = False
    pct = _pct(alloc, size)
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


# ─── Disk + network throughput (/proc counters, rates by delta) ───────
# Whole disks only — partitions, loop, zram, dm and the like would double
# count. Net: physical/bond/bridge uplinks, not the per-container plumbing.
RE_IO_DISK = re.compile(r'^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|md\d+|mmcblk\d+)\Z')
RE_IO_NET_SKIP = re.compile(r'^(lo|veth.*|docker.*|br-.*|virbr.*|lxdbr.*|lxcbr.*|tap.*|vnet.*|cni.*|flannel.*)\Z')
DISKSTATS = os.environ.get('DASHBOARD_DISKSTATS', '/proc/diskstats')
NETDEV = os.environ.get('DASHBOARD_NETDEV', '/proc/net/dev')
SECTOR = 512                                   # /proc/diskstats sectors are always 512 B


def _parse_diskstats(text):
    """{dev: (read_bytes, write_bytes)} for whole disks. Pure."""
    out = {}
    for line in (text or '').splitlines():
        p = line.split()
        if len(p) >= 14 and RE_IO_DISK.match(p[2]):
            try:
                out[p[2]] = (int(p[5]) * SECTOR, int(p[9]) * SECTOR)
            except ValueError:
                pass
    return out


def _parse_netdev(text):
    """{iface: (rx_bytes, tx_bytes)}. Pure."""
    out = {}
    for line in (text or '').splitlines():
        if ':' not in line:
            continue
        name, rest = line.split(':', 1)
        name = name.strip()
        p = rest.split()
        if RE_IO_NET_SKIP.match(name) or len(p) < 16:
            continue
        try:
            out[name] = (int(p[0]), int(p[8]))
        except ValueError:
            pass
    return out


def _io_counters():
    def rd(path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return ''
    return {'ts': time.time(), 'disks': _parse_diskstats(rd(DISKSTATS)), 'net': _parse_netdev(rd(NETDEV))}


def _io_rates(prev, cur):
    """Bytes/second between two counter snapshots, per device and in total.
    A counter that went backwards (reboot, hot-swap) is skipped. Pure."""
    if not prev or not cur:
        return None
    dt = cur['ts'] - prev['ts']
    if dt <= 0:
        return None
    out = {'interval_s': round(dt, 1), 'disks': {}, 'net': {}, 'disk_total': {'read_bps': 0, 'write_bps': 0},
           'net_total': {'rx_bps': 0, 'tx_bps': 0}}
    for kind, a, b, tot in (('disks', 'read_bps', 'write_bps', 'disk_total'), ('net', 'rx_bps', 'tx_bps', 'net_total')):
        for name, (c0, c1) in cur[kind].items():
            p = prev[kind].get(name)
            if not p or c0 < p[0] or c1 < p[1]:
                continue
            r0, r1 = int((c0 - p[0]) / dt), int((c1 - p[1]) / dt)
            out[kind][name] = {a: r0, b: r1}
            out[tot][a] += r0
            out[tot][b] += r1
    return out


_IO_PREV = {}      # in-process previous snapshot for the 30 s /api/system/resources poll
IO_MIN_INTERVAL = 5.0   # the dashboard polls twice within a second (machine strip + panel)


def _io_rates_live():
    """Rates since the last snapshot that is at least IO_MIN_INTERVAL old —
    a burst of polls in the same second must not yield a 0.2 s rate."""
    cur = _io_counters()
    prev = _IO_PREV.get('snap')
    if prev is None:
        _IO_PREV['snap'] = cur
        return None
    dt = cur['ts'] - prev['ts']
    if dt < 1.0:
        return None
    rates = _io_rates(prev, cur)
    if dt >= IO_MIN_INTERVAL:
        _IO_PREV['snap'] = cur
    return rates


# ─── Mount usage: one box per real, persistent filesystem ─────────────
# ALLOWLIST, not a skip list: the question is "where can data live", so
# only on-disk and network filesystems count. No tmpfs/overlay/squashfs
# (snaps)/loop images/pseudo. ZFS collapses to ONE box per pool (every
# dataset would otherwise repeat the pool's free space), fed by the usable
# figures from pool_space(). Bind mounts of the same device show once.
PERSISTENT_FSTYPES = {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'f2fs', 'jfs', 'reiserfs', 'vfat', 'exfat', 'ntfs', 'ntfs3'}
REMOTE_FSTYPES = {'nfs', 'nfs4', 'cifs', 'smb3', 'fuse.sshfs', 'ceph', 'glusterfs'}
MOUNT_SKIP_PREFIXES = ('/snap/', '/var/lib/docker/', '/var/lib/containers/', '/run/', '/proc/', '/sys/', '/dev/')
REMOTE_STATVFS_TIMEOUT = 2.0        # a hard NFS mount with its server gone would hang us


def _parse_mount_table(text):
    """[(source, mountpoint, fstype, opts)] from /proc/mounts text. Pure."""
    out = []
    for line in (text or '').splitlines():
        p = line.split()
        if len(p) >= 4 and p[1].startswith('/'):
            out.append((p[0], p[1].replace('\\040', ' '), p[2], p[3].split(',')))
    return out


def _select_mounts(table):
    """Which rows deserve a box: [(source, mount, fstype, kind)] with kind
    'zfs' (one per pool, source = pool name), 'disk' or 'remote'. Pure."""
    out, seen_dev, pool_idx = [], set(), {}
    for src, mnt, fstype, opts in table:
        if mnt.startswith(MOUNT_SKIP_PREFIXES) or 'ro' in opts:
            continue
        if fstype == 'zfs':
            pool = src.split('/', 1)[0]
            root_mount = mnt if '/' not in src else None
            if pool not in pool_idx:
                pool_idx[pool] = len(out)
                out.append((pool, root_mount, 'zfs', 'zfs'))
            elif root_mount:                     # root dataset seen after a child: prefer its mount
                out[pool_idx[pool]] = (pool, root_mount, 'zfs', 'zfs')
            continue
        if fstype in REMOTE_FSTYPES:
            kind = 'remote'
        elif fstype in PERSISTENT_FSTYPES:
            kind = 'disk'
        else:
            continue
        if src.startswith('/dev/loop') or src in seen_dev:
            continue
        seen_dev.add(src)
        out.append((src, mnt, fstype, kind))
    return out


def _statvfs_usage(path, timeout=None):
    """(total, used, avail, pct) via statvfs; None on error/timeout."""
    result = {}

    def go():
        try:
            st = os.statvfs(path)
            result['st'] = st
        except OSError:
            pass
    if timeout:
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(timeout)
    else:
        go()
    st = result.get('st')
    if not st or st.f_blocks <= 0:
        return None
    total, free, avail = st.f_blocks * st.f_frsize, st.f_bfree * st.f_frsize, st.f_bavail * st.f_frsize
    return total, total - free, avail, _df_use_pct(st.f_blocks, st.f_bfree, st.f_bavail)


def _mount_usage(mounts_text=None, pools=None):
    """[{mount, source, fstype, kind, total, used, avail, pct}] for the
    dashboard's Filesystems row. pools = pool_space() (injected in tests)."""
    if mounts_text is None:
        try:
            with open('/proc/mounts') as f:
                mounts_text = f.read()
        except OSError:
            return []
    rows = []
    selected = _select_mounts(_parse_mount_table(mounts_text))
    if any(k == 'zfs' for *_r, k in selected) and pools is None:
        pools = pool_space() or {}
    for src, mnt, fstype, kind in selected:
        if kind == 'zfs':
            sp = (pools or {}).get(src)
            if not sp:
                continue
            rows.append({'mount': mnt or src, 'source': src, 'fstype': 'zfs', 'kind': 'zfs',
                         'total': sp['size'], 'used': sp['alloc'], 'avail': sp['free'],
                         'pct': _pct(sp['alloc'], sp['size'])})
            continue
        u = _statvfs_usage(mnt, REMOTE_STATVFS_TIMEOUT if kind == 'remote' else None)
        if not u:
            rows.append({'mount': mnt, 'source': src, 'fstype': fstype, 'kind': kind,
                         'total': None, 'used': None, 'avail': None, 'pct': None, 'error': 'unreachable'})
            continue
        total, used, avail, pct = u
        rows.append({'mount': mnt, 'source': src, 'fstype': fstype, 'kind': kind,
                     'total': total, 'used': used, 'avail': avail, 'pct': pct})
    return rows


# ─── Host temperatures (hwmon) ────────────────────────────────────────
HWMON_DIR = os.environ.get('DASHBOARD_HWMON_DIR', '/sys/class/hwmon')
CPU_CHIPS = ('coretemp', 'k10temp', 'zenpower', 'cpu_thermal', 'acpitz', 'soc_thermal')
TEMP_HIGH_C = {'cpu': 90, 'nvme': 70}


def _read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _host_temps(base=None):
    """[{chip, label, c}] from every hwmon temp*_input. Pure over sysfs;
    a box with no sensors returns []."""
    base = base or HWMON_DIR
    out = []
    try:
        chips = sorted(os.listdir(base))
    except OSError:
        return out
    for h in chips:
        d = os.path.join(base, h)
        chip = _read_sysfs(os.path.join(d, 'name')) or h
        try:
            files = sorted(f for f in os.listdir(d) if f.startswith('temp') and f.endswith('_input'))
        except OSError:
            continue
        for f in files:
            raw = _read_sysfs(os.path.join(d, f))
            if raw is None or _num(raw) is None:
                continue
            label = _read_sysfs(os.path.join(d, f.replace('_input', '_label'))) or f[:-6]
            out.append({'chip': chip, 'label': label, 'c': round(_num(raw) / 1000.0, 1)})
    return out


def _temps_summary(temps):
    """Headline figures: hottest CPU sensor and hottest NVMe, plus the list."""
    cpu = [t['c'] for t in temps if t['chip'] in CPU_CHIPS]
    nvme = [t['c'] for t in temps if t['chip'].startswith('nvme')]
    return {'cpu': max(cpu) if cpu else None, 'nvme': max(nvme) if nvme else None,
            'sensors': temps}


def _temp_alerts():
    s = _temps_summary(_host_temps())
    out = []
    for k, limit in TEMP_HIGH_C.items():
        if s.get(k) is not None and s[k] >= limit:
            out.append({'key': 'temp_high:' + k,
                        'message': '%s temperature is %.0f°C (limit %d)' % (k.upper(), s[k], limit)})
    return out


@bp.route('/api/system/resources')
def system_resources():
    r = _system_resources()
    r['temps'] = _temps_summary(_host_temps())
    r['io'] = _io_rates_live()          # None on the first call after a start
    r['mounts'] = _mount_usage()
    return jsonify(r)


# ─── Host power (reboot / shutdown) ────────────────────────────────────
# Core, never module-gated; POST means central RBAC already requires admin.
# The blanket systemctl sudoers grant every node has covers reboot/poweroff —
# no helper, no new sudoers. The action is fired from a short-delay thread so
# the HTTP response and the audit-log write get out before the box goes down.
def _schedule_power(action):
    from flask import current_app
    if current_app.config.get('TESTING'):
        return False   # a test must never power-cycle the machine it runs on
    def go():
        time.sleep(2)
        run(['systemctl', action])
    threading.Thread(target=go, daemon=True).start()
    return True


@bp.route('/api/system/reboot', methods=['POST'])
def api_system_reboot():
    return jsonify({'success': True, 'action': 'reboot',
                    'scheduled': _schedule_power('reboot')})


@bp.route('/api/system/shutdown', methods=['POST'])
def api_system_shutdown():
    return jsonify({'success': True, 'action': 'shutdown',
                    'scheduled': _schedule_power('poweroff')})


