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
from ..core.auth import (_is_admin, _hash_token, RE_USERNAME,
                         LOCKOUT_MAX, LOCKOUT_WINDOW, _user_role, _users)

bp = Blueprint('network', __name__)

NETPLAN_HELPER = HELPER_PREFIX + '-netplan'
NETCONF_FILE = os.environ.get('DASHBOARD_NETCONF_FILE', os.path.join(APP_DIR, 'network_config.json'))
HOSTS_FILE = os.environ.get('DASHBOARD_HOSTS_FILE', '/etc/hosts')
PENDING_WINDOW = 600   # seconds the un-finalized new address lingers before auto-cleanup
FINALIZE_WINDOW = 90   # seconds to heartbeat-confirm a finalize before it rolls back
DHCP_LEASE_WAIT = 15   # seconds to wait for a DHCP lease so we can report the new IP

RE_HOST_LABEL = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')
RE_DOMAIN = re.compile(r'^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*'
                       r'[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')
RE_IFACE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$')

# The pending network change (None when idle). 'phase' is 'dual' (new address
# added, awaiting finalize) or 'finalizing' (committed, awaiting heartbeat).
_net_pending = {'phase': None, 'token': None, 'timer': None, 'prev': None,
                'target': None, 'dual': None, 'iface': None, 'desc': None,
                'new_addr': '', 'new_url': ''}
_net_lock = threading.Lock()

# Single-use, short-lived handoff tokens that let the new-address origin mint a
# session without re-typing credentials (session cookies are per-host). Stored
# by SHA-256; minted only by an authenticated admin apply; bound to that user.
HANDOFF_TTL = 120  # seconds
_net_handoffs = {}  # token_hash -> {user, role, exp, used}
_handoff_fails = {}  # ip -> (count, first_ts) brute-force throttle for the public endpoint


def _valid_ipv4(s):
    parts = (s or '').split('.')
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255):
            return False
    return True


def _valid_cidr(s):
    ip, sep, prefix = (s or '').partition('/')
    return bool(sep) and _valid_ipv4(ip) and prefix.isdigit() and 0 <= int(prefix) <= 32


def load_netconf():
    try:
        with open(NETCONF_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'ethernets': {}, 'bridges': {}}


def save_netconf(conf):
    write_json_atomic(NETCONF_FILE, conf, 0o600)


def render_netplan(conf):
    """Render the app's network config to netplan YAML (values are pre-validated,
    so no escaping is needed)."""
    lines = ['network:', '  version: 2', '  renderer: networkd']

    def emit(name, spec, indent):
        pad = ' ' * indent
        p2 = ' ' * (indent + 2)
        lines.append('%s%s:' % (pad, name))
        if spec.get('interfaces'):
            lines.append('%sinterfaces: [%s]' % (p2, ', '.join(spec['interfaces'])))
        # dhcp4 and a list of static addresses are emitted independently:
        # networkd happily holds both at once (DHCP lease + extra static IPs),
        # which is exactly what the dual-IP transition phase relies on.
        lines.append('%sdhcp4: %s' % (p2, 'true' if spec.get('dhcp4') else 'false'))
        addrs = spec.get('addresses', [])
        if addrs:
            lines.append('%saddresses:' % p2)
            for a in addrs:
                lines.append('%s  - %s' % (p2, a))
        if spec.get('gateway'):
            lines.append('%sroutes:' % p2)
            lines.append('%s  - to: default' % p2)
            lines.append('%s    via: %s' % (p2, spec['gateway']))
        if spec.get('nameservers'):
            lines.append('%snameservers:' % p2)
            lines.append('%s  addresses: [%s]' % (p2, ', '.join(spec['nameservers'])))

    eth = conf.get('ethernets', {})
    if eth:
        lines.append('  ethernets:')
        for n, s in eth.items():
            emit(n, s, 4)
    br = conf.get('bridges', {})
    if br:
        lines.append('  bridges:')
        for n, s in br.items():
            emit(n, s, 4)
    return '\n'.join(lines) + '\n'


def _netplan_apply_yaml(yaml_text):
    """Hand the YAML to the root-owned helper: write + `netplan generate` +
    `netplan apply`. On a generate failure the helper restores the prior file and
    returns non-zero (so connectivity is never changed by a bad config)."""
    return run([NETPLAN_HELPER, 'apply'], input_data=yaml_text)


def _iface_spec(conf, iface):
    """The spec for `iface` from either ethernets or bridges (empty if absent)."""
    return (conf.get('ethernets', {}).get(iface)
            or conf.get('bridges', {}).get(iface) or {})


def _addr_host(cidr):
    """'192.168.1.50/24' -> '192.168.1.50'."""
    return (cidr or '').split('/')[0]


def _net_live_spec(iface):
    """The interface's actual current state (addresses/dhcp/gateway) from live
    `ip` data — the fallback when the dashboard has no managed spec yet, so the
    dual phase still preserves the real current IP."""
    for i in _net_interfaces():
        if i['name'] == iface:
            return {'dhcp4': bool(i.get('dhcp')),
                    'addresses': list(i.get('addresses', [])),
                    'gateway': i.get('gateway', '')}
    return {}


def _net_union_spec(prev_spec, target_spec, live_spec=None):
    """Build the transitional 'dual' spec: the new address(es) added on top of
    the old one(s), keeping the OLD gateway/DNS active so routing doesn't switch
    until finalize. `prev_spec` is the dashboard-managed config (may be falsy on
    first configure); `live_spec` reflects the interface's real current state and
    is the fallback. Pure function (unit-tested)."""
    old = prev_spec or live_spec or {}
    target_spec = target_spec or {}
    # Only carry the old *static* addresses forward. If the old side was DHCP, its
    # address comes from the lease and dhcp4 below re-acquires it — re-listing it
    # as static would duplicate the lease.
    old_addrs = [] if old.get('dhcp4') else list(old.get('addresses', []))
    merged, seen = [], set()
    for a in old_addrs + list(target_spec.get('addresses', [])):
        if a and a not in seen:
            seen.add(a)
            merged.append(a)
    dual = {'dhcp4': bool(old.get('dhcp4') or target_spec.get('dhcp4'))}
    if target_spec.get('interfaces') is not None:
        dual['interfaces'] = target_spec['interfaces']
    elif old.get('interfaces'):
        dual['interfaces'] = old['interfaces']
    if merged:
        dual['addresses'] = merged
    # Keep the OLD gateway/DNS during the dual phase, but only when the dual spec
    # is purely static — if DHCP is on it supplies its own default route, and
    # adding a manual one would create a duplicate/ambiguous default route.
    if not dual['dhcp4']:
        if old.get('gateway'):
            dual['gateway'] = old['gateway']
        if old.get('nameservers'):
            dual['nameservers'] = old['nameservers']
    return dual


def _net_resolve_new_addr(iface, target_spec, prev_conf):
    """The address the admin should browse to after applying. For a static
    target that's the configured IP; for DHCP we wait briefly for a lease and
    return the new dynamic address (one not already present in the old config)."""
    static = target_spec.get('addresses') or []
    if static:
        return _addr_host(static[0])
    if not target_spec.get('dhcp4'):
        return ''
    known = {_addr_host(a) for a in _iface_spec(prev_conf, iface).get('addresses', [])}
    deadline = time.time() + DHCP_LEASE_WAIT
    while time.time() < deadline:
        out, _, _ = run(['ip', '-j', 'addr', 'show', iface])
        try:
            links = json.loads(out or '[]')
        except json.JSONDecodeError:
            links = []
        for l in links:
            for a in l.get('addr_info', []):
                if (a.get('family') == 'inet' and a.get('dynamic')
                        and a.get('local') and a['local'] not in known):
                    return a['local']
        time.sleep(1)
    return ''


def _mint_handoff():
    """Issue a single-use handoff secret for the *current admin session*, so the
    new-address origin can mint a session without re-typing credentials. Only one
    is valid at a time (cleared whenever a change starts/ends)."""
    user = session.get('user')
    if not user or _user_role(_users().get(user)) != 'admin':
        return ''
    secret = secrets.token_urlsafe(32)
    _net_handoffs.clear()
    _net_handoffs[_hash_token(secret)] = {'user': user, 'role': 'admin',
                                          'exp': time.time() + HANDOFF_TTL, 'used': False}
    return secret


def _consume_handoff(secret):
    """Validate + burn a handoff secret (constant-time). Valid only if unused,
    unexpired, AND a network change is still pending."""
    if not secret:
        return None
    h = _hash_token(secret)
    now = time.time()
    match = None
    for kh in list(_net_handoffs.keys()):
        rec = _net_handoffs[kh]
        if rec.get('used') or rec.get('exp', 0) < now:
            _net_handoffs.pop(kh, None)
            continue
        if hmac.compare_digest(kh, h):
            match = rec
            _net_handoffs.pop(kh, None)  # single-use
    if not match or not _net_pending['phase']:
        return None
    return match


def _net_clear_timer():
    if _net_pending.get('timer'):
        _net_pending['timer'].cancel()


def _net_clear_pending():
    _net_pending.update({'phase': None, 'token': None, 'timer': None, 'prev': None,
                         'target': None, 'dual': None, 'iface': None, 'desc': None,
                         'new_addr': '', 'new_url': ''})
    _net_handoffs.clear()


def _net_timeout_revert(token, expect_phase):
    """Timer callback: restore the previous (working) config. For 'dual' this
    removes the un-finalized new address; for 'finalizing' it rolls back a
    finalize that was never heartbeat-confirmed. Either way the admin keeps/
    regains a working connection — lockout is impossible."""
    with _net_lock:
        if _net_pending['token'] != token or _net_pending['phase'] != expect_phase:
            return  # superseded, finalized, confirmed, or already reverted
        prev = _net_pending['prev']
        _netplan_apply_yaml(render_netplan(prev))
        save_netconf(prev)
        _net_clear_pending()
    print('network: %s' % ('removed un-finalized address (dual-phase timeout)'
                           if expect_phase == 'dual'
                           else 'finalize not confirmed — rolled back'), flush=True)


def _net_apply(target_conf, dual_conf, iface, desc):
    """Enter the dual phase: apply `dual_conf` (new address ADDED alongside the
    old one, old gateway/DNS kept), arm the cleanup janitor, and return where to
    go to finalize. The clean `target_conf` is committed later by finalize.
    Returns a jsonify-able dict."""
    with _net_lock:
        prev = load_netconf()
        out, errtxt, rc = _netplan_apply_yaml(render_netplan(dual_conf))
        if rc != 0:
            return {'success': False, 'error': (errtxt or out or 'netplan rejected the config').strip()[:300]}
        save_netconf(dual_conf)
        _net_clear_timer()
        token = secrets.token_hex(8)
        timer = threading.Timer(PENDING_WINDOW, _net_timeout_revert, args=[token, 'dual'])
        timer.daemon = True
        _net_pending.update({'phase': 'dual', 'token': token, 'timer': timer,
                             'prev': prev, 'target': target_conf, 'dual': dual_conf,
                             'iface': iface, 'desc': desc, 'new_addr': '', 'new_url': ''})
        timer.start()
    # Resolve the new address outside the lock (a DHCP lease wait can take a few
    # seconds) and mint the handoff link for it.
    new_addr = _net_resolve_new_addr(iface, _iface_spec(target_conf, iface), prev)
    new_url = ''
    if new_addr:
        secret = _mint_handoff()
        scheme = 'https' if TLS_ENABLED else 'http'
        new_url = '%s://%s:%d/?nethandoff=%s' % (scheme, new_addr, DASHBOARD_PORT, secret)
    with _net_lock:
        if _net_pending['token'] == token:
            _net_pending['new_addr'] = new_addr
            _net_pending['new_url'] = new_url
    return {'success': True, 'pending': True, 'phase': 'dual', 'token': token,
            'new_addr': new_addr, 'new_url': new_url, 'window': PENDING_WINDOW, 'desc': desc}


def _net_dns():
    servers = []
    try:
        with open('/etc/resolv.conf') as f:
            for line in f:
                if line.startswith('nameserver'):
                    parts = line.split()
                    if len(parts) >= 2:
                        servers.append(parts[1])
    except OSError:
        pass
    return servers


def _net_interfaces():
    out, _, _ = run(['ip', '-j', 'addr', 'show'])
    try:
        links = json.loads(out or '[]')
    except json.JSONDecodeError:
        links = []
    # Map each interface to its default-route gateway (so the Configure dialog
    # can pre-fill the current gateway).
    gwmap = {}
    rout, _, _ = run(['ip', '-j', 'route', 'show', 'default'])
    try:
        for r in json.loads(rout or '[]'):
            if r.get('dst') == 'default' and r.get('dev') and r.get('gateway'):
                gwmap.setdefault(r['dev'], r['gateway'])
    except json.JSONDecodeError:
        pass
    ifaces = []
    for l in links:
        name = l.get('ifname', '')
        if name == 'lo':
            continue
        kind = (l.get('linkinfo', {}) or {}).get('info_kind', '')
        inet = [a for a in l.get('addr_info', []) if a.get('family') == 'inet']
        addrs = ['%s/%s' % (a.get('local', ''), a.get('prefixlen', '')) for a in inet]
        # A DHCP-assigned address carries dynamic=true (kernel sets it from the
        # lease); a static address does not. This tells us the current mode
        # without parsing the existing netplan.
        dhcp = any(a.get('dynamic') for a in inet)
        ifaces.append({'name': name, 'type': 'bridge' if kind == 'bridge' else 'ethernet',
                       'state': (l.get('operstate') or '').lower(), 'mac': l.get('address', ''),
                       'addresses': addrs, 'dhcp': dhcp, 'gateway': gwmap.get(name, '')})
    return ifaces


def _net_gateway():
    out, _, _ = run(['ip', '-j', 'route', 'show', 'default'])
    try:
        for r in json.loads(out or '[]'):
            if r.get('dst') == 'default' and r.get('gateway'):
                return r['gateway']
    except json.JSONDecodeError:
        pass
    return ''


@bp.route('/api/network')
def network_get():
    fqdn = socket.getfqdn()
    host = socket.gethostname()
    domain = fqdn[len(host) + 1:] if fqdn.startswith(host + '.') else ''
    with _net_lock:
        pending = None
        if _net_pending['phase']:
            pending = {'phase': _net_pending['phase'], 'token': _net_pending['token'],
                       'desc': _net_pending['desc'], 'new_addr': _net_pending['new_addr'],
                       'new_url': _net_pending['new_url'],
                       'window': PENDING_WINDOW if _net_pending['phase'] == 'dual' else FINALIZE_WINDOW}
    return jsonify({
        'hostname': host, 'domain': domain, 'fqdn': fqdn,
        'interfaces': _net_interfaces(), 'gateway': _net_gateway(), 'dns': _net_dns(),
        'config': load_netconf(), 'pending': pending,
    })


@bp.route('/api/network/hostname', methods=['POST'])
def network_hostname():
    data = request.get_json() or {}
    host = (data.get('hostname') or '').strip()
    domain = (data.get('domain') or '').strip()
    if not RE_HOST_LABEL.match(host):
        return err('Invalid hostname')
    if domain and not RE_DOMAIN.match(domain):
        return err('Invalid domain')
    r = run_safe(['hostnamectl', 'set-hostname', host])
    if not r['success']:
        return jsonify(r)
    # Maintain the 127.0.1.1 FQDN line in /etc/hosts (world-readable; rewrite via tee).
    fqdn = '%s.%s' % (host, domain) if domain else host
    try:
        with open(HOSTS_FILE) as f:
            lines = [ln.rstrip('\n') for ln in f]
    except OSError:
        lines = []
    lines = [ln for ln in lines if not ln.split('#', 1)[0].strip().startswith('127.0.1.1')]
    # insert after the 127.0.0.1 line if present, else at top
    newline = '127.0.1.1\t%s %s' % (fqdn, host) if domain else '127.0.1.1\t%s' % host
    out = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.split('#', 1)[0].strip().startswith('127.0.0.1'):
            out.append(newline)
            inserted = True
    if not inserted:
        out.insert(0, newline)
    run(['tee', HOSTS_FILE], input_data='\n'.join(out) + '\n')
    return jsonify({'success': True, 'fqdn': fqdn})


def _build_static_spec(data, base):
    """Validate the static-IP fields and attach them to `base`: one or MORE CIDR
    addresses (so a single interface can intentionally hold several IPs), one
    optional default gateway, and optional DNS. Accepts `addresses` (a list) or
    the legacy single `address`. Returns (spec, None) on success, else
    (None, error_response)."""
    raw = data.get('addresses')
    if raw is None:
        raw = [data.get('address')]
    if not isinstance(raw, list):
        raw = [raw]
    addrs = []
    for a in raw:
        a = (a or '').strip()
        if not a:
            continue
        if not _valid_cidr(a):
            return None, err('Address must be CIDR, e.g. 192.168.1.50/24')
        if a not in addrs:           # de-dupe, preserve order
            addrs.append(a)
    if not addrs:
        return None, err('At least one static address (CIDR) is required')
    base['addresses'] = addrs
    gw = (data.get('gateway') or '').strip()
    if gw:
        if not _valid_ipv4(gw):
            return None, err('Invalid gateway')
        base['gateway'] = gw          # one default gateway, regardless of address count
    dns = [d.strip() for d in (data.get('nameservers') or []) if d.strip()]
    for d in dns:
        if not _valid_ipv4(d):
            return None, err('Invalid nameserver: %s' % d)
    if dns:
        base['nameservers'] = dns
    return base, None


@bp.route('/api/network/interface', methods=['POST'])
def network_interface():
    data = request.get_json() or {}
    iface = (data.get('iface') or '').strip()
    mode = data.get('mode', 'dhcp')
    if not RE_IFACE.match(iface):
        return err('Invalid interface name')
    if mode not in ('dhcp', 'static'):
        return err('Invalid mode')
    spec = {'dhcp4': mode == 'dhcp'}
    if mode == 'static':
        spec, e = _build_static_spec(data, spec)
        if e:
            return e
    conf = load_netconf()
    prev_spec = conf.get('ethernets', {}).get(iface)
    target_conf = json.loads(json.dumps(conf))
    target_conf.setdefault('ethernets', {})[iface] = spec
    # Dual: keep the interface's current address and add the new one, so the
    # admin's existing connection is never dropped during verification.
    dual_conf = json.loads(json.dumps(conf))
    dual_conf.setdefault('ethernets', {})[iface] = _net_union_spec(
        prev_spec, spec, _net_live_spec(iface))
    return jsonify(_net_apply(target_conf, dual_conf, iface,
                              'interface %s → %s' % (iface, mode)))


@bp.route('/api/network/bridge', methods=['POST'])
def network_bridge():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    members = [m.strip() for m in (data.get('interfaces') or []) if m.strip()]
    mode = data.get('mode', 'dhcp')
    if not RE_IFACE.match(name):
        return err('Invalid bridge name')
    for m in members:
        if not RE_IFACE.match(m):
            return err('Invalid member interface: %s' % m)
    if mode not in ('dhcp', 'static'):
        return err('Invalid mode')
    spec = {'dhcp4': mode == 'dhcp', 'interfaces': members}
    if mode == 'static':
        spec, e = _build_static_spec(data, spec)
        if e:
            return e
    conf = load_netconf()
    target_conf = json.loads(json.dumps(conf))
    target_conf.setdefault('bridges', {})[name] = spec
    # Member NICs join the bridge with no IP of their own.
    for m in members:
        target_conf.setdefault('ethernets', {})[m] = {'dhcp4': False}
    # A bridge can't be dual-IP'd — enslaving a member NIC removes its address —
    # so the change applies in full and is protected by the finalize/janitor net
    # (and the handoff link) rather than a non-disruptive dual phase.
    return jsonify(_net_apply(target_conf, target_conf, name,
                              'bridge %s (%s)' % (name, ', '.join(members) or 'no members')))


@bp.route('/api/network/finalize', methods=['POST'])
def network_finalize():
    """Commit the dual phase: apply the clean target config (drops the old
    address, switches gateway/DNS) and arm the short heartbeat-confirm net."""
    token = (request.get_json() or {}).get('token')
    with _net_lock:
        if _net_pending['phase'] != 'dual':
            return err('No pending change to finalize', 409)
        if token != _net_pending['token']:
            return err('Stale token; a newer change is pending', 409)
        target = _net_pending['target']
        out, errtxt, rc = _netplan_apply_yaml(render_netplan(target))
        if rc != 0:
            return err((errtxt or out or 'netplan rejected the config').strip()[:300])
        save_netconf(target)
        _net_clear_timer()
        ctoken = secrets.token_hex(8)
        timer = threading.Timer(FINALIZE_WINDOW, _net_timeout_revert, args=[ctoken, 'finalizing'])
        timer.daemon = True
        _net_pending.update({'phase': 'finalizing', 'token': ctoken, 'timer': timer})
        timer.start()
    return jsonify({'success': True, 'phase': 'finalizing', 'confirm_token': ctoken,
                    'window': FINALIZE_WINDOW})


@bp.route('/api/network/confirm', methods=['POST'])
def network_confirm():
    """Heartbeat from the new-address page after finalize: cancels the rollback
    net, locking in the committed config."""
    token = (request.get_json() or {}).get('token')
    with _net_lock:
        if _net_pending['phase'] != 'finalizing':
            return jsonify({'success': True, 'note': 'nothing to confirm'})
        if token != _net_pending['token']:
            return err('Stale confirmation token; a newer change is pending', 409)
        _net_clear_timer()
        _net_clear_pending()
    return jsonify({'success': True})


@bp.route('/api/network/revert', methods=['POST'])
def network_revert_now():
    """Roll back to the previous working config immediately (either phase)."""
    with _net_lock:
        if not _net_pending['phase']:
            return jsonify({'success': True, 'note': 'nothing to revert'})
        prev = _net_pending['prev']
        _netplan_apply_yaml(render_netplan(prev))
        save_netconf(prev)
        _net_clear_timer()
        _net_clear_pending()
    return jsonify({'success': True})


@bp.route('/api/network/handoff', methods=['POST'])
def network_handoff():
    """PUBLIC: exchange a single-use handoff secret (minted by the admin's apply)
    for a session on this origin. Lets the new-address page log in without
    re-typing credentials (session cookies are per-host). High-entropy,
    single-use, 120s, valid only while a change is pending — plus a per-IP
    throttle on this unauthenticated endpoint."""
    ip = request.remote_addr or '?'
    cnt, first = _handoff_fails.get(ip, (0, 0))
    now = time.time()
    if now - first > LOCKOUT_WINDOW:
        cnt, first = 0, now
    if cnt >= LOCKOUT_MAX:
        return jsonify({'success': False, 'error': 'Too many attempts; try again later'}), 429
    secret = (request.get_json(silent=True) or {}).get('token') or ''
    with _net_lock:
        rec = _consume_handoff(secret)
    if not rec:
        _handoff_fails[ip] = (cnt + 1, first or now)
        return jsonify({'success': False, 'error': 'Invalid or expired handoff'}), 401
    _handoff_fails.pop(ip, None)
    g.audit_user = rec['user']
    session.clear()
    session['user'] = rec['user']
    session.permanent = True
    return jsonify({'success': True, 'user': rec['user'], 'role': rec['role'],
                    'fqdn': socket.getfqdn()})


# ─── Automatic snapshot schedules ─────────────────────────────────────
# Opt-in only: nothing is snapshotted or pruned unless the user has created an
# *enabled* schedule. The systemd timer is enabled only while at least one
# enabled schedule exists, and pruning only ever touches autosnap_<freq>_*
# snapshots of scheduled datasets (never manual snapshots).

