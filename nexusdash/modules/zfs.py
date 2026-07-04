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
from .disks import _walk, BY_ID_DIR, _disk_by_id_map, _pool_uses_kernel_names, _resolve_stable_dev

bp = Blueprint('zfs', __name__)

def parse_zpool_status(output):
    pools = {}
    current_pool = None
    for line in output.split('\n'):
        if line.startswith('  pool:'):
            current_pool = line.split('pool:')[1].strip()
            pools[current_pool] = {'config': [], 'errors': ''}
        elif line.startswith(' state:') and current_pool:
            pools[current_pool]['state'] = line.split('state:')[1].strip()
        elif line.startswith('  scan:') and current_pool:
            pools[current_pool]['scan'] = line.split('scan:')[1].strip()
        elif line.startswith('config:') and current_pool:
            pass
        elif current_pool and ('ONLINE' in line or 'DEGRADED' in line or 'FAULTED' in line or 'OFFLINE' in line or 'UNAVAIL' in line or 'REMOVED' in line):
            pools[current_pool]['config'].append(line.strip())
        elif line.startswith('errors:') and current_pool:
            pools[current_pool]['errors'] = line.split('errors:')[1].strip()
    return pools

@bp.route('/api/zfs/pools')
def zfs_pools():
    out, e, rc = run(['zpool', 'list', '-Ho', 'name,size,alloc,free,cap,frag,dedup,health,altroot'])
    if rc != 0:
        return jsonify({'pools': [], 'raw_output': e})
    pools = []
    for line in out.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) >= 8:
            pools.append({
                'name': parts[0], 'size': parts[1], 'alloc': parts[2],
                'free': parts[3], 'cap': parts[4], 'frag': parts[5],
                'dedup': parts[6], 'health': parts[7], 'altroot': parts[8],
            })
    return jsonify(pools)

@bp.route('/api/zfs/pools/detail')
def zfs_pools_detail():
    out, _, _ = run(['zpool', 'status'])
    pools = {}
    if out:
        pools = parse_zpool_status(out)
    # Flag pools whose members are referenced by reorder-unstable kernel names so
    # the UI can offer to "stabilize" them (re-import by /dev/disk/by-id).
    for pname, pdata in pools.items():
        pdata['unstable'] = _pool_uses_kernel_names(pname)
    return jsonify(pools)


def _parse_arcstats(text):
    """Parse /proc/spl/kstat/zfs/arcstats ('name  type  value' columns) into a
    flat {name: int}. Pure — unit-tested without ZFS present."""
    stats = {}
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].lstrip('-').isdigit():
            stats[parts[0]] = int(parts[2])
    return stats


def _arc_summary(stats):
    """Derive the headline ARC/L2ARC figures from raw arcstats."""
    hits, misses = stats.get('hits', 0), stats.get('misses', 0)
    total = hits + misses
    l2_size = stats.get('l2_size', 0)
    return {
        'size': stats.get('size', 0),
        'c_max': stats.get('c_max', 0),
        'c_min': stats.get('c_min', 0),
        'hits': hits, 'misses': misses,
        'hit_ratio': round(hits / total * 100, 1) if total else None,
        'l2_present': l2_size > 0,
        'l2_size': l2_size,
        'l2_hits': stats.get('l2_hits', 0),
        'l2_misses': stats.get('l2_misses', 0),
    }


@bp.route('/api/zfs/arc')
def zfs_arc():
    """ARC/L2ARC stats from /proc (no sudo). ARC is present on any host with ZFS
    loaded regardless of cache devices; l2_present stays false until an L2ARC
    (cache vdev) exists."""
    try:
        with open('/proc/spl/kstat/zfs/arcstats') as f:
            stats = _parse_arcstats(f.read())
    except OSError:
        return jsonify({'available': False})
    return jsonify({'available': True, **_arc_summary(stats)})


@bp.route('/api/zfs/pools/<name>/scrub', methods=['POST'])
def zfs_pool_scrub(name):
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    action = (request.get_json(silent=True) or {}).get('action', 'start')
    if action == 'start':
        return jsonify(run_safe(['zpool', 'scrub', name]))
    if action == 'stop':
        return jsonify(run_safe(['zpool', 'scrub', '-s', name]))
    return err('Invalid scrub action')

@bp.route('/api/zfs/pools/<name>/trim', methods=['POST'])
def zfs_pool_trim(name):
    """SSD TRIM: reclaim unused blocks. Errors on vdevs that don't support it are
    surfaced from stderr rather than swallowed."""
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    action = (request.get_json(silent=True) or {}).get('action', 'start')
    if action == 'start':
        return jsonify(run_safe(['zpool', 'trim', name]))
    if action == 'cancel':
        return jsonify(run_safe(['zpool', 'trim', '-c', name]))
    return err('Invalid trim action')

@bp.route('/api/zfs/pools/<name>/autotrim', methods=['POST'])
def zfs_pool_autotrim(name):
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    enabled = bool((request.get_json(silent=True) or {}).get('enabled'))
    return jsonify(run_safe(['zpool', 'set', 'autotrim=' + ('on' if enabled else 'off'), name]))

@bp.route('/api/zfs/pools/<name>/device', methods=['POST'])
def zfs_pool_device(name):
    """Per-device operations: offline / online / detach a member, replace a
    member with a new device, or remove a device. `detach` splits a mirror
    member; `remove` pulls a cache (L2ARC) / log (SLOG) / spare device (and, on
    modern OpenZFS, evacuates a top-level data vdev)."""
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    data = request.get_json() or {}
    action = data.get('action', '')
    device = (data.get('device') or '').strip()
    if action not in ('replace', 'offline', 'online', 'detach', 'remove'):
        return err('Invalid device action')
    if not device or not RE_DEVICE.match(device):
        return err('Invalid device')
    if action == 'replace':
        new_device = (data.get('new_device') or '').strip()
        if not new_device or not RE_DEVICE.match(new_device):
            return err('Invalid replacement device')
        # Bring the replacement in by its stable by-id path so it survives reboots.
        new_device, _ = _resolve_stable_dev(new_device, _disk_by_id_map())
        return jsonify(run_safe(['zpool', 'replace', name, device, new_device]))
    return jsonify(run_safe(['zpool', action, name, device]))

def _zfs_disk_usable(dev):
    """Whether a device may back a new pool/vdev. `zpool create/add` use -f, so
    the server must reject in-use disks itself (the client is never trusted): a
    real block device must be free — not mounted/boot, nor a ZFS/RAID/LVM/swap
    member (a stale label counts as in-use; wipe it first). A path lsblk doesn't
    recognise as a block device (e.g. a file vdev) is allowed — it can't clobber
    other storage."""
    if not RE_DISK.match(dev):
        return False
    out, _, _ = run(['lsblk', '-J', '-o', 'NAME,FSTYPE,MOUNTPOINT', dev])
    try:
        nodes = json.loads(out).get('blockdevices', [])
    except json.JSONDecodeError:
        nodes = []
    if not nodes:
        return True   # not a block device (file vdev) — no risk to other storage
    for n in nodes:
        for x in _walk(n):
            if x.get('mountpoint'):
                return False
            if x.get('fstype') in ('zfs_member', 'linux_raid_member', 'LVM2_member', 'swap'):
                return False
    return True


@bp.route('/api/zfs/pools/<name>/vdev', methods=['POST'])
def zfs_pool_add_vdev(name):
    """Add a vdev to a pool: extra data vdev (optionally mirror/raidz), or a
    spare / cache (L2ARC) / log (SLOG) device."""
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    data = request.get_json() or {}
    role = data.get('role', '')
    disks = data.get('disks', [])
    if role not in VDEV_ADD_ROLES:
        return err('Invalid vdev role')
    if not disks:
        return err('No disks specified')
    for d in disks:
        if not RE_DEVICE.match(d):
            return err(f'Invalid disk: {d}')
        if not _zfs_disk_usable(d):
            return err(f'Disk {d} is in use (mounted/boot or a pool/RAID/LVM member) — refusing', 409)
    by_id_map = _disk_by_id_map()
    disks = [_resolve_stable_dev(d, by_id_map)[0] for d in disks]
    cmd = ['zpool', 'add', '-f', name]
    if role:
        cmd.append(role)
    cmd.extend(disks)
    return jsonify(run_safe(cmd))

# Section keywords that precede a vdev group in `zpool create` ('' = data vdev).
VDEV_ROLES = {'', 'log', 'cache', 'spare'}


def _normalize_vdev_spec(data):
    """Return (groups, error). Each group is {role, type, disks}. Accepts the
    structured `vdevs` list AND the legacy {vdev_type, disks} single-group form."""
    if isinstance(data.get('vdevs'), list):
        groups = []
        for g in data['vdevs']:
            if not isinstance(g, dict):
                return None, 'Invalid vdev group'
            groups.append({'role': (g.get('role') or '').strip(),
                           'type': (g.get('type') or '').strip(),
                           'disks': g.get('disks') or []})
        return groups, None
    return [{'role': '', 'type': (data.get('vdev_type') or '').strip(),
             'disks': data.get('disks') or []}], None


def _pool_vdev_args(groups):
    """Turn normalized {role,type,disks} groups (disks already resolved) into the
    `zpool create` argument tail. Pure — raises ValueError on an invalid spec."""
    args = []
    for g in groups:
        role, vtype, disks = g['role'], g['type'], g['disks']
        if role not in VDEV_ROLES:
            raise ValueError(f'Invalid vdev role: {role}')
        if vtype not in VDEV_TYPES:
            raise ValueError(f'Invalid vdev type: {vtype}')
        if not disks:
            raise ValueError('A vdev group has no disks')
        if role in ('cache', 'spare') and vtype:
            raise ValueError(f'{role} devices cannot be {vtype}')
        if role:
            args.append(role)
        if vtype:
            args.append(vtype)
        args.extend(disks)
    return args


@bp.route('/api/zfs/pools', methods=['POST'])
def zfs_pool_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name or not RE_POOL.match(name):
        return err('Invalid pool name')
    groups, e = _normalize_vdev_spec(data)
    if e:
        return err(e)
    if not any(g['disks'] for g in groups):
        return err('No disks specified')
    # Validate + resolve every disk to its stable /dev/disk/by-id path so the pool
    # won't go DEGRADED if kernel device names get reordered across a reboot.
    by_id_map = _disk_by_id_map()
    for g in groups:
        resolved = []
        for d in g['disks']:
            if not RE_DISK.match(d):
                return err(f'Invalid disk: {d}')
            if not _zfs_disk_usable(d):
                return err(f'Disk {d} is in use (mounted/boot or a pool/RAID/LVM member) — refusing', 409)
            resolved.append(_resolve_stable_dev(d, by_id_map)[0])
        g['disks'] = resolved
    try:
        vargs = _pool_vdev_args(groups)
    except ValueError as ve:
        return err(str(ve))
    return jsonify(run_safe(['zpool', 'create', '-f', name] + vargs))

@bp.route('/api/zfs/pools/<name>', methods=['DELETE'])
def zfs_pool_destroy(name):
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    return jsonify(run_safe(['zpool', 'destroy', '-f', name]))


def _parse_importable(text):
    """Parse `zpool import` (scan) output into a list of importable pools."""
    pools, cur = [], None
    for raw in (text or '').split('\n'):
        line = raw.strip()
        if line.startswith('pool:'):
            if cur:
                pools.append(cur)
            cur = {'name': line.split(':', 1)[1].strip(), 'id': '', 'state': '', 'action': ''}
        elif cur is None:
            continue
        elif line.startswith('id:'):
            cur['id'] = line.split(':', 1)[1].strip()
        elif line.startswith('state:'):
            cur['state'] = line.split(':', 1)[1].strip()
        elif line.startswith('status:'):
            cur['action'] = line.split(':', 1)[1].strip()
    if cur:
        pools.append(cur)
    return pools


@bp.route('/api/zfs/pools/importable')
def zfs_pools_importable():
    """Pools present on attached devices but not currently imported."""
    out, _, _ = run(['zpool', 'import'])
    return jsonify(_parse_importable(out))


@bp.route('/api/zfs/pools/import', methods=['POST'])
def zfs_pool_import():
    data = request.get_json() or {}
    ident = (data.get('name') or data.get('id') or '').strip()  # pool name or numeric id
    new_name = (data.get('new_name') or '').strip()
    altroot = (data.get('altroot') or '').strip()
    # Accept either a pool name or an all-digit pool id.
    if not (RE_POOL.match(ident) or RE_NUM.match(ident)):
        return err('Invalid pool name or id')
    if new_name and not RE_POOL.match(new_name):
        return err('Invalid new pool name')
    if altroot and not RE_PATH.match(altroot):
        return err('Invalid altroot path')
    cmd = ['zpool', 'import']
    if data.get('force'):
        cmd.append('-f')
    if altroot:
        cmd += ['-R', altroot]
    cmd.append(ident)
    if new_name:
        cmd.append(new_name)
    return jsonify(run_safe(cmd))


@bp.route('/api/zfs/pools/<name>/export', methods=['POST'])
def zfs_pool_export(name):
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    # Never export a pool that backs the running system (a dataset mounted at /
    # or another critical path) — that would wedge the host.
    out, _, _ = run(['zfs', 'list', '-r', '-H', '-o', 'mountpoint', name])
    mounts = {m.strip() for m in out.split('\n') if m.strip()}
    if mounts & {'/', '/boot', '/usr', '/var'}:
        return err('Refusing to export: this pool backs the running system', 409)
    cmd = ['zpool', 'export']
    if (request.get_json(silent=True) or {}).get('force'):
        cmd.append('-f')
    cmd.append(name)
    return jsonify(run_safe(cmd))


@bp.route('/api/zfs/pools/<name>/stabilize', methods=['POST'])
def zfs_pool_stabilize(name):
    """Rewrite a pool's member paths to stable /dev/disk/by-id links by exporting
    and re-importing with `-d /dev/disk/by-id`. Fixes pools that go DEGRADED when
    kernel device names (nvme0n1, sda) get reordered across reboots. The pool is
    briefly offline during the export/import."""
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    # Same guard as export: never take the pool backing the running system offline.
    out, _, _ = run(['zfs', 'list', '-r', '-H', '-o', 'mountpoint', name])
    mounts = {m.strip() for m in out.split('\n') if m.strip()}
    if mounts & {'/', '/boot', '/usr', '/var'}:
        return err('Refusing to stabilize: this pool backs the running system', 409)
    steps = []
    exp = run_safe(['zpool', 'export', name])
    steps.append({'step': 'export', **exp})
    if not exp['success']:
        # Export failed (pool busy) — abort before touching anything else.
        return jsonify({'steps': steps, 'success': False,
                        'error': 'Export failed (pool in use?); pool left imported.'})
    imp = run_safe(['zpool', 'import', '-d', BY_ID_DIR, name])
    steps.append({'step': 'import (by-id)', **imp})
    if not imp['success']:
        # Recover: re-import normally so we don't leave the pool exported.
        steps.append({'step': 'recover import', **run_safe(['zpool', 'import', name])})
        return jsonify({'steps': steps, 'success': False,
                        'error': 'Re-import by-id failed; pool re-imported with previous paths.'})
    return jsonify({'steps': steps, 'success': True})

@bp.route('/api/zfs/pools/<name>/datasets')
def zfs_datasets(name):
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    out, _, _ = run(['zfs', 'list', '-r', '-H', '-o',
                     'name,used,available,referenced,mountpoint,compression,quota,reservation,type,encryption,keystatus', name])
    datasets = []
    for line in out.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) >= 4:
            datasets.append({
                'name': parts[0], 'used': parts[1], 'available': parts[2],
                'referenced': parts[3], 'mountpoint': parts[4],
                'compression': parts[5] if len(parts) > 5 else '-',
                'quota': parts[6] if len(parts) > 6 else '-',
                'reservation': parts[7] if len(parts) > 7 else '-',
                'type': parts[8] if len(parts) > 8 else 'filesystem',
                'encryption': parts[9] if len(parts) > 9 else 'off',
                'keystatus': parts[10] if len(parts) > 10 else '-',
            })
    return jsonify(datasets)

# Native ZFS encryption is set only at dataset creation. Algorithms + key formats
# are allowlisted; the passphrase is fed on stdin (keylocation=prompt) so it never
# reaches the process command line or the audit log.
ZFS_ENC_ALGOS = {'on', 'aes-256-gcm', 'aes-192-gcm', 'aes-128-gcm',
                 'aes-256-ccm', 'aes-192-ccm', 'aes-128-ccm'}
ZFS_KEYFORMATS = {'passphrase', 'hex', 'raw'}


@bp.route('/api/zfs/datasets', methods=['POST'])
def zfs_dataset_create():
    data = request.get_json()
    name = data.get('name', '').strip()
    properties = data.get('properties', {})
    volsize = (data.get('volsize') or '').strip()
    if not name or not RE_DATASET.match(name):
        return err('Invalid dataset name')
    cmd = ['zfs', 'create']
    if volsize:
        # A ZVOL (block volume) - usable as an iSCSI block backstore.
        if not RE_SIZE.match(volsize):
            return err('Invalid volume size')
        cmd += ['-V', volsize]
    # Optional native encryption (creation-time only).
    input_data = None
    enc = (data.get('encryption') or '').strip()
    if enc:
        if enc not in ZFS_ENC_ALGOS:
            return err('Invalid encryption algorithm')
        keyformat = (data.get('keyformat') or 'passphrase').strip()
        if keyformat != 'passphrase':
            return err('Only the passphrase key format is supported from the UI')
        passphrase = data.get('passphrase') or ''
        if len(passphrase) < 8:
            return err('Encryption passphrase must be at least 8 characters')
        cmd += ['-o', f'encryption={enc}', '-o', 'keyformat=passphrase',
                '-o', 'keylocation=prompt']
        # `zfs create` reads the passphrase from stdin and asks to confirm it.
        input_data = passphrase + '\n' + passphrase + '\n'
    for k, v in properties.items():
        if v:
            if not RE_PROP.match(k):
                return err(f'Invalid property name: {k}')
            cmd.extend(['-o', f'{k}={v}'])
    cmd.append(name)
    return jsonify(run_safe(cmd, input_data=input_data))

@bp.route('/api/zfs/datasets/all')
def zfs_datasets_all():
    """Every snapshot target: pool roots, datasets, and volumes (for pickers)."""
    out, _, _ = run(['zfs', 'list', '-H', '-o', 'name,type', '-t', 'filesystem,volume'])
    items = []
    for line in out.strip().split('\n'):
        if '\t' in line:
            name, dtype = line.split('\t')[:2]
            items.append({'name': name, 'type': dtype, 'is_pool': '/' not in name})
    return jsonify(items)

@bp.route('/api/zfs/zvols')
def zfs_zvols():
    """List ZFS volumes (ZVOLs) usable as iSCSI block backstores."""
    out, _, _ = run(['zfs', 'list', '-H', '-t', 'volume', '-o', 'name,volsize'])
    vols = []
    for line in out.strip().split('\n'):
        if '\t' in line:
            name, volsize = line.split('\t')[:2]
            vols.append({'name': name, 'volsize': volsize, 'path': f'/dev/zvol/{name}'})
    return jsonify(vols)

@bp.route('/api/zfs/datasets/rename', methods=['POST'])
def zfs_dataset_rename():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    new_name = (data.get('new_name') or '').strip()
    if not RE_DATASET.match(name) or not RE_DATASET.match(new_name):
        return err('Invalid dataset name')
    return jsonify(run_safe(['zfs', 'rename', name, new_name]))

@bp.route('/api/zfs/datasets/<path:name>', methods=['DELETE'])
def zfs_dataset_destroy(name):
    if not RE_DATASET.match(name):
        return err('Invalid dataset name')
    return jsonify(run_safe(['zfs', 'destroy', '-r', name]))


# ─── Encryption key management (passphrase always via stdin, never argv) ──
@bp.route('/api/zfs/datasets/<path:name>/key/load', methods=['POST'])
def zfs_key_load(name):
    if not RE_DATASET.match(name):
        return err('Invalid dataset name')
    passphrase = (request.get_json() or {}).get('passphrase') or ''
    if not passphrase:
        return err('Passphrase required')
    return jsonify(run_safe(['zfs', 'load-key', name], input_data=passphrase + '\n'))


@bp.route('/api/zfs/datasets/<path:name>/key/unload', methods=['POST'])
def zfs_key_unload(name):
    if not RE_DATASET.match(name):
        return err('Invalid dataset name')
    return jsonify(run_safe(['zfs', 'unload-key', name]))


@bp.route('/api/zfs/datasets/<path:name>/key/change', methods=['POST'])
def zfs_key_change(name):
    if not RE_DATASET.match(name):
        return err('Invalid dataset name')
    passphrase = (request.get_json() or {}).get('passphrase') or ''
    if len(passphrase) < 8:
        return err('New passphrase must be at least 8 characters')
    return jsonify(run_safe(['zfs', 'change-key', name],
                            input_data=passphrase + '\n' + passphrase + '\n'))

@bp.route('/api/zfs/snapshots')
def zfs_snapshots():
    pool = request.args.get('pool', '')
    # `written` = space unique to this snapshot since the previous one; `used` =
    # space freed if ONLY this snapshot is destroyed (not additive across snaps).
    cols = 'name,used,written,referenced,creation'
    cmd = ['zfs', 'list', '-H', '-t', 'snapshot', '-o', cols]
    if pool:
        if not RE_POOL.match(pool):
            return err('Invalid pool name')
        cmd = ['zfs', 'list', '-H', '-r', '-t', 'snapshot', '-o', cols, pool]
    out, _, _ = run(cmd)
    snapshots = []
    for line in out.strip().split('\n'):
        if not line.strip() or '\t' not in line:
            continue
        parts = line.split('\t')
        if len(parts) >= 5:
            snapshots.append({
                'name': parts[0], 'used': parts[1], 'written': parts[2],
                'referenced': parts[3], 'creation': parts[4],
            })
    return jsonify(snapshots)

@bp.route('/api/zfs/snapshots', methods=['POST'])
def zfs_snapshot_create():
    data = request.get_json()
    dataset = data.get('dataset', '').strip()
    snap_name = data.get('snap_name', '').strip()
    if not dataset or not RE_DATASET.match(dataset):
        return err('Invalid dataset')
    if not snap_name:
        snap_name = f'snap-{int(time.time())}'
    full_name = f'{dataset}@{snap_name}'
    if not RE_SNAP.match(full_name):
        return err('Invalid snapshot name')
    cmd = ['zfs', 'snapshot']
    if data.get('recursive'):
        cmd.append('-r')
    cmd.append(full_name)
    return jsonify(run_safe(cmd))

@bp.route('/api/zfs/snapshots/clone', methods=['POST'])
def zfs_snapshot_clone():
    data = request.get_json() or {}
    snapshot = (data.get('snapshot') or '').strip()
    target = (data.get('target') or '').strip()
    if not RE_SNAP.match(snapshot):
        return err('Invalid snapshot')
    if not RE_DATASET.match(target):
        return err('Invalid target dataset name')
    return jsonify(run_safe(['zfs', 'clone', snapshot, target]))

@bp.route('/api/zfs/snapshots/rollback', methods=['POST'])
def zfs_snapshot_rollback():
    data = request.get_json()
    snap = data.get('snapshot', '').strip()
    if not snap or not RE_SNAP.match(snap):
        return err('Invalid snapshot')
    return jsonify(run_safe(['zfs', 'rollback', '-r', snap]))

@bp.route('/api/zfs/snapshots/<path:name>', methods=['DELETE'])
def zfs_snapshot_destroy(name):
    if not RE_SNAP.match(name):
        return err('Invalid snapshot')
    return jsonify(run_safe(['zfs', 'destroy', '-r', name]))


# zfs diff change-type codes -> human labels.
_DIFF_KIND = {'+': 'added', '-': 'removed', 'M': 'modified', 'R': 'renamed'}


@bp.route('/api/zfs/snapshots/diff')
def zfs_snapshot_diff():
    """Differences between a snapshot and a later snapshot (or the live dataset).
    `to` may be another snapshot of the same dataset, or the dataset itself."""
    frm = (request.args.get('from') or '').strip()
    to = (request.args.get('to') or '').strip()
    if not RE_SNAP.match(frm):
        return err('Invalid "from" snapshot')
    if to and not (RE_SNAP.match(to) or RE_DATASET.match(to)):
        return err('Invalid "to" snapshot/dataset')
    cmd = ['zfs', 'diff', '-H', '-F', frm]
    if to:
        cmd.append(to)
    out, errtxt, rc = run(cmd)
    if rc != 0:
        return jsonify({'success': False, 'error': (errtxt or 'zfs diff failed').strip()[:200]}), 400
    changes = []
    for line in out.split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) < 3:
            continue
        change, ftype, path = parts[0], parts[1], parts[2]
        entry = {'change': _DIFF_KIND.get(change, change), 'ftype': ftype, 'path': path}
        if change == 'R' and len(parts) >= 4:
            entry['path_to'] = parts[3]
        changes.append(entry)
    return jsonify({'success': True, 'changes': changes, 'count': len(changes)})


# Root-owned helper that resolves & confines snapshot/live paths and does the
# actual read/copy as root (snapshot dirs and live datasets aren't readable by
# the unprivileged dashboard user). It enforces its own confinement, so it is
# the security boundary — see install.sh. Not writable by `dashboard`.
SNAP_FS_HELPER = HELPER_PREFIX + '-snap-fs'


def _split_snap(snap):
    """Validate a dataset@snapshot string and return (dataset, snapname)."""
    if not RE_SNAP.match(snap) or '@' not in snap:
        return None, None
    dataset, snapname = snap.split('@', 1)
    return dataset, snapname


def _valid_relpath(p):
    # Relative, no NUL/newline, no traversal segments. (The helper re-confines
    # via realpath regardless; this is a cheap first gate.)
    if p in ('', '.'):
        return True
    if p.startswith('/') or '\x00' in p or '\n' in p or '\r' in p:
        return False
    return '..' not in p.split('/')


@bp.route('/api/zfs/snapshots/<path:snap>/browse')
def zfs_snapshot_browse(snap):
    dataset, snapname = _split_snap(snap)
    if not dataset:
        return err('Invalid snapshot')
    relpath = request.args.get('path', '')
    if not _valid_relpath(relpath):
        return err('Invalid path')
    out, errtxt, rc = run([SNAP_FS_HELPER, 'browse', dataset, snapname, relpath])
    if rc != 0:
        return err((errtxt or 'browse failed').strip()[:200], 400)
    try:
        return jsonify(json.loads(out))
    except json.JSONDecodeError:
        return err('Could not read snapshot directory', 500)


@bp.route('/api/zfs/snapshots/<path:snap>/restore', methods=['POST'])
def zfs_snapshot_restore(snap):
    dataset, snapname = _split_snap(snap)
    if not dataset:
        return err('Invalid snapshot')
    data = request.get_json() or {}
    relpath = (data.get('path') or '').strip()
    mode = data.get('mode', 'copy')  # 'copy' (beside original, never clobbers) | 'inplace'
    if not relpath or not _valid_relpath(relpath):
        return err('Invalid path')
    if mode not in ('copy', 'inplace'):
        return err('Invalid restore mode')
    out, errtxt, rc = run([SNAP_FS_HELPER, 'restore', dataset, snapname, relpath, mode])
    if rc != 0:
        return err((errtxt or 'restore failed').strip()[:200], 400)
    try:
        return jsonify(json.loads(out))
    except json.JSONDecodeError:
        return jsonify({'success': True})

@bp.route('/api/zfs/pools/<name>/properties')
def zfs_pool_properties(name):
    if not RE_POOL.match(name):
        return err('Invalid pool name')
    out, _, _ = run(['zpool', 'get', 'all', name])
    props = {}
    for line in out.strip().split('\n')[1:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 3:
            props[parts[1]] = parts[2]
    return jsonify(props)

@bp.route('/api/zfs/datasets/<path:name>/properties')
def zfs_dataset_properties(name):
    if not RE_DATASET.match(name):
        return err('Invalid dataset name')
    out, _, _ = run(['zfs', 'get', 'all', name])
    props = {}
    for line in out.strip().split('\n')[1:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 3:
            props[parts[1]] = parts[2]
    return jsonify(props)

@bp.route('/api/zfs/datasets/<path:name>/properties', methods=['PUT'])
def zfs_dataset_set_property(name):
    data = request.get_json()
    prop = data.get('property', '').strip()
    value = data.get('value', '').strip()
    if not RE_DATASET.match(name):
        return err('Invalid dataset name')
    if not prop or not RE_PROP.match(prop):
        return err('Invalid property')
    return jsonify(run_safe(['zfs', 'set', f'{prop}={value}', name]))

# ─── iSCSI Target Management ─────────────────────────────────────────



# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'zfs', 'label': 'ZFS Pools', 'category': 'Storage MGMT',
          'blueprint': bp}
