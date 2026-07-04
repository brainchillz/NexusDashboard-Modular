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
from ..core.config import *
from ..core.runcmd import run, run_safe, err, _size_to_bytes, _human_bytes, _num
from ..core.validators import *
from ..core.services import (SYSTEM_SERVICES, SERVICE_OVERRIDES, resolve_service,
                             _unit_present, RE_SERVICE, LLAMA_SERVICE, LLAMA_CONF,
                             LLAMA_MODELS_DIR, LLAMA_DEFAULT_BIN, LLAMA_URL)
from ..core.registry import load_disabled_modules, MODULES, MODULE_IDS
from ..core.auth import _is_admin, _hash_token, RE_USERNAME

bp = Blueprint('disks', __name__)

_smart_cache = {'ts': 0.0, 'ok': None}


def _smart_health_ok():
    now = time.time()
    if _smart_cache['ts'] and now - _smart_cache['ts'] < 300:
        return _smart_cache['ok']
    ok = True
    out, _, _ = run(['lsblk', '-J', '-o', 'NAME,TYPE'])
    try:
        for d in json.loads(out).get('blockdevices', []):
            if (d.get('type') or '') != 'disk':
                continue
            so, _, _ = run(['smartctl', '-H', '-j', f"/dev/{d['name']}"])
            try:
                st = json.loads(so).get('smart_status') or {}
            except json.JSONDecodeError:
                continue
            if 'passed' in st and not st['passed']:
                ok = False
    except json.JSONDecodeError:
        ok = None
    _smart_cache['ts'], _smart_cache['ok'] = now, ok
    return ok


# ─── Authentication ───────────────────────────────────────────────────
# Single-file admin tool: credentials and the session secret live in a 0600
# JSON file owned by the dashboard user. No database, in keeping with the
# project's footprint.

BOOT_MOUNTS = {'/', '/boot', '/boot/efi', '/boot/efi/', '[SWAP]'}


def _walk(node):
    yield node
    for c in (node.get('children') or []):
        yield from _walk(c)


def _mdadm_conf_arrays():
    """Names of md arrays declared in mdadm.conf (treated as 'defined')."""
    names = set()
    try:
        with open(MDADM_CONF) as f:
            for line in f:
                if line.strip().upper().startswith('ARRAY') and len(line.split()) >= 2:
                    names.add(os.path.basename(line.split()[1]))
    except FileNotFoundError:
        pass
    return names


def _zfs_active_member(node, pool_map):
    """True if any part of this disk belongs to a currently-imported pool. A
    `zfs_member` signature alone is NOT enough — `zpool destroy`/export leaves the
    label behind, so we only trust live membership (from `zpool status`)."""
    return any(n.get('name') in pool_map for n in _walk(node))


def disk_wipe_status(node, defined_md, pool_map):
    """Decide whether a whole disk may be wiped, and why not if not. Protects
    boot/system disks, mounted disks, *live* ZFS/LVM members, and disks in an
    active or defined RAID array. A disk held only by a *stale* signature — an
    auto-assembled md not in mdadm.conf, or a `zfs_member` label from a
    destroyed/exported pool — stays wipeable (md devices to stop are recorded)."""
    nodes = list(_walk(node))
    mounts = [n.get('mountpoint') for n in nodes if n.get('mountpoint')]
    fstypes = [n.get('fstype') for n in nodes if n.get('fstype')]
    if any(m in BOOT_MOUNTS for m in mounts):
        return {'wipeable': False, 'reason': 'system/boot disk'}
    if 'zfs_member' in fstypes and _zfs_active_member(node, pool_map):
        return {'wipeable': False, 'reason': 'ZFS pool member'}
    if 'LVM2_member' in fstypes:
        return {'wipeable': False, 'reason': 'LVM member'}
    md_stale = []
    for md in [n for n in nodes if (n.get('type') or '') == 'md' or (n.get('type') or '').startswith('raid')]:
        sub = list(_walk(md))
        in_use = any(s.get('mountpoint') for s in sub) or \
                 any(s.get('fstype') in ('zfs_member', 'LVM2_member') for s in sub)
        if in_use or md.get('name') in defined_md:
            return {'wipeable': False, 'reason': 'active RAID array member'}
        md_stale.append(md.get('name'))
    if mounts:
        return {'wipeable': False, 'reason': 'mounted'}
    return {'wipeable': True, 'reason': None, 'md_stop': md_stale}


def _zpool_disk_map():
    """Map device basenames (as `zpool status` reports them) to their pool."""
    out, _, _ = run(['zpool', 'status', '-LP'])
    mapping = {}
    pool = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith('pool:'):
            pool = s.split(':', 1)[1].strip()
        elif pool and s.startswith('/dev/'):
            mapping[os.path.basename(s.split()[0])] = pool
    return mapping


# Identify pool members by stable /dev/disk/by-id links (serial/WWN based) rather
# than kernel names (nvme0n1, sda), which can be reordered across reboots and make
# ZFS report a healthy pool as DEGRADED. This is the OpenZFS-recommended scheme.
BY_ID_DIR = '/dev/disk/by-id'


def _by_id_rank(link):
    """Preference key for choosing among several by-id links for one disk.
    Lower sorts first: descriptive serial-bearing ids beat bare wwn-; longer
    (more specific) names win ties. Used only to pick a canonical link."""
    if link.startswith(('nvme-', 'ata-', 'scsi-')) and not link.startswith('nvme-eui.'):
        prio = 0
    elif link.startswith('wwn-') or link.startswith('nvme-eui.'):
        prio = 1
    else:
        prio = 2
    return (prio, -len(link), link)


def _disk_by_id_map(by_id_dir=BY_ID_DIR):
    """Map kernel basename (e.g. 'nvme0n1', 'sda') -> preferred stable
    '/dev/disk/by-id/<link>' path. Reads the symlinks directly (world-readable,
    no sudo); partition links (containing '-part') are skipped so only whole-disk
    identifiers are returned."""
    candidates = {}
    try:
        names = os.listdir(by_id_dir)
    except OSError:
        return {}
    for link in names:
        if '-part' in link:
            continue
        full = os.path.join(by_id_dir, link)
        try:
            target = os.path.basename(os.readlink(full))
        except OSError:
            continue
        candidates.setdefault(target, []).append(link)
    return {dev: os.path.join(by_id_dir, sorted(links, key=_by_id_rank)[0])
            for dev, links in candidates.items()}


def _resolve_stable_dev(dev, by_id_map):
    """Resolve a member identifier (bare name, /dev/X, or an existing by-id path)
    to its stable by-id path. Falls back to the original when no by-id link
    exists (loopback-file scratch pools, virtio disks without a serial) so those
    keep working. Returns (resolved, used_stable)."""
    d = (dev or '').strip()
    if d.startswith(BY_ID_DIR + '/'):
        return d, True
    base = os.path.basename(d)
    stable = by_id_map.get(base)
    if stable:
        return stable, True
    return d, False


# Classify a pool-member path (as `zpool status -P` reports it, WITHOUT -L so
# symlinks are kept) into stable / kernel / other.
RE_KERNEL_DEV = re.compile(r'^/dev/(sd|nvme|vd|hd|xvd|mmcblk|dm-|md)[0-9a-z]')


def _classify_member_path(path):
    if path.startswith(BY_ID_DIR + '/'):
        return 'stable'
    if RE_KERNEL_DEV.match(path):
        return 'kernel'
    return 'other'   # file vdev, /dev/disk/by-{path,uuid}, etc. — not flagged


def _pool_uses_kernel_names(name):
    """True if any leaf vdev of `name` is referenced by a kernel device node
    (and is thus reorder-unstable). Uses `zpool status -P` (full paths) WITHOUT
    -L so stored by-id symlinks are not resolved back to kernel names."""
    out, _, _ = run(['zpool', 'status', '-P', name])
    for line in (out or '').splitlines():
        tok = line.strip().split()
        if tok and tok[0].startswith('/') and _classify_member_path(tok[0]) == 'kernel':
            return True
    return False


def disk_usage(node, pool_map, defined_md):
    """A short human label of what a whole disk is being used for."""
    nodes = list(_walk(node))
    mounts = [n.get('mountpoint') for n in nodes if n.get('mountpoint')]
    fstypes = [n.get('fstype') for n in nodes if n.get('fstype')]
    if any(m in BOOT_MOUNTS for m in mounts):
        return 'System / boot'
    if 'zfs_member' in fstypes:
        for n in nodes:
            if n.get('name') in pool_map:
                return f'ZFS pool: {pool_map[n["name"]]}'
        return 'ZFS member (stale)'
    if 'LVM2_member' in fstypes:
        return 'LVM member'
    md_nodes = [n for n in nodes if (n.get('type') or '') == 'md' or (n.get('type') or '').startswith('raid')]
    for md in md_nodes:
        sub = list(_walk(md))
        if any(s.get('mountpoint') for s in sub) or \
           any(s.get('fstype') in ('zfs_member', 'LVM2_member') for s in sub) or \
           md.get('name') in defined_md:
            return f'RAID: {md.get("name")}'
    if md_nodes or 'linux_raid_member' in fstypes:
        return 'RAID member (stale)'
    if mounts:
        return f'Mounted: {mounts[0]}'
    return 'Free'


@bp.route('/api/disks')
def api_disks():
    out, _, _ = run(['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,MODEL,SERIAL,TRAN'])
    try:
        data = json.loads(out) if out.strip() else {'blockdevices': []}
    except json.JSONDecodeError:
        data = {'blockdevices': []}
    devices = data.get('blockdevices', [])
    defined_md = _mdadm_conf_arrays()
    pool_map = _zpool_disk_map()
    by_id_map = _disk_by_id_map()
    for d in devices:
        if (d.get('type') or '') == 'disk':
            st = disk_wipe_status(d, defined_md, pool_map)
            d['wipeable'] = st['wipeable']
            d['wipe_reason'] = st.get('reason')
            d['md_stop'] = st.get('md_stop', [])
            d['usage'] = disk_usage(d, pool_map, defined_md)
            d['by_id'] = by_id_map.get(d.get('name'))
    out2, _, _ = run(['lsscsi', '-t'])
    return jsonify({'devices': devices, 'scsi_info': out2})

@bp.route('/api/disks/<dev>/smart')
def disk_smart(dev):
    """SMART health for a single block device, normalized across ATA and NVMe.
    smartctl's exit code is a bitmask (non-zero != failure), so we always parse
    the JSON it emits rather than gating on the return code."""
    if not RE_DEVNAME.match(dev):
        return err('Invalid device')
    out, e, _ = run(['smartctl', '-H', '-A', '-i', '-j', f'/dev/{dev}'])
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        return jsonify({'device': dev, 'available': False, 'error': e or 'no SMART data'})

    status = data.get('smart_status') or {}
    info = {
        'device': dev,
        'available': bool(data),
        'model': data.get('model_name'),
        'serial': data.get('serial_number'),
        'firmware': data.get('firmware_version'),
        'rotation_rate': data.get('rotation_rate'),
        'capacity': (data.get('user_capacity') or {}).get('bytes'),
        'health': ('PASSED' if status['passed'] else 'FAILED') if 'passed' in status else 'unknown',
        'temperature_c': (data.get('temperature') or {}).get('current'),
        'power_on_hours': (data.get('power_on_time') or {}).get('hours'),
    }

    # ATA attributes of interest
    attrs = {}
    for a in ((data.get('ata_smart_attributes') or {}).get('table') or []):
        attrs[a.get('name')] = (a.get('raw') or {}).get('value')
    if attrs:
        info['reallocated'] = attrs.get('Reallocated_Sector_Ct')
        info['pending'] = attrs.get('Current_Pending_Sector')
        info['uncorrectable'] = attrs.get('Offline_Uncorrectable')

    # NVMe health log
    nvme = data.get('nvme_smart_health_information_log')
    if nvme:
        info['power_on_hours'] = info['power_on_hours'] or nvme.get('power_on_hours')
        info['temperature_c'] = info['temperature_c'] or nvme.get('temperature')
        info['media_errors'] = nvme.get('media_errors')
        info['percentage_used'] = nvme.get('percentage_used')
        info['critical_warning'] = nvme.get('critical_warning')

    msgs = [m.get('string') for m in ((data.get('smartctl') or {}).get('messages') or [])]
    if msgs:
        info['messages'] = msgs
    return jsonify(info)

@bp.route('/api/disks/<dev>/wipe', methods=['POST'])
def disk_wipe(dev):
    """Wipe a disk back to a blank state: stop any stale md array holding it,
    zero RAID superblocks, remove all signatures, and clear the partition table.
    Eligibility is re-checked server-side here — the client is never trusted."""
    if not RE_DEVNAME.match(dev):
        return err('Invalid device')
    out, _, _ = run(['lsblk', '-J', '-o', 'NAME,TYPE,FSTYPE,MOUNTPOINT', f'/dev/{dev}'])
    try:
        tree = json.loads(out).get('blockdevices', []) if out.strip() else []
    except json.JSONDecodeError:
        tree = []
    if not tree:
        return err('Device not found', 404)
    node = tree[0]
    if (node.get('type') or '') != 'disk':
        return err('Not a whole disk')
    status = disk_wipe_status(node, _mdadm_conf_arrays(), _zpool_disk_map())
    if not status['wipeable']:
        return err(f'Refusing to wipe: {status["reason"]}', 409)

    target = f'/dev/{dev}'
    parts = ['/dev/' + n.get('name') for n in _walk(node) if (n.get('type') or '') == 'part']
    steps = []

    # 1. Stop any stale assembled md array holding this disk.
    for md in status.get('md_stop', []):
        if RE_DEVNAME.match(md or ''):
            steps.append({'step': f'stop md {md}', **run_safe(['mdadm', '--stop', f'/dev/{md}'])})
    # 2. Clear stale ZFS labels (front + back) and RAID superblocks on members +
    #    whole disk, then signatures (ignore "no label/superblock" failures).
    for m in parts:
        run(['zpool', 'labelclear', '-f', m])
        run(['mdadm', '--zero-superblock', m])
        run_safe(['wipefs', '-a', m])
    run(['zpool', 'labelclear', '-f', target])
    run(['mdadm', '--zero-superblock', target])
    # 3. Remove remaining signatures and clear the partition table.
    steps.append({'step': 'wipefs', **run_safe(['wipefs', '-a', target])})
    steps.append({'step': 'zap partition table', **run_safe(['sgdisk', '--zap-all', target])})
    run(['partprobe', target])

    ok = all(s.get('success', True) for s in steps)
    return jsonify({'success': ok, 'steps': steps})

# ─── Disk locate (identify a physical drive) ──────────────────────────
# Best-effort enclosure locate LED (ledctl, SES/SGPIO) PLUS read-only I/O so the
# drive's activity light flashes on any hardware. Read-only -> safe on any disk,
# including in-use pool members (the usual reason you want to find a drive).

_locate_jobs = {}
_locate_lock = threading.Lock()


def _locate_worker(dev, seconds, stop):
    run(['ledctl', f'locate=/dev/{dev}'])  # best effort; no-op if unsupported
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop.is_set():
        # Root-owned read-only helper (O_DIRECT) -> real device reads so the
        # activity LED flashes; the pause between bursts makes it blink.
        run([HELPER_PREFIX + '-locate-read', dev])
        stop.wait(0.3)
    run(['ledctl', f'locate_off=/dev/{dev}'])
    with _locate_lock:
        if _locate_jobs.get(dev) is stop:
            del _locate_jobs[dev]


def _locate_stop(dev):
    with _locate_lock:
        ev = _locate_jobs.pop(dev, None)
    if ev:
        ev.set()


@bp.route('/api/disks/<dev>/locate', methods=['POST'])
def disk_locate(dev):
    if not RE_DEVNAME.match(dev):
        return err('Invalid device')
    if not os.path.exists(f'/dev/{dev}'):
        return err('Device not found', 404)
    data = request.get_json(silent=True) or {}
    if data.get('stop'):
        _locate_stop(dev)
        run(['ledctl', f'locate_off=/dev/{dev}'])
        return jsonify({'success': True, 'stopped': True})
    try:
        seconds = max(3, min(120, int(data.get('seconds', 20))))
    except (TypeError, ValueError):
        seconds = 20
    _locate_stop(dev)  # restart any existing job
    stop = threading.Event()
    with _locate_lock:
        _locate_jobs[dev] = stop
    threading.Thread(target=_locate_worker, args=(dev, seconds, stop), daemon=True).start()
    return jsonify({'success': True, 'seconds': seconds,
                    'message': f'Locating {dev} for {seconds}s — drive activity light '
                               f'(and enclosure locate LED if supported).'})

# ─── Plain-disk format & mount ────────────────────────────────────────
# Everyday "format this disk and mount it" workflow for standard filesystems
# (incl. a just-plugged-in USB drive). All the dangerous primitives — mount,
# umount, and /etc/fstab edits — go through the root-owned wrapper
# `storage-dashboard-mount`, which is the trust boundary: it confines mount
# points to MOUNT_BASES, forces a safe fstab option set, and validates fstab
# before committing. Formatting reuses already-granted tools (wipefs/sgdisk/
# partprobe/mkfs.*). Eligibility is always re-checked server-side.

MOUNT_HELPER = HELPER_PREFIX + '-mount'
FSTAB_PATH = '/etc/fstab'
FSTAB_MARK_BEGIN = '# >>> %s managed >>>' % UNIT_PREFIX
FSTAB_MARK_END = '# <<< %s managed <<<' % UNIT_PREFIX

# mkfs invocation per filesystem: command, force flag, label flag, and the
# GPT partition type code (Linux filesystem vs Microsoft basic data).
MKFS_CFG = {
    'ext4':  {'cmd': 'mkfs.ext4',  'force': '-F', 'label': '-L', 'labelmax': 16, 'ptype': '8300'},
    'xfs':   {'cmd': 'mkfs.xfs',   'force': '-f', 'label': '-L', 'labelmax': 12, 'ptype': '8300'},
    'vfat':  {'cmd': 'mkfs.vfat',  'force': None, 'label': '-n', 'labelmax': 11, 'ptype': '0700'},
    'exfat': {'cmd': 'mkfs.exfat', 'force': None, 'label': '-L', 'labelmax': 15, 'ptype': '0700'},
}


def _part1_name(dev):
    """Kernel name of partition 1 on a whole disk: sdb→sdb1, nvme0n1→nvme0n1p1."""
    return f'{dev}p1' if dev[-1:].isdigit() else f'{dev}1'


def _managed_fstab_uuids(path=FSTAB_PATH):
    """UUIDs the dashboard added to /etc/fstab (inside its managed block).
    fstab is world-readable, so no sudo is needed to read it."""
    uuids = set()
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return uuids
    inblock = False
    for ln in lines:
        s = ln.strip()
        if s == FSTAB_MARK_BEGIN:
            inblock = True
        elif s == FSTAB_MARK_END:
            inblock = False
        elif inblock and s.startswith('UUID='):
            uuids.add(s.split()[0][len('UUID='):])
    return uuids


def _list_filesystems():
    """Plain mountable filesystems (partitions or whole-disk), including USB.
    Excludes ZFS/LVM/MD/swap members (those belong to other subsystems)."""
    out, _, _ = run(['lsblk', '-J', '-o',
                     'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL,UUID,TRAN,MODEL'])
    try:
        devices = json.loads(out).get('blockdevices', []) if out.strip() else []
    except json.JSONDecodeError:
        devices = []
    managed = _managed_fstab_uuids()
    fs = []
    for top in devices:
        for n in _walk(top):
            fstype = n.get('fstype')
            if not fstype or fstype in NON_MOUNTABLE_FSTYPES:
                continue
            if (n.get('type') or '') not in ('part', 'disk'):
                continue
            mnt = n.get('mountpoint') or ''
            system = mnt in BOOT_MOUNTS or any(
                mnt == b or mnt.startswith(b + '/') for b in ('/', '/boot', '/usr', '/var', '/etc'))
            # A non-system mount that isn't under our bases (e.g. mounted by
            # hand elsewhere) is shown but treated as not-ours to unmount.
            ours = any(mnt == b or mnt.startswith(b + '/') for b in MOUNT_BASES)
            fs.append({
                'name': n.get('name'), 'size': n.get('size'), 'fstype': fstype,
                'label': n.get('label'), 'uuid': n.get('uuid'),
                'tran': n.get('tran'), 'model': (n.get('model') or '').strip(),
                'mountpoint': mnt or None, 'mounted': bool(mnt),
                'system': system, 'unmountable': bool(mnt) and not system and ours,
                'fstab': bool(n.get('uuid') and n.get('uuid') in managed),
            })
    return fs


@bp.route('/api/disks/<dev>/format', methods=['POST'])
def disk_format(dev):
    """Initialize a Free disk: GPT label + one whole-disk partition + mkfs.
    Refuses any disk that is in use (re-checked here, client never trusted)."""
    if not RE_DEVNAME.match(dev):
        return err('Invalid device')
    data = request.get_json(silent=True) or {}
    fstype = (data.get('fstype') or '').lower()
    if fstype not in MOUNT_FSTYPES:
        return err('Unsupported filesystem type')
    label = data.get('label') or ''
    if label and not RE_FSLABEL.match(label):
        return err('Invalid label (letters, digits, . _ - ; max 32)')

    out, _, _ = run(['lsblk', '-J', '-o', 'NAME,TYPE,FSTYPE,MOUNTPOINT', f'/dev/{dev}'])
    try:
        tree = json.loads(out).get('blockdevices', []) if out.strip() else []
    except json.JSONDecodeError:
        tree = []
    if not tree:
        return err('Device not found', 404)
    node = tree[0]
    if (node.get('type') or '') != 'disk':
        return err('Not a whole disk')
    status = disk_wipe_status(node, _mdadm_conf_arrays(), _zpool_disk_map())
    if not status['wipeable']:
        return err(f'Refusing to format: {status["reason"]}', 409)

    cfg = MKFS_CFG[fstype]
    target = f'/dev/{dev}'
    steps = []
    # Clear any stale signatures / labels, then lay down a fresh GPT + 1 part.
    for m in ['/dev/' + n.get('name') for n in _walk(node) if (n.get('type') or '') == 'part']:
        run(['wipefs', '-a', m])
    steps.append({'step': 'wipe signatures', **run_safe(['wipefs', '-a', target])})
    steps.append({'step': 'new GPT label', **run_safe(['sgdisk', '-Z', target])})
    steps.append({'step': 'create partition',
                  **run_safe(['sgdisk', '-n', '1:0:0', '-t', f'1:{cfg["ptype"]}', target])})
    run(['partprobe', target])

    part = _part1_name(dev)
    pdev = f'/dev/{part}'
    for _ in range(20):  # give udev a moment to create the partition node
        if os.path.exists(pdev):
            break
        time.sleep(0.15)
    if not os.path.exists(pdev):
        return jsonify({'success': False, 'error': 'Partition did not appear after partitioning',
                        'steps': steps}), 500

    mkfs = [cfg['cmd']]
    if cfg['force']:
        mkfs.append(cfg['force'])
    if label:
        lbl = label[:cfg['labelmax']]
        if fstype == 'vfat':
            lbl = lbl.upper()
        mkfs += [cfg['label'], lbl]
    mkfs.append(pdev)
    steps.append({'step': f'mkfs.{fstype}', **run_safe(mkfs)})

    # Let udev catch up so the new filesystem's UUID/fstype are populated before
    # we read them back (and before the UI re-lists filesystems).
    run(['udevadm', 'settle'], no_sudo=True)
    uuid = ''
    for _ in range(20):
        uuid = run(['lsblk', '-no', 'UUID', pdev], no_sudo=True)[0].strip()
        if uuid:
            break
        time.sleep(0.15)
    ok = all(s.get('success', True) for s in steps)
    return jsonify({'success': ok, 'steps': steps, 'partition': part, 'uuid': uuid})


@bp.route('/api/filesystems')
def api_filesystems():
    return jsonify({'filesystems': _list_filesystems(), 'bases': list(MOUNT_BASES)})


def _lookup_fs(part):
    """(node-dict, error-response) for a partition/disk that holds a plain fs."""
    out, _, _ = run(['lsblk', '-J', '-o', 'NAME,TYPE,FSTYPE,UUID,MOUNTPOINT', f'/dev/{part}'])
    try:
        tree = json.loads(out).get('blockdevices', []) if out.strip() else []
    except json.JSONDecodeError:
        tree = []
    if not tree:
        return None, err('Device not found', 404)
    n = tree[0]
    fstype = n.get('fstype')
    if not fstype or fstype in NON_MOUNTABLE_FSTYPES:
        return None, err('Not a mountable filesystem (it belongs to ZFS/LVM/RAID/swap)', 409)
    return n, None


@bp.route('/api/filesystems/<part>/mount', methods=['POST'])
def fs_mount(part):
    if not RE_DEVNAME.match(part):
        return err('Invalid device')
    data = request.get_json(silent=True) or {}
    name = data.get('name') or part
    if not RE_MOUNTNAME.match(name):
        return err('Invalid mount-point name (letters, digits, . _ -)')
    base = data.get('base') or MOUNT_BASES[0]
    if base not in MOUNT_BASES:
        return err('Invalid mount base')
    n, e = _lookup_fs(part)
    if e:
        return e
    if n.get('mountpoint'):
        return err(f'Already mounted at {n["mountpoint"]}', 409)
    res = run_safe([MOUNT_HELPER, 'mount', part, name, base])
    if not res['success']:
        return jsonify({'success': False, 'error': res['stderr'].strip() or 'mount failed',
                        'detail': res}), 500
    fstab = bool(data.get('fstab'))
    fstab_res = None
    if fstab:
        uuid = n.get('uuid')
        if not uuid:
            fstab_res = {'success': False, 'stderr': 'no filesystem UUID; cannot persist'}
        else:
            fstab_res = run_safe([MOUNT_HELPER, 'fstab-add', uuid, f'{base}/{name}', n.get('fstype')])
    return jsonify({'success': True, 'mountpoint': f'{base}/{name}',
                    'fstab': (fstab_res['success'] if fstab_res else False),
                    'fstab_detail': fstab_res})


@bp.route('/api/filesystems/<part>/unmount', methods=['POST'])
def fs_unmount(part):
    if not RE_DEVNAME.match(part):
        return err('Invalid device')
    data = request.get_json(silent=True) or {}
    n, e = _lookup_fs(part)
    if e:
        return e
    mnt = n.get('mountpoint')
    if not mnt:
        return err('Not mounted', 409)
    if not any(mnt == b or mnt.startswith(b + '/') for b in MOUNT_BASES):
        return err('Refusing to unmount: not a dashboard-managed mount point', 409)
    res = run_safe([MOUNT_HELPER, 'umount', part])
    if not res['success']:
        return jsonify({'success': False, 'error': res['stderr'].strip() or 'unmount failed',
                        'detail': res}), 500
    fstab_res = None
    if data.get('remove_fstab') and n.get('uuid'):
        fstab_res = run_safe([MOUNT_HELPER, 'fstab-remove', n.get('uuid')])
    return jsonify({'success': True, 'fstab_removed': (fstab_res['success'] if fstab_res else False),
                    'fstab_detail': fstab_res})


@bp.route('/api/logs/<service>')
def api_logs(service):
    svc = resolve_service(service)
    if not svc:
        return err('Invalid service')
    out, _, rc = run(['journalctl', '-u', svc, '--no-pager', '-n', '100', '--output=short-unix'])
    if rc != 0 or not out.strip():
        out = out or 'No logs available'
    return jsonify({'logs': out})


# ─── Log viewer (feature 08) ──────────────────────────────────────────
# A journald browser over a CURATED set of units (this app, the system services,
# and the dashboard-managed task units) — never an arbitrary unit from the client,
# and the grep filter is allowlisted so it can't become a journalctl flag.


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'disks', 'label': 'Disks', 'category': 'Storage MGMT',
          'blueprint': bp}
