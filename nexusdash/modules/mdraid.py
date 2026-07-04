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
from .disks import _walk
from .lvm import _device_free_for_pv

bp = Blueprint('mdraid', __name__)

RE_MDDEV = re.compile(r'^md\d+$')
RE_MDNAME = re.compile(r'^[a-zA-Z0-9_.-]+$')
MD_MIN_DEVICES = {'0': 2, '1': 2, '5': 3, '6': 4, '10': 2}


def _md_list_devs():
    devs = []
    try:
        with open('/proc/mdstat') as f:
            for line in f:
                m = re.match(r'^(md\d+)\s*:', line)
                if m:
                    devs.append(m.group(1))
    except FileNotFoundError:
        pass
    return devs


def parse_mdadm_detail(out):
    info = {'level': '', 'size': '', 'state': '', 'raid_devices': '', 'active': '',
            'failed': '', 'spare': '', 'sync': '', 'devices': []}
    keymap = {'Raid Level': 'level', 'Array Size': 'size', 'State': 'state',
              'Raid Devices': 'raid_devices', 'Active Devices': 'active',
              'Failed Devices': 'failed', 'Spare Devices': 'spare'}
    for line in out.split('\n'):
        s = line.strip()
        if ':' in s:
            k, v = s.split(':', 1)
            k, v = k.strip(), v.strip()
            if k in keymap:
                info[keymap[k]] = v.split('(')[0].strip() if k == 'Array Size' else v
            elif k in ('Rebuild Status', 'Resync Status', 'Check Status'):
                info['sync'] = f'{k.split()[0]}: {v}'
        parts = s.split()
        if len(parts) >= 5 and parts[0].isdigit() and parts[-1].startswith('/dev/'):
            info['devices'].append({'number': parts[0], 'state': ' '.join(parts[4:-1]), 'device': parts[-1]})
    return info


def _md_protected(dev):
    out, _, _ = run(['lsblk', '-J', '-o', 'NAME,FSTYPE,MOUNTPOINT', f'/dev/{dev}'])
    try:
        nodes = json.loads(out).get('blockdevices', [])
    except json.JSONDecodeError:
        return False
    for n in nodes:
        for x in _walk(n):
            if x.get('mountpoint') or x.get('fstype') in ('zfs_member', 'LVM2_member', 'swap'):
                return True
    return False


def _md_sync_conf(persist=True):
    """Rebuild the ARRAY lines in mdadm.conf from the live arrays (best effort)."""
    scan = run(['mdadm', '--detail', '--scan'])[0]
    array_lines = [l.strip() for l in scan.split('\n') if l.strip().startswith('ARRAY')]
    try:
        with open(MDADM_CONF) as f:
            kept = [l for l in f.read().split('\n') if not l.strip().startswith('ARRAY')]
    except FileNotFoundError:
        kept = []
    content = '\n'.join(kept).rstrip('\n') + '\n' + '\n'.join(array_lines) + '\n'
    run_safe(['tee', MDADM_CONF], input_data=content)
    if persist:
        run(INITRAMFS_UPDATE)  # best effort; for boot-time assembly


@bp.route('/api/mdadm/arrays')
def mdadm_arrays():
    arrays = []
    for dev in _md_list_devs():
        info = parse_mdadm_detail(run(['mdadm', '--detail', f'/dev/{dev}'])[0])
        info['device'] = dev
        info['protected'] = _md_protected(dev)
        arrays.append(info)
    return jsonify({'arrays': arrays})


@bp.route('/api/mdadm/arrays', methods=['POST'])
def mdadm_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    level = str(data.get('level', '1')).strip()
    devices = data.get('devices', [])
    spares = data.get('spares', [])
    persist = data.get('persist', True)
    if not RE_MDNAME.match(name):
        return err('Invalid array name')
    if level not in MD_MIN_DEVICES:
        return err('Invalid RAID level')
    for d in list(devices) + list(spares):
        if not _device_free_for_pv(d):
            return err(f'{d} is not a free disk')
    if len(devices) < MD_MIN_DEVICES[level]:
        return err(f'RAID{level} needs at least {MD_MIN_DEVICES[level]} devices')
    # Ensure the RAID personality is loaded (this host boots with none).
    run(['modprobe', {'0': 'raid0', '1': 'raid1', '5': 'raid456', '6': 'raid456', '10': 'raid10'}[level]])
    cmd = ['mdadm', '--create', f'/dev/md/{name}', '--run', f'--level={level}',
           f'--raid-devices={len(devices)}']
    if spares:
        cmd.append(f'--spare-devices={len(spares)}')
    cmd += list(devices) + list(spares)
    r = run_safe(cmd, input_data='y\n')
    if r['success']:
        _md_sync_conf(persist)
    return jsonify(r)


@bp.route('/api/mdadm/arrays/<dev>')
def mdadm_detail(dev):
    if not RE_MDDEV.match(dev):
        return err('Invalid array')
    info = parse_mdadm_detail(run(['mdadm', '--detail', f'/dev/{dev}'])[0])
    info['device'] = dev
    return jsonify(info)


@bp.route('/api/mdadm/arrays/<dev>/device', methods=['POST'])
def mdadm_device(dev):
    if not RE_MDDEV.match(dev):
        return err('Invalid array')
    data = request.get_json() or {}
    action = data.get('action')
    device = (data.get('device') or '').strip()
    if action not in ('add', 'remove', 'fail'):
        return err('Invalid action')
    if not RE_DEVICE.match(device):
        return err('Invalid device')
    if action == 'add' and not _device_free_for_pv(device):
        return err('Device is not free (wipe it first to re-add)')
    flag = {'add': '--add', 'remove': '--remove', 'fail': '--fail'}[action]
    r = run_safe(['mdadm', '--manage', f'/dev/{dev}', flag, device])
    if r['success'] and action in ('add', 'remove'):
        _md_sync_conf(persist=False)
    return jsonify(r)


@bp.route('/api/mdadm/arrays/<dev>/stop', methods=['POST'])
def mdadm_stop(dev):
    if not RE_MDDEV.match(dev):
        return err('Invalid array')
    if _md_protected(dev):
        return err('Refusing to stop an array that is in use', 409)
    return jsonify(run_safe(['mdadm', '--stop', f'/dev/{dev}']))


@bp.route('/api/mdadm/assemble', methods=['POST'])
def mdadm_assemble():
    return jsonify(run_safe(['mdadm', '--assemble', '--scan']))


@bp.route('/api/mdadm/arrays/<dev>', methods=['DELETE'])
def mdadm_delete(dev):
    if not RE_MDDEV.match(dev):
        return err('Invalid array')
    if _md_protected(dev):
        return err('Refusing to delete an array that is in use', 409)
    members = [d['device'] for d in parse_mdadm_detail(run(['mdadm', '--detail', f'/dev/{dev}'])[0])['devices']]
    run_safe(['mdadm', '--stop', f'/dev/{dev}'])
    for m in members:
        if RE_DEVICE.match(m):
            run(['mdadm', '--zero-superblock', m])
    _md_sync_conf()
    return jsonify({'success': True})


# ─── Network Info ─────────────────────────────────────────────────────

@bp.route('/api/network')
def api_network():
    out, _, rc = run(['ip', '-j', 'addr', 'show'])
    if rc != 0:
        out, _, _ = run(['ip', 'addr', 'show'])
        return jsonify({'interfaces': [], 'raw': out})
    try:
        data = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        data = []
    return jsonify({'interfaces': data})




# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'mdraid', 'label': 'MD RAID', 'category': 'Storage MGMT',
          'blueprint': bp}
