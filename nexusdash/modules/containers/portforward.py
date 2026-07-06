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

bp = Blueprint('portforward', __name__)

@bp.route('/api/proxies')
def proxies_list():
    """All proxy (port-forward) devices across instances, one flat list."""
    try:
        insts = lxd_request('GET', '/1.0/instances?recursion=1')
    except LxdError as e:
        return _lxd_error_response(e)
    out = []
    for i in insts:
        for dname, dev in (i.get('devices') or {}).items():
            if dev.get('type') == 'proxy':
                out.append({'instance': i.get('name'), 'device': dname,
                            'listen': dev.get('listen'), 'connect': dev.get('connect'),
                            'bind': dev.get('bind', 'host'), 'nat': dev.get('nat', 'false'),
                            'status': i.get('status')})
    return jsonify(out)


@bp.route('/api/instances/<name>/proxy', methods=['POST'])
def instance_proxy_add(name):
    if not valid_instance_name(name):
        return err('Invalid instance name')
    data = request.get_json() or {}
    dev = (data.get('device') or '').strip()
    listen = (data.get('listen') or '').strip()
    connect = (data.get('connect') or '').strip()
    if not RE_CT_DEVNAME.match(dev):
        return err('Invalid device name')
    if not RE_PROXY_ADDR.match(listen):
        return err('listen must look like tcp:0.0.0.0:80')
    if not RE_PROXY_ADDR.match(connect):
        return err('connect must look like tcp:127.0.0.1:80')
    device = {'type': 'proxy', 'listen': listen, 'connect': connect}
    if data.get('bind') in ('host', 'instance'):
        device['bind'] = data['bind']
    # nat=true uses DNAT (preserves source IP) but requires connect to be the
    # container's real IP, not 127.0.0.1. Default is the userspace proxy.
    if data.get('nat'):
        device['nat'] = 'true'
    try:
        lxd_request('PATCH', f'/1.0/instances/{name}', {'devices': {dev: device}}, wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════
#  Export / import  (instance backups → tarball)
# ═══════════════════════════════════════════════════════════════════════



# ─── Module descriptor ─────────────────────────────────────────────────
MODULE = {'id': 'portforward', 'label': 'Port Forward', 'category': 'LXD / Incus',
          'blueprint': bp}
