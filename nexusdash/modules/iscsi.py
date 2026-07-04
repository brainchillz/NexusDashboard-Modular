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

bp = Blueprint('iscsi', __name__)

def targetcli(*cmd_args):
    return run_safe(['targetcli', *cmd_args])


def tmutate(*cmd_args):
    """Run a mutating targetcli command and persist the config on success. LIO
    config is in-memory until `saveconfig`, so without this a `target.service`
    restart loses everything."""
    r = run_safe(['targetcli', *cmd_args])
    if r['success']:
        run(['targetcli', 'saveconfig'])
    return r


def parse_tpg(output):
    """Parse `targetcli /iscsi/<iqn>/tpg1 ls` into luns / acls / portals. Items
    sit exactly one tree level (2 columns) below their section header, which
    excludes nested entries like mapped_lunN."""
    res = {'luns': [], 'acls': [], 'portals': []}
    section = None
    base_col = None
    for line in output.split('\n'):
        idx = line.find('o- ')
        if idx == -1:
            continue
        rest = line[idx + 3:].strip()
        name = rest.split()[0] if rest else ''
        low = name.lower()
        if low in ('luns', 'acls', 'portals'):
            section, base_col = low, idx
            continue
        if not section or base_col is None or idx != base_col + 2:
            continue
        if section == 'luns':
            bs = rest[rest.find('[') + 1:rest.find(']')] if '[' in rest and ']' in rest else ''
            res['luns'].append({'lun': name, 'backstore': bs})
        elif section == 'acls':
            res['acls'].append({'initiator': name})
        elif section == 'portals':
            if name.startswith('['):  # IPv6 e.g. [::0]:3260
                ip, port = name[1:name.rfind(']')], name[name.rfind(':') + 1:]
            else:
                ip, port = name.rsplit(':', 1) if ':' in name else (name, '')
            res['portals'].append({'ip': ip, 'port': port, 'portal': name})
    return res


def parse_targets(output):
    """Extract target IQNs from `targetcli /iscsi ls`. Target rows are the only
    ones tagged with [TPGs:], which avoids picking up ACL initiator IQNs."""
    targets = []
    for line in output.split('\n'):
        if '[TPGs:' not in line:
            continue
        idx = line.find('o- ')
        if idx == -1:
            continue
        rest = line[idx + 3:].strip().split()
        if rest and rest[0].startswith('iqn'):
            targets.append(rest[0])
    return targets


def parse_backstores(output):
    """Extract backstore objects from `targetcli /backstores ls` with size and
    in-use status. Each object sits exactly one tree level (2 columns) below its
    type header, which distinguishes it from nested alua entries. The bracket
    looks like: [/path (64.0MiB) write-back activated]."""
    backstores = []
    types = {'block', 'fileio', 'pscsi', 'ramdisk'}
    base_col = None
    cur_type = None
    for line in output.split('\n'):
        idx = line.find('o- ')
        if idx == -1:
            continue
        rest = line[idx + 3:].strip()
        name = rest.split()[0] if rest else ''
        if name in types:
            cur_type, base_col = name, idx
        elif cur_type and base_col is not None and idx == base_col + 2:
            bracket = rest[rest.find('[') + 1:rest.rfind(']')] if '[' in rest and ']' in rest else ''
            size = ''
            m = re.search(r'\(([\d.]+\s*[KMGTP]?i?B)\)', bracket)
            if m:
                size = m.group(1)
            backstores.append({
                'type': cur_type, 'name': name, 'size': size,
                'in_use': 'activated' in bracket and 'deactivated' not in bracket,
            })
    return backstores

@bp.route('/api/iscsi/status')
def iscsi_status():
    out, _, rc = run(['targetcli', '/iscsi', 'ls'])
    if rc != 0:
        out = 'NO CONFIG'
    out2, _, rc2 = run(['targetcli', '/backstores', 'ls'])
    if rc2 != 0:
        out2 = 'NO CONFIG'
    return jsonify({'targets': out, 'backstores': out2})

@bp.route('/api/iscsi/targets')
def iscsi_targets():
    out, _, rc = run(['targetcli', '/iscsi', 'ls'])
    if rc != 0:
        return jsonify({'targets': [], 'raw': ''})
    return jsonify({'targets': parse_targets(out), 'raw': out})

def _set_shared_mode(iqn):
    # Any initiator may connect and read/write the shared LUNs - the usual
    # default for clustered hypervisor storage (Proxmox / VMware).
    return tmutate(f'/iscsi/{iqn}/tpg1', 'set', 'attribute',
                   'generate_node_acls=1', 'demo_mode_write_protect=0', 'cache_dynamic_acls=1')


def _set_restricted_mode(iqn):
    # Only explicitly-added initiator ACLs may connect (optionally with CHAP).
    return tmutate(f'/iscsi/{iqn}/tpg1', 'set', 'attribute', 'generate_node_acls=0')


@bp.route('/api/iscsi/targets', methods=['POST'])
def iscsi_target_create():
    data = request.get_json()
    iqn = data.get('iqn', '').strip()
    access_mode = data.get('access_mode', 'shared')
    if not iqn or not RE_IQN.match(iqn):
        return err('Invalid IQN')
    if access_mode not in ('shared', 'restricted'):
        return err('Invalid access mode')
    r = tmutate('/iscsi', 'create', iqn)
    if not r['success']:
        return jsonify(r)
    _set_shared_mode(iqn) if access_mode == 'shared' else _set_restricted_mode(iqn)
    return jsonify(r)

@bp.route('/api/iscsi/targets/<path:iqn>', methods=['DELETE'])
def iscsi_target_destroy(iqn):
    if not RE_IQN.match(iqn):
        return err('Invalid IQN')
    return jsonify(tmutate('/iscsi', 'delete', iqn))

@bp.route('/api/iscsi/targets/<path:iqn>', methods=['GET'])
def iscsi_target_detail(iqn):
    if not RE_IQN.match(iqn):
        return err('Invalid IQN')
    out, _, rc = run(['targetcli', f'/iscsi/{iqn}/tpg1', 'ls'])
    if rc != 0:
        return err('Target not found', 404)
    detail = parse_tpg(out)
    attr_out, _, _ = run(['targetcli', f'/iscsi/{iqn}/tpg1', 'get', 'attribute',
                          'generate_node_acls', 'demo_mode_write_protect', 'authentication'])
    attrs = dict(t.split('=', 1) for t in attr_out.split() if '=' in t)
    detail['attributes'] = attrs
    detail['shared'] = attrs.get('generate_node_acls') == '1'
    detail['auth'] = attrs.get('authentication') == '1'
    detail['raw'] = out
    return jsonify(detail)

@bp.route('/api/iscsi/targets/<path:iqn>/mode', methods=['POST'])
def iscsi_target_mode(iqn):
    if not RE_IQN.match(iqn):
        return err('Invalid IQN')
    mode = (request.get_json() or {}).get('mode', '')
    if mode == 'shared':
        return jsonify(_set_shared_mode(iqn))
    if mode == 'restricted':
        return jsonify(_set_restricted_mode(iqn))
    return err('Invalid access mode')

@bp.route('/api/iscsi/backstores')
def iscsi_backstores():
    out, _, _ = run(['targetcli', '/backstores', 'ls'])
    return jsonify({'backstores': parse_backstores(out), 'raw': out})

@bp.route('/api/iscsi/backstores', methods=['POST'])
def iscsi_backstore_create():
    data = request.get_json()
    btype = data.get('type', 'fileio')
    name = data.get('name', '').strip()
    path = data.get('path', '').strip()
    size = str(data.get('size', '')).strip()
    if not name or not RE_BSNAME.match(name):
        return err('Invalid backstore name')
    if btype not in ('fileio', 'block'):
        return err(f'Unknown backstore type: {btype}')
    if not path or not RE_PATH.match(path):
        return err('Invalid path')
    if btype == 'fileio':
        cmd = ['/backstores/fileio', 'create', name, path]
        if size:
            if not RE_SIZE.match(size):
                return err('Invalid size')
            cmd.append(size)
    else:  # block
        cmd = ['/backstores/block', 'create', name, path]
    return jsonify(tmutate(*cmd))

@bp.route('/api/iscsi/backstores/<btype>/<name>', methods=['DELETE'])
def iscsi_backstore_delete(btype, name):
    if btype not in ('fileio', 'block'):
        return err('Unknown backstore type')
    if not RE_BSNAME.match(name):
        return err('Invalid backstore name')
    return jsonify(tmutate(f'/backstores/{btype}', 'delete', name))

@bp.route('/api/iscsi/luns', methods=['POST'])
def iscsi_lun_create():
    data = request.get_json()
    iqn = data.get('iqn', '').strip()
    backstore_type = data.get('backstore_type', 'fileio')
    backstore_name = data.get('backstore_name', '').strip()
    lun_id = str(data.get('lun_id', '')).strip()
    if not iqn or not RE_IQN.match(iqn):
        return err('Invalid IQN')
    if backstore_type not in ('fileio', 'block'):
        return err('Unknown backstore type')
    if not backstore_name or not RE_BSNAME.match(backstore_name):
        return err('Invalid backstore name')
    cmd = [f'/iscsi/{iqn}/tpg1/luns', 'create', f'/backstores/{backstore_type}/{backstore_name}']
    if lun_id:
        if not RE_NUM.match(lun_id):
            return err('Invalid LUN id')
        cmd.append(lun_id)
    return jsonify(tmutate(*cmd))

@bp.route('/api/iscsi/luns/delete', methods=['POST'])
def iscsi_lun_delete():
    data = request.get_json() or {}
    iqn = data.get('iqn', '').strip()
    lun = data.get('lun', '').strip()
    if not RE_IQN.match(iqn):
        return err('Invalid target IQN')
    if not re.match(r'^lun[0-9]+$', lun):
        return err('Invalid LUN')
    return jsonify(tmutate(f'/iscsi/{iqn}/tpg1/luns', 'delete', lun))

@bp.route('/api/iscsi/acls', methods=['POST'])
def iscsi_acl_create():
    data = request.get_json()
    iqn = data.get('iqn', '').strip()
    initiator_iqn = data.get('initiator_iqn', '').strip()
    if not iqn or not RE_IQN.match(iqn):
        return err('Invalid target IQN')
    if not initiator_iqn or not RE_IQN.match(initiator_iqn):
        return err('Invalid initiator IQN')
    return jsonify(tmutate(f'/iscsi/{iqn}/tpg1/acls', 'create', initiator_iqn))

@bp.route('/api/iscsi/acls/delete', methods=['POST'])
def iscsi_acl_delete():
    data = request.get_json() or {}
    iqn = data.get('iqn', '').strip()
    initiator_iqn = data.get('initiator_iqn', '').strip()
    if not RE_IQN.match(iqn) or not RE_IQN.match(initiator_iqn):
        return err('Invalid IQN')
    return jsonify(tmutate(f'/iscsi/{iqn}/tpg1/acls', 'delete', initiator_iqn))

@bp.route('/api/iscsi/acls/chap', methods=['POST'])
def iscsi_acl_chap():
    data = request.get_json() or {}
    iqn = data.get('iqn', '').strip()
    initiator_iqn = data.get('initiator_iqn', '').strip()
    if not RE_IQN.match(iqn) or not RE_IQN.match(initiator_iqn):
        return err('Invalid IQN')
    acl = f'/iscsi/{iqn}/tpg1/acls/{initiator_iqn}'
    if data.get('clear'):
        tmutate(acl, 'set', 'auth', 'userid=', 'password=')
        return jsonify({'success': True})
    userid = (data.get('userid') or '').strip()
    password = (data.get('password') or '').strip()
    if not RE_CHAP.match(userid) or not RE_CHAP.match(password):
        return err('Invalid CHAP userid/password (use letters, digits, . _ : + -)')
    tmutate(f'/iscsi/{iqn}/tpg1', 'set', 'attribute', 'authentication=1')
    return jsonify(tmutate(acl, 'set', 'auth', f'userid={userid}', f'password={password}'))

@bp.route('/api/iscsi/portals', methods=['POST'])
def iscsi_portal_create():
    data = request.get_json()
    iqn = data.get('iqn', '').strip()
    ip = str(data.get('ip', '0.0.0.0')).strip()
    port = str(data.get('port', '3260')).strip()
    if not iqn or not RE_IQN.match(iqn):
        return err('Invalid IQN')
    if not RE_IP.match(ip):
        return err('Invalid IP address')
    if not RE_NUM.match(port):
        return err('Invalid port')
    return jsonify(tmutate(f'/iscsi/{iqn}/tpg1/portals', 'create', ip, port))

@bp.route('/api/iscsi/portals/delete', methods=['POST'])
def iscsi_portal_delete():
    data = request.get_json() or {}
    iqn = data.get('iqn', '').strip()
    ip = str(data.get('ip', '')).strip()
    port = str(data.get('port', '')).strip()
    if not RE_IQN.match(iqn):
        return err('Invalid IQN')
    if not RE_IP.match(ip) or not RE_NUM.match(port):
        return err('Invalid portal')
    return jsonify(tmutate(f'/iscsi/{iqn}/tpg1/portals', 'delete', ip, port))

@bp.route('/api/iscsi/sessions')
def iscsi_sessions():
    # targetcli's `sessions` doesn't report demo-mode dynamic sessions, so read
    # connected initiators from configfs via a root-owned helper.
    out, _, _ = run([HELPER_PREFIX + '-iscsi-sessions'])
    sessions = []
    for line in out.strip().split('\n'):
        parts = line.split('\t')
        if len(parts) >= 2:
            sessions.append({'target': parts[0], 'initiator': parts[1],
                             'type': parts[2] if len(parts) > 2 else ''})
    return jsonify({'sessions': sessions})

@bp.route('/api/iscsi/saveconfig', methods=['POST'])
def iscsi_saveconfig():
    return jsonify(targetcli('saveconfig'))

# ─── NFS Export Management ───────────────────────────────────────────



# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'iscsi', 'label': 'iSCSI Targets', 'category': 'Sharing',
          'blueprint': bp}
