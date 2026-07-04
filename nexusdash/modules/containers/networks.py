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

bp = Blueprint('ctnetworks', __name__)

@bp.route('/api/instances/<name>/network', methods=['POST'])
def instance_set_network(name):
    """Move an instance's NIC to a different network (new or existing instance).
    Overrides the profile's device at the instance level. A running container
    usually needs a restart for the change to take effect."""
    if not valid_instance_name(name):
        return err('Invalid instance name')
    data = request.get_json() or {}
    net = (data.get('network') or '').strip()
    dev = (data.get('device') or 'eth0').strip()
    if not RE_CT_NETWORK.match(net):
        return err('Invalid network name')
    if not RE_CT_DEVNAME.match(dev):
        return err('Invalid device name')
    try:
        device = _nic_device_for(net, dev)
        lxd_request('PATCH', f'/1.0/instances/{name}', {'devices': {dev: device}}, wait=False)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True, 'restart_recommended': True})


@bp.route('/api/networks')
def networks_list():
    try:
        nets = lxd_request('GET', '/1.0/networks?recursion=1')
    except LxdError as e:
        return _lxd_error_response(e)
    out = []
    for n in nets:
        cfg = n.get('config') or {}
        out.append({
            'name': n.get('name'),
            'type': n.get('type'),
            'managed': n.get('managed', False),
            'description': n.get('description', ''),
            'ipv4_address': cfg.get('ipv4.address', ''),
            'ipv6_address': cfg.get('ipv6.address', ''),
            'ipv4_nat': cfg.get('ipv4.nat', ''),
            'ipv4_dhcp': cfg.get('ipv4.dhcp', ''),
            'dns_domain': cfg.get('dns.domain', ''),
            'external_interfaces': cfg.get('bridge.external_interfaces', ''),
            'parent': cfg.get('parent', ''),
            'used_by': len(n.get('used_by', [])),
        })
    return jsonify(out)


# ─── Host interfaces (uplink candidates for bridges / macvlan) ────────
# We read `ip -j addr` / `ip -j route` (no root needed) to flag which interface
# carries the host's own IP / default route — enslaving THAT into a bridge would
# drop host connectivity (LXD also refuses configured interfaces, but we warn
# earlier and more clearly).

def _host_interfaces():
    try:
        nets = lxd_request('GET', '/1.0/networks?recursion=1')
    except LxdError:
        nets = []
    lxd_type = {n.get('name'): n.get('type') for n in nets}
    out_addr, _, _ = run(['ip', '-j', 'addr'])
    out_route, _, _ = run(['ip', '-j', 'route'])
    try:
        addrs = json.loads(out_addr or '[]')
    except ValueError:
        addrs = []
    try:
        routes = json.loads(out_route or '[]')
    except ValueError:
        routes = []
    default_devs = {r.get('dev') for r in routes if r.get('dst') == 'default'}
    result = []
    for i in addrs:
        name = i.get('ifname')
        if not name or name == 'lo':
            continue
        flags = i.get('flags', [])
        v4 = [a['local'] for a in i.get('addr_info', []) if a.get('family') == 'inet']
        ntype = lxd_type.get(name)
        is_default = name in default_devs
        result.append({
            'name': name,
            'lxd_type': ntype,                       # physical / bridge / …
            'state': i.get('operstate'),
            'carrier': 'LOWER_UP' in flags,
            'addresses': v4,
            'master': i.get('master'),               # already enslaved to a bridge?
            'is_default_route': is_default,
            'has_ip': bool(v4),
            # Safe to enslave into a no-IP bridge: a real NIC with no host IP,
            # not the default route, not already in a bridge.
            'bridgeable': (ntype == 'physical' and not v4 and not is_default
                           and not i.get('master')),
        })
    return result


@bp.route('/api/host/interfaces')
def host_interfaces():
    return jsonify(_host_interfaces())


# ─── Network create / update / delete ─────────────────────────────────
NET_KINDS = {'nat', 'bridge-lan', 'macvlan'}
RE_CIDR = re.compile(r'^\d{1,3}(\.\d{1,3}){3}/\d{1,2}\Z')
# Config keys an operator may set/edit through the UI (allowlist).
NET_CONFIG_KEYS = {'ipv4.address', 'ipv4.nat', 'ipv4.dhcp', 'ipv6.address',
                   'ipv6.nat', 'ipv6.dhcp', 'dns.domain', 'bridge.external_interfaces',
                   'bridge.mode', 'parent', 'mtu'}


@bp.route('/api/networks', methods=['POST'])
def network_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    kind = data.get('kind', 'nat')
    force = bool(data.get('force'))
    if not RE_CT_NETWORK.match(name):
        return err('Invalid network name (letters/digits/._-, max 15 chars)')
    if kind not in NET_KINDS:
        return err('Invalid network kind')
    ifaces = {i['name']: i for i in _host_interfaces()}

    if kind == 'nat':
        ipv4 = (data.get('ipv4') or 'auto').strip()
        if ipv4 not in ('auto', 'none') and not RE_CIDR.match(ipv4):
            return err('IPv4 must be "auto", "none", or a CIDR like 10.10.0.1/24')
        cfg = {'ipv4.address': ipv4,
               'ipv4.nat': 'true' if data.get('nat', True) else 'false',
               'ipv6.address': 'auto' if data.get('ipv6', False) else 'none'}
        body = {'name': name, 'type': 'bridge', 'config': cfg}
    elif kind == 'bridge-lan':
        up = (data.get('uplink') or '').strip()
        if up not in ifaces:
            return err('Unknown uplink interface')
        it = ifaces[up]
        if (it['has_ip'] or it['is_default_route']) and not force:
            return err(f'{up} carries the host IP / default route — enslaving it '
                       f'would cut host connectivity. Pick a NIC with no IP.', 409)
        # No-IP L2 bridge enslaving the NIC: containers get DHCP from the real LAN.
        cfg = {'ipv4.address': 'none', 'ipv6.address': 'none',
               'bridge.external_interfaces': up}
        body = {'name': name, 'type': 'bridge', 'config': cfg}
    else:  # macvlan
        up = (data.get('uplink') or '').strip()
        if up not in ifaces:
            return err('Unknown parent interface')
        body = {'name': name, 'type': 'macvlan', 'config': {'parent': up}}

    if data.get('description'):
        body['description'] = str(data['description'])[:200]
    try:
        lxd_request('POST', '/1.0/networks', body, wait=False)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True, 'name': name})


@bp.route('/api/networks/<name>', methods=['PATCH'])
def network_update(name):
    if not RE_CT_NETWORK.match(name):
        return err('Invalid network name')
    data = request.get_json() or {}
    cfg = {k: str(v) for k, v in (data.get('config') or {}).items() if k in NET_CONFIG_KEYS}
    if not cfg:
        return err('No editable config keys supplied')
    try:
        net = lxd_request('GET', f'/1.0/networks/{name}')
        if not net.get('managed'):
            return err('Only managed networks can be edited', 409)
        lxd_request('PATCH', f'/1.0/networks/{name}', {'config': cfg}, wait=False)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/networks/<name>', methods=['DELETE'])
def network_delete(name):
    if not RE_CT_NETWORK.match(name):
        return err('Invalid network name')
    try:
        net = lxd_request('GET', f'/1.0/networks/{name}')
        if not net.get('managed'):
            return err('Only managed networks can be deleted', 409)
        # LXD refuses (and we surface) deletion of a network still in use.
        lxd_request('DELETE', f'/1.0/networks/{name}', wait=False)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════
#  Console  (text/serial) — websocket proxy: browser <-> daemon operation ws
# ═══════════════════════════════════════════════════════════════════════



# ─── Module descriptor ─────────────────────────────────────────────────
MODULE = {'id': 'ctnetworks', 'label': 'Instance Networks', 'category': 'Containers',
          'blueprint': bp}
