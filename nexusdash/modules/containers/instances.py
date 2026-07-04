"""Nexus Containers — extracted from the LXD-Console app.py (Stage 3 port).
Logic unchanged except: shared core imports, RE_POOL/RE_DEVNAME renamed to
RE_CT_* (the dashboard owns those names with different patterns), and the
LXDWEB_SOCKET env override renamed DASHBOARD_LXD_SOCKET."""
import os
import re
import json
import time
import socket
import threading
import http.client
import urllib.request
import urllib.error
from datetime import datetime
from flask import Blueprint, jsonify, request, g, Response

from ...core.config import APP_DIR, write_json_atomic
from ...core.runcmd import run, run_safe, err
from ...core.registry import load_disabled_modules
from .client import (
    DAEMON, SOCKET_PATH, DEFAULT_IMAGE_REMOTE, IMAGE_REMOTES, IMAGE_REMOTE_URLS,
    RE_INSTANCE, RE_CT_NETWORK, RE_CT_POOL, RE_PROFILE, RE_PROJECT,
    RE_IMAGE_ALIAS, RE_CT_DEVNAME, RE_SNAPNAME, RE_FINGERPRINT, RE_PROXY_ADDR,
    INSTANCE_TYPES, STATE_ACTIONS, CONFIG_EDIT_KEYS, _CT_FTYPES, _VM_FTYPES,
    _ARCH_MAP, valid_instance_name, LxdError, _UnixHTTPConnection,
    lxd_raw, lxd_request, _wait_operation, _lxd_error_response, _host_arch,
    _nic_device_for,
)

bp = Blueprint('instances', __name__)

@bp.route('/api/server')
def api_server():
    try:
        meta = lxd_request('GET', '/1.0')
    except LxdError as e:
        return jsonify({'reachable': False, 'daemon': DAEMON, 'socket': SOCKET_PATH,
                        'error': e.message})
    env = meta.get('environment', {})
    # Presence of a storage pool / managed network drives an "initialize" hint.
    try:
        pools = lxd_request('GET', '/1.0/storage-pools')
    except LxdError:
        pools = []
    try:
        nets = lxd_request('GET', '/1.0/networks')
    except LxdError:
        nets = []
    return jsonify({
        'reachable': True,
        'daemon': DAEMON,
        'socket': SOCKET_PATH,
        'auth': meta.get('auth'),
        'server_version': env.get('server_version'),
        'kernel': env.get('kernel_version'),
        'arch': _host_arch(),
        'architectures': env.get('architectures', []),
        'server_name': env.get('server_name'),
        'storage_pools': [p.rsplit('/', 1)[-1] for p in pools],
        'has_storage': bool(pools),
        'network_count': len(nets),
        'default_image_remote': DEFAULT_IMAGE_REMOTE,
    })


# ═══════════════════════════════════════════════════════════════════════
#  Instances
# ═══════════════════════════════════════════════════════════════════════

def _instance_addresses(state):
    """Extract global-scope IPv4/IPv6 addresses from an instance state object."""
    v4, v6 = [], []
    for iface, info in (state.get('network') or {}).items():
        if iface == 'lo':
            continue
        for addr in info.get('addresses', []):
            if addr.get('scope') != 'global':
                continue
            if addr.get('family') == 'inet':
                v4.append(addr.get('address'))
            elif addr.get('family') == 'inet6':
                v6.append(addr.get('address'))
    return v4, v6


def _instance_summary(inst):
    state = inst.get('state') or {}
    v4, v6 = _instance_addresses(state)
    cfg = inst.get('config') or {}
    return {
        'name': inst.get('name'),
        'type': inst.get('type', 'container'),
        'status': inst.get('status') or state.get('status'),
        'status_code': inst.get('status_code') or state.get('status_code'),
        'architecture': inst.get('architecture'),
        'ephemeral': inst.get('ephemeral', False),
        'profiles': inst.get('profiles', []),
        'created_at': inst.get('created_at'),
        'last_used_at': inst.get('last_used_at'),
        'description': inst.get('description') or cfg.get('image.description', ''),
        'os': cfg.get('image.os', ''),
        'release': cfg.get('image.release', ''),
        'ipv4': v4,
        'ipv6': v6,
        'memory': (state.get('memory') or {}).get('usage'),
        'processes': state.get('processes'),
    }


@bp.route('/api/instances')
def instances_list():
    try:
        # recursion=2 returns each instance with its live state in one call.
        insts = lxd_request('GET', '/1.0/instances?recursion=2')
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify([_instance_summary(i) for i in insts])


@bp.route('/api/instances/<name>')
def instance_detail(name):
    if not valid_instance_name(name):
        return err('Invalid instance name')
    try:
        inst = lxd_request('GET', f'/1.0/instances/{name}')
        state = lxd_request('GET', f'/1.0/instances/{name}/state')
        try:
            snaps = lxd_request('GET', f'/1.0/instances/{name}/snapshots?recursion=1')
        except LxdError:
            snaps = []
        try:
            backups = lxd_request('GET', f'/1.0/instances/{name}/backups?recursion=1')
        except LxdError:
            backups = []
    except LxdError as e:
        return _lxd_error_response(e)
    inst['state'] = state
    summary = _instance_summary(inst)
    summary.update({
        'config': inst.get('config', {}),
        'devices': inst.get('devices', {}),
        'expanded_devices': inst.get('expanded_devices', {}),
        'state': state,
        'snapshots': [{'name': s.get('name'), 'created_at': s.get('created_at'),
                       'stateful': s.get('stateful', False)} for s in snaps],
        'backups': [{'name': b.get('name'), 'created_at': b.get('created_at')} for b in backups],
    })
    return jsonify(summary)


@bp.route('/api/instances', methods=['POST'])
def instance_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    itype = data.get('type', 'container')
    alias = (data.get('alias') or '').strip()
    server = (data.get('server') or DEFAULT_IMAGE_REMOTE).strip()
    profiles = data.get('profiles') or ['default']
    pool = (data.get('pool') or '').strip()
    network = (data.get('network') or '').strip()
    config = data.get('config') or {}
    start = bool(data.get('start', True))

    if not valid_instance_name(name):
        return err('Invalid instance name (letters/digits/hyphen, must start with a letter)')
    if itype not in INSTANCE_TYPES:
        return err('Invalid instance type')
    if not RE_IMAGE_ALIAS.match(alias):
        return err('Invalid or missing image alias')
    if server not in IMAGE_REMOTE_URLS:
        return err('Unknown image server')
    if not all(RE_PROFILE.match(p) for p in profiles):
        return err('Invalid profile name')
    if pool and not RE_CT_POOL.match(pool):
        return err('Invalid storage pool')
    if network and not RE_CT_NETWORK.match(network):
        return err('Invalid network name')
    # Only allow a known-safe subset of config keys (limits + a couple flags).
    safe_config = {}
    for k, v in config.items():
        if k in ('limits.cpu', 'limits.memory', 'security.nesting',
                 'security.privileged', 'boot.autostart') and isinstance(v, (str, int, bool)):
            safe_config[k] = str(v)

    body = {
        'name': name,
        'type': itype,
        'profiles': profiles,
        'config': safe_config,
        'source': {'type': 'image', 'protocol': 'simplestreams',
                   'server': server, 'alias': alias},
    }
    devices = {}
    if pool:
        devices['root'] = {'type': 'disk', 'path': '/', 'pool': pool}
    if network:
        try:
            devices['eth0'] = _nic_device_for(network, 'eth0')
        except LxdError as e:
            return _lxd_error_response(e)
    if devices:
        body['devices'] = devices
    try:
        lxd_request('POST', '/1.0/instances', body, wait=True)
        if start:
            lxd_request('PUT', f'/1.0/instances/{name}/state',
                        {'action': 'start', 'timeout': 60}, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True, 'name': name})


@bp.route('/api/instances/<name>/state', methods=['PUT'])
def instance_state(name):
    if not valid_instance_name(name):
        return err('Invalid instance name')
    data = request.get_json() or {}
    action = data.get('action')
    if action not in STATE_ACTIONS:
        return err('Invalid action')
    body = {'action': action, 'timeout': int(data.get('timeout', 60)),
            'force': bool(data.get('force', False)),
            'stateful': bool(data.get('stateful', False))}
    try:
        lxd_request('PUT', f'/1.0/instances/{name}/state', body, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/instances/<name>', methods=['DELETE'])
def instance_delete(name):
    if not valid_instance_name(name):
        return err('Invalid instance name')
    force = request.args.get('force') in ('1', 'true', 'yes')
    try:
        if force:
            # Stop first (ignore "already stopped" errors) so a running instance
            # can be removed in one click.
            try:
                lxd_request('PUT', f'/1.0/instances/{name}/state',
                            {'action': 'stop', 'timeout': 30, 'force': True}, wait=True)
            except LxdError:
                pass
        lxd_request('DELETE', f'/1.0/instances/{name}', wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


# ─── Snapshots ────────────────────────────────────────────────────────

@bp.route('/api/instances/<name>/snapshots', methods=['POST'])
def snapshot_create(name):
    if not valid_instance_name(name):
        return err('Invalid instance name')
    data = request.get_json() or {}
    snap = (data.get('name') or '').strip()
    if not RE_SNAPNAME.match(snap):
        return err('Invalid snapshot name')
    body = {'name': snap, 'stateful': bool(data.get('stateful', False))}
    try:
        lxd_request('POST', f'/1.0/instances/{name}/snapshots', body, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/instances/<name>/snapshots/<snap>/restore', methods=['POST'])
def snapshot_restore(name, snap):
    if not valid_instance_name(name) or not RE_SNAPNAME.match(snap):
        return err('Invalid name')
    try:
        lxd_request('PUT', f'/1.0/instances/{name}', {'restore': snap}, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/instances/<name>/snapshots/<snap>', methods=['DELETE'])
def snapshot_delete(name, snap):
    if not valid_instance_name(name) or not RE_SNAPNAME.match(snap):
        return err('Invalid name')
    try:
        lxd_request('DELETE', f'/1.0/instances/{name}/snapshots/{snap}', wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


# ─── Config / limits editing, rename, copy ────────────────────────────

@bp.route('/api/instances/<name>/config', methods=['PATCH'])
def instance_config_edit(name):
    if not valid_instance_name(name):
        return err('Invalid instance name')
    data = request.get_json() or {}
    cfg = {k: ('' if v is None else str(v)) for k, v in (data.get('config') or {}).items()
           if k in CONFIG_EDIT_KEYS}
    if not cfg:
        return err('No editable config keys supplied')
    try:
        lxd_request('PATCH', f'/1.0/instances/{name}', {'config': cfg}, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/instances/<name>/rename', methods=['POST'])
def instance_rename(name):
    if not valid_instance_name(name):
        return err('Invalid instance name')
    new = ((request.get_json() or {}).get('new_name') or '').strip()
    if not valid_instance_name(new):
        return err('Invalid new name')
    try:
        # Rename is a POST with the new name; the instance must be stopped.
        lxd_request('POST', f'/1.0/instances/{name}', {'name': new}, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True, 'name': new})


@bp.route('/api/instances/<name>/copy', methods=['POST'])
def instance_copy(name):
    if not valid_instance_name(name):
        return err('Invalid instance name')
    data = request.get_json() or {}
    new = (data.get('new_name') or '').strip()
    if not valid_instance_name(new):
        return err('Invalid new name')
    body = {'name': new, 'source': {'type': 'copy', 'source': name}}
    try:
        lxd_request('POST', '/1.0/instances', body, wait=True)
        # The raw API copy preserves volatile MAC(s); strip them so a running copy
        # doesn't collide with the source (mirrors what the `lxc copy` CLI does).
        ni = lxd_request('GET', f'/1.0/instances/{new}')
        hw = [k for k in (ni.get('config') or {}) if k.startswith('volatile.') and k.endswith('.hwaddr')]
        if hw:
            for k in hw:
                ni['config'].pop(k, None)
            lxd_request('PUT', f'/1.0/instances/{new}', ni, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True, 'name': new})


# ─── Generic device removal (used by proxy port-forwards, etc.) ───────

@bp.route('/api/instances/<name>/device/<dev>', methods=['DELETE'])
def instance_device_remove(name, dev):
    if not valid_instance_name(name) or not RE_CT_DEVNAME.match(dev):
        return err('Invalid name')
    try:
        inst = lxd_request('GET', f'/1.0/instances/{name}')
        if dev not in (inst.get('devices') or {}):
            return err('No such device on this instance', 404)
        inst['devices'].pop(dev, None)
        # Device removal needs the full object via PUT (PATCH cannot delete keys).
        lxd_request('PUT', f'/1.0/instances/{name}', inst, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


# ─── Port forwarding (LXD proxy devices) ──────────────────────────────

@bp.route('/api/instances/<name>/export', methods=['POST'])
def instance_export(name):
    """Create a backup and return a download URL. instance_only omits snapshots."""
    if not valid_instance_name(name):
        return err('Invalid instance name')
    data = request.get_json() or {}
    bname = 'export-' + datetime.now().strftime('%Y%m%d-%H%M%S')
    body = {'name': bname, 'instance_only': bool(data.get('instance_only', True)),
            'optimized_storage': False, 'compression_algorithm': 'gzip'}
    try:
        lxd_request('POST', f'/1.0/instances/{name}/backups', body, wait=True, wait_timeout=1800)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True, 'backup': bname,
                    'download': f'/api/instances/{name}/backups/{bname}/download'})


@bp.route('/api/instances/<name>/backups/<bak>/download')
def backup_download(name, bak):
    if not valid_instance_name(name) or not RE_SNAPNAME.match(bak):
        return err('Invalid name')

    def generate():
        conn = _UnixHTTPConnection(SOCKET_PATH, timeout=1800)
        conn.request('GET', f'/1.0/instances/{name}/backups/{bak}/export',
                     headers={'Host': 'lxd'})
        resp = conn.getresponse()
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            conn.close()
    return Response(generate(), mimetype='application/octet-stream', headers={
        'Content-Disposition': f'attachment; filename="{name}-{bak}.tar.gz"'})


@bp.route('/api/instances/<name>/backups/<bak>', methods=['DELETE'])
def backup_delete(name, bak):
    if not valid_instance_name(name) or not RE_SNAPNAME.match(bak):
        return err('Invalid name')
    try:
        lxd_request('DELETE', f'/1.0/instances/{name}/backups/{bak}', wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/instances/import', methods=['POST'])
def instance_import():
    """Import an instance from an uploaded backup tarball (raw body). Optional
    ?name= renames it."""
    name = (request.args.get('name') or '').strip()
    if name and not valid_instance_name(name):
        return err('Invalid instance name')
    body = request.get_data()
    if not body:
        return err('No backup file uploaded')
    headers = {'Host': 'lxd', 'Content-Type': 'application/octet-stream'}
    if name:
        headers['X-LXD-name'] = name
    conn = _UnixHTTPConnection(SOCKET_PATH, timeout=1800)
    try:
        conn.request('POST', '/1.0/instances', body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as e:
        return err(f'Upload to daemon failed: {e}', 502)
    finally:
        conn.close()
    try:
        doc = json.loads(raw or b'{}')
    except ValueError:
        return err('Malformed daemon response', 502)
    if doc.get('type') == 'error':
        return err(doc.get('error', 'import failed'), doc.get('error_code') or 500)
    if doc.get('type') == 'async':
        try:
            _wait_operation((doc.get('operation') or '').rstrip('/').split('/')[-1], 1800)
        except LxdError as e:
            return _lxd_error_response(e)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════
#  Images  (local cache + remote simplestreams browse)
# ═══════════════════════════════════════════════════════════════════════

@bp.route('/api/storage-pools')
def storage_pools():
    try:
        pools = lxd_request('GET', '/1.0/storage-pools?recursion=1')
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify([{'name': p.get('name'), 'driver': p.get('driver'),
                     'status': p.get('status', '')} for p in pools])


@bp.route('/api/profiles')
def profiles_list():
    try:
        profs = lxd_request('GET', '/1.0/profiles?recursion=1')
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify([{'name': p.get('name'), 'description': p.get('description', ''),
                     'devices': list((p.get('devices') or {}).keys())} for p in profs])




# ─── Module descriptor ─────────────────────────────────────────────────
MODULE = {'id': 'instances', 'label': 'Instances', 'category': 'Containers',
          'blueprint': bp}
