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
from .disks import _walk, BOOT_MOUNTS

bp = Blueprint('lvm', __name__)

RE_LVM = re.compile(r'^[a-zA-Z0-9+_.][a-zA-Z0-9+_.-]*$')        # vg / lv names
RE_LVSIZE = re.compile(r'^\+?[0-9]+(\.[0-9]+)?[KkMmGgTtPp]?$')  # -L sizes (and +N to extend)
RE_LVPCT = re.compile(r'^[0-9]{1,3}%(FREE|VG)$')               # -l percentages


def _lvm_report(tool, fields):
    out, _, _ = run([tool, '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', fields])
    try:
        rep = json.loads(out).get('report', [])
    except json.JSONDecodeError:
        return []
    key = {'pvs': 'pv', 'vgs': 'vg', 'lvs': 'lv'}[tool]
    return rep[0].get(key, []) if rep else []


def _lv_mountpoint(path):
    return run(['findmnt', '-n', '-o', 'TARGET', path], no_sudo=True)[0].strip() if path else ''


def _lvm_mounted():
    """Sets of VGs and 'vg/lv' that currently back a mounted filesystem."""
    prot_vgs, prot_lvs = set(), set()
    for l in _lvm_report('lvs', 'lv_name,vg_name,lv_path'):
        if _lv_mountpoint(l.get('lv_path', '')):
            prot_vgs.add(l['vg_name'])
            prot_lvs.add(f"{l['vg_name']}/{l['lv_name']}")
    return prot_vgs, prot_lvs


def _standalone_pvs():
    """PV names that exist but are not yet in any VG."""
    return {p['pv_name'] for p in _lvm_report('pvs', 'pv_name,vg_name') if not p.get('vg_name')}


def _device_free_for_pv(dev):
    if not RE_DEVICE.match(dev):
        return False
    out, _, _ = run(['lsblk', '-J', '-o', 'NAME,TYPE,FSTYPE,MOUNTPOINT', dev])
    try:
        nodes = json.loads(out).get('blockdevices', [])
    except json.JSONDecodeError:
        return False
    if not nodes:
        return False
    for n in nodes:
        for x in _walk(n):
            if x.get('mountpoint') or x.get('mountpoint') in BOOT_MOUNTS:
                return False
            if x.get('fstype') in ('zfs_member', 'linux_raid_member', 'LVM2_member', 'swap'):
                return False
    return True


@bp.route('/api/lvm')
def lvm_overview():
    prot_vgs, _ = _lvm_mounted()
    lvs = []
    for l in _lvm_report('lvs', 'lv_name,vg_name,lv_size,lv_path,lv_attr'):
        mnt = _lv_mountpoint(l.get('lv_path', ''))
        lvs.append({'name': l['lv_name'], 'vg': l['vg_name'], 'size': _human_bytes(int(l['lv_size'])),
                    'path': l.get('lv_path', ''), 'mountpoint': mnt, 'protected': bool(mnt)})
    pvs = [{'name': p['pv_name'], 'vg': p.get('vg_name', ''),
            'size': _human_bytes(int(p['pv_size'])), 'free': _human_bytes(int(p['pv_free'])),
            'protected': p.get('vg_name') in prot_vgs}
           for p in _lvm_report('pvs', 'pv_name,vg_name,pv_size,pv_free')]
    vgs = [{'name': g['vg_name'], 'pv_count': int(g['pv_count']), 'lv_count': int(g['lv_count']),
            'size': _human_bytes(int(g['vg_size'])), 'free': _human_bytes(int(g['vg_free'])),
            'protected': g['vg_name'] in prot_vgs}
           for g in _lvm_report('vgs', 'vg_name,pv_count,lv_count,vg_size,vg_free')]
    return jsonify({'pvs': pvs, 'vgs': vgs, 'lvs': lvs})


# ── Physical volumes ──
@bp.route('/api/lvm/pv', methods=['POST'])
def lvm_pv_create():
    dev = (request.get_json() or {}).get('device', '').strip()
    if not _device_free_for_pv(dev):
        return err('Device is not a free block device')
    return jsonify(run_safe(['pvcreate', dev]))

@bp.route('/api/lvm/pv/resize', methods=['POST'])
def lvm_pv_resize():
    dev = (request.get_json() or {}).get('device', '').strip()
    if not RE_DEVICE.match(dev):
        return err('Invalid device')
    return jsonify(run_safe(['pvresize', dev]))

@bp.route('/api/lvm/pv/move', methods=['POST'])
def lvm_pv_move():
    data = request.get_json() or {}
    src = (data.get('source') or '').strip()
    dest = (data.get('dest') or '').strip()
    if not RE_DEVICE.match(src) or (dest and not RE_DEVICE.match(dest)):
        return err('Invalid device')
    prot_vgs, _ = _lvm_mounted()
    src_vg = next((p.get('vg_name') for p in _lvm_report('pvs', 'pv_name,vg_name') if p['pv_name'] == src), None)
    if src_vg in prot_vgs:
        return err('Refusing to move data off a PV in a mounted volume group', 409)
    return jsonify(run_safe(['pvmove'] + ([src, dest] if dest else [src])))

@bp.route('/api/lvm/pv/remove', methods=['POST'])
def lvm_pv_remove():
    dev = (request.get_json() or {}).get('device', '').strip()
    if not RE_DEVICE.match(dev):
        return err('Invalid device')
    in_vg = next((p.get('vg_name') for p in _lvm_report('pvs', 'pv_name,vg_name') if p['pv_name'] == dev), '')
    if in_vg:
        return err(f'PV is in volume group "{in_vg}" — remove it from the VG first', 409)
    return jsonify(run_safe(['pvremove', dev]))


# ── Volume groups ──
@bp.route('/api/lvm/vg', methods=['POST'])
def lvm_vg_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    devices = data.get('devices', [])
    if not RE_LVM.match(name):
        return err('Invalid VG name')
    standalone = _standalone_pvs()
    for d in devices:
        if not RE_DEVICE.match(d) or d not in standalone:
            return err(f'{d} is not an unused physical volume')
    if not devices:
        return err('Select at least one physical volume')
    return jsonify(run_safe(['vgcreate', name] + devices))

@bp.route('/api/lvm/vg/<name>/extend', methods=['POST'])
def lvm_vg_extend(name):
    dev = (request.get_json() or {}).get('device', '').strip()
    if not RE_LVM.match(name):
        return err('Invalid VG name')
    if dev not in _standalone_pvs():
        return err('Device is not an unused physical volume')
    return jsonify(run_safe(['vgextend', name, dev]))

@bp.route('/api/lvm/vg/<name>/reduce', methods=['POST'])
def lvm_vg_reduce(name):
    dev = (request.get_json() or {}).get('device', '').strip()
    if not RE_LVM.match(name) or not RE_DEVICE.match(dev):
        return err('Invalid VG or device')
    prot_vgs, _ = _lvm_mounted()
    if name in prot_vgs:
        return err('Refusing to alter a mounted volume group', 409)
    return jsonify(run_safe(['vgreduce', name, dev]))

@bp.route('/api/lvm/vg/<name>', methods=['DELETE'])
def lvm_vg_remove(name):
    if not RE_LVM.match(name):
        return err('Invalid VG name')
    prot_vgs, _ = _lvm_mounted()
    if name in prot_vgs:
        return err('Refusing to remove a volume group with mounted volumes', 409)
    return jsonify(run_safe(['vgremove', name]))  # no -f: refuses if LVs still exist


# ── Logical volumes ──
@bp.route('/api/lvm/lv', methods=['POST'])
def lvm_lv_create():
    data = request.get_json() or {}
    vg = (data.get('vg') or '').strip()
    name = (data.get('name') or '').strip()
    size = (data.get('size') or '').strip()
    fstype = (data.get('fstype') or '').strip()
    if not RE_LVM.match(vg) or not RE_LVM.match(name):
        return err('Invalid VG or LV name')
    if RE_LVPCT.match(size):
        cmd = ['lvcreate', '-l', size, '-n', name, vg]
    elif RE_LVSIZE.match(size):
        cmd = ['lvcreate', '-L', size, '-n', name, vg]
    else:
        return err('Invalid size (e.g. 10G or 100%FREE)')
    if fstype and fstype not in ('ext4', 'xfs'):
        return err('Unsupported filesystem')
    r = run_safe(cmd)
    if r['success'] and fstype:
        opt = '-F' if fstype == 'ext4' else '-f'
        run_safe([f'mkfs.{fstype}', opt, f'/dev/{vg}/{name}'])
    return jsonify(r)

@bp.route('/api/lvm/lv/<vg>/<name>/extend', methods=['POST'])
def lvm_lv_extend(vg, name):
    data = request.get_json() or {}
    size = (data.get('size') or '').strip()
    if not RE_LVM.match(vg) or not RE_LVM.match(name):
        return err('Invalid VG or LV name')
    if RE_LVPCT.match(size):
        cmd = ['lvextend', '-l', size]
    elif RE_LVSIZE.match(size):
        cmd = ['lvextend', '-L', size]
    else:
        return err('Invalid size (e.g. +10G, 50G, or 100%FREE)')
    if data.get('resize_fs'):
        cmd.append('-r')  # grow the filesystem too
    cmd.append(f'{vg}/{name}')
    return jsonify(run_safe(cmd))  # extend (grow) only — never shrinks

@bp.route('/api/lvm/lv/<vg>/<name>', methods=['DELETE'])
def lvm_lv_remove(vg, name):
    if not RE_LVM.match(vg) or not RE_LVM.match(name):
        return err('Invalid VG or LV name')
    _, prot_lvs = _lvm_mounted()
    if f'{vg}/{name}' in prot_lvs:
        return err('Refusing to remove a mounted logical volume', 409)
    return jsonify(run_safe(['lvremove', '-y', f'{vg}/{name}']))


# ─── MD RAID (mdadm) management ──────────────────────────────────────
# Members can only be FREE disks; arrays backing a mounted FS / pool / LVM are
# protected from stop/delete. Created arrays are persisted to mdadm.conf.



# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'lvm', 'label': 'LVM', 'category': 'Storage MGMT',
          'blueprint': bp}
