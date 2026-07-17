"""DNS & DHCP management module — dnsmasq (category "DNS").

Ported from the standalone DNSMAQ-MGR appliance (github.com/brainchillz/
nexus-dnsmasq-mgr), which shares this dashboard's DNA. This makes a dashboard
node a fuller homelab appliance (storage + containers + DNS/DHCP in one box).
Default-DISABLED (like metrics) — zero footprint until enabled per node on the
Modules page; nothing is written to disk and no drop-in touches dnsmasq until
the first edit after enabling.

Model: the module OWNS its config. JSON stores (state/) are the source of
truth; every mutation renders dnsmasq.d/*.conf + a managed hosts file + the
dhcp hosts/opts files into a private render dir, validates with
`dnsmasq --test` (rootless), atomically swaps, then reloads (SIGHUP for
host/lease/option edits) or restarts (structural) via argument-pinned sudoers.
The installer drops /etc/dnsmasq.d/zz-<prefix>.conf pointing dnsmasq at the
render dir (existing fleet nodes need it added by hand — firewall/caddy
precedent).

Scope vs the standalone: NO network-boot server (TFTP/proxy-DHCP), NO
mirroring. DHCP carries only external-boot options (dhcp-boot pointing at a
separate boot server) plus the standard option presets.
"""
import os
import copy
import json
import time
import shutil
import struct
import socket
import secrets
import tempfile
import threading
import ipaddress
import re as _re
from flask import Blueprint, jsonify, request

from ..core.config import APP_DIR, UNIT_PREFIX, write_json_atomic
from ..core.runcmd import run, err

bp = Blueprint('dnsmasq', __name__)

# ─── Paths (module-owned, under the app dir) ──────────────────────────
MOD_DIR = os.environ.get('DASHBOARD_DNSMASQ_DIR', os.path.join(APP_DIR, 'dnsmasq'))
STATE_DIR = os.path.join(MOD_DIR, 'state')
RENDER_DIR = os.path.join(MOD_DIR, 'render')
CONF_DIR = os.path.join(RENDER_DIR, 'dnsmasq.d')
HOSTS_DIR = os.path.join(RENDER_DIR, 'hosts.d')
MANAGED_HOSTS = os.path.join(HOSTS_DIR, 'managed-hosts')
DHCP_HOSTS_FILE = os.path.join(RENDER_DIR, 'dhcp-hosts')
DHCP_OPTS_FILE = os.path.join(RENDER_DIR, 'dhcp-opts')
LEASES_DIR = os.path.join(MOD_DIR, 'leases')
LEASES_FILE = os.path.join(LEASES_DIR, 'dnsmasq.leases')

DNSMASQ_BIN = os.environ.get('DASHBOARD_DNSMASQ_BIN', 'dnsmasq')
DNSMASQ_UNIT = os.environ.get('DASHBOARD_DNSMASQ_UNIT', 'dnsmasq')
# CHAOS-stats query port (dnsmasq serves DNS on 53; overridable for dev).
DNS_PORT = int(os.environ.get('DASHBOARD_DNSMASQ_DNS_PORT', 53))

HEADER = ('# Managed by Nexus Dashboard (dnsmasq module) — do not edit; '
          'overwritten on every apply.\n')
HUP_ONLY = {'hosts.d/managed-hosts', 'dhcp-hosts', 'dhcp-opts'}
TRUST_ANCHORS = [
    '.,20326,8,2,E06D44B80B8F1D39A95C0B0D7C65D08458E880409BBC683457104237C7F8EC8D',
    '.,38696,8,2,683D2D0ACB8C9B712A1948B27F741219298D0A450D612C483AF444A4C0FB2B16',
]


def write_text_atomic(path, text, mode=0o644):
    """Atomic text write (Nexus core.config has only write_json_atomic). Render
    files are world-readable — dashboard user writes, root dnsmasq reads."""
    tmp = '%s.tmp.%d' % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ─── Validators (inlined; Nexus core/validators is storage-oriented) ──
RE_HOSTNAME = _re.compile(r'^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?$')
RE_DOMAIN = _re.compile(r'^(?=.{1,253}$)[A-Za-z0-9_]([A-Za-z0-9_-]*[A-Za-z0-9_])?'
                        r'(\.[A-Za-z0-9_]([A-Za-z0-9_-]*[A-Za-z0-9_])?)*$')
RE_MAC = _re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
RE_LEASE = _re.compile(r'^(\d+[smhdw]?|infinite)$')
RE_TAG = _re.compile(r'^[A-Za-z0-9_-]{1,32}$')
RE_IFACE = _re.compile(r'^[A-Za-z0-9._@-]{1,15}$')
RE_DHCP_OPTION = _re.compile(r'^(\d{1,3}|option6?:[a-z0-9-]{1,40})$')
RE_OPT_VALUE = _re.compile(r'^[A-Za-z0-9 .,:/_"\'=\[\]-]{1,255}$')
RE_BOOT_FILE = _re.compile(r'^[A-Za-z0-9._/-]{1,128}$')
RE_ID = _re.compile(r'^[a-z]_[0-9a-f]{6}$')
RE_COMMENT = _re.compile(r'^[^\r\n]{0,200}$')


def is_ipv4(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except (ValueError, TypeError):
        return False


def is_ipv6(s):
    try:
        ipaddress.IPv6Address(s)
        return True
    except (ValueError, TypeError):
        return False


def is_ip(s):
    return is_ipv4(s) or is_ipv6(s)


def is_upstream(s):
    s = str(s or '')
    if '#' in s:
        host, _, port = s.partition('#')
        return is_ip(host) and port.isdigit() and 0 < int(port) < 65536
    return is_ip(s)


def valid_hostname_fqdn(s):
    s = str(s or '')
    if not s or len(s) > 253:
        return False
    return all(RE_HOSTNAME.match(part) for part in s.rstrip('.').split('.'))


# ─── JSON stores ──────────────────────────────────────────────────────
STORE_LOCK = threading.RLock()
STORE_NAMES = ('settings', 'dns', 'dhcp')

DEFAULTS = {
    'settings': {
        'serial': 0, 'dns_enabled': True, 'dhcp_enabled': False,
        'domain': 'lan', 'expand_hosts': True,
        'interfaces': [], 'listen_addresses': [], 'bind_interfaces': True,
        'upstreams': ['1.1.1.1', '9.9.9.9'], 'no_resolv': True,
        'cache_size': 1000, 'domain_needed': True, 'bogus_priv': True,
        'dnssec': False, 'dhcp_authoritative': True,
        'log_queries': False, 'log_dhcp': False, 'extra_options': '',
    },
    'dns': {'serial': 0, 'hosts': [], 'cnames': [], 'addresses': [], 'forwards': []},
    'dhcp': {'serial': 0, 'ranges': [], 'static_leases': [], 'options': [],
             'boot': {'filename': '', 'server': ''}},
    'stats_cursor': {},
}


def _store_path(name):
    return os.path.join(STATE_DIR, name + '.json')


def load_store(name):
    base = copy.deepcopy(DEFAULTS[name])
    try:
        with open(_store_path(name)) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return base
    if isinstance(base, dict) and isinstance(data, dict):
        base.update(data)
        return base
    return data


def save_store(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)   # lazy: nothing on disk until first write
    write_json_atomic(_store_path(name), data, 0o600)


def bump_serial(name, data):
    data['serial'] = int(data.get('serial', 0)) + 1
    save_store(name, data)
    return data['serial']


def new_id(prefix):
    return '%s_%s' % (prefix, secrets.token_hex(3))


def find_record(items, rid):
    for it in items:
        if it.get('id') == rid:
            return it
    return None


def _enabled(items):
    return [it for it in items if it.get('enabled', True)]


# ─── Render functions (pure: stores in, text out) ─────────────────────

def render_main(settings):
    lines = [HEADER]
    if not settings.get('dns_enabled', True):
        lines.append('# DNS disabled from the UI')
        lines.append('port=0')
    if settings.get('domain'):
        lines.append('domain=%s' % settings['domain'])
        if settings.get('expand_hosts'):
            lines.append('expand-hosts')
    for ifc in settings.get('interfaces', []):
        lines.append('interface=%s' % ifc)
    for addr in settings.get('listen_addresses', []):
        lines.append('listen-address=%s' % addr)
    if settings.get('interfaces') or settings.get('listen_addresses'):
        lines.append('listen-address=127.0.0.1')   # CHAOS stats need loopback
    if settings.get('bind_interfaces'):
        lines.append('bind-interfaces')
    for up in settings.get('upstreams', []):
        lines.append('server=%s' % up)
    if settings.get('no_resolv'):
        lines.append('no-resolv')
    if settings.get('cache_size'):
        lines.append('cache-size=%d' % int(settings['cache_size']))
    if settings.get('domain_needed'):
        lines.append('domain-needed')
    if settings.get('bogus_priv'):
        lines.append('bogus-priv')
    if settings.get('dnssec'):
        lines.append('dnssec')
        for ta in TRUST_ANCHORS:
            lines.append('trust-anchor=%s' % ta)
    if settings.get('log_queries'):
        lines.append('log-queries=extra')
    if settings.get('log_dhcp'):
        lines.append('log-dhcp')
    lines.append('dhcp-leasefile=%s' % LEASES_FILE)
    return '\n'.join(lines) + '\n'


def render_dns(dns):
    lines = [HEADER, 'addn-hosts=%s' % MANAGED_HOSTS]
    for rec in _enabled(dns.get('addresses', [])):
        lines.append('address=/%s/%s' % (rec['domain'], rec['ip']))
    for rec in _enabled(dns.get('cnames', [])):
        lines.append('cname=%s,%s' % (rec['alias'], rec['target']))
    for rec in _enabled(dns.get('forwards', [])):
        lines.append('server=/%s/%s' % (rec['domain'], rec['upstream']))
    return '\n'.join(lines) + '\n'


def render_hosts(dns):
    lines = [HEADER]
    for rec in dns.get('hosts', []):
        comment = (' # %s' % rec['comment']) if rec.get('comment') else ''
        for ip_key in ('a', 'aaaa'):
            ip = rec.get(ip_key)
            if not ip:
                continue
            if rec.get('enabled', True):
                lines.append('%s %s%s' % (ip, rec['name'], comment))
            else:
                lines.append('# disabled: %s %s%s' % (ip, rec['name'], comment))
    return '\n'.join(lines) + '\n'


def render_dhcp(dhcp, settings):
    lines = [HEADER]
    if not settings.get('dhcp_enabled'):
        lines.append('# DHCP disabled from the UI')
        return '\n'.join(lines) + '\n'
    for r in _enabled(dhcp.get('ranges', [])):
        parts = []
        if r.get('interface'):
            parts.append('interface:%s' % r['interface'])
        if r.get('tag'):
            parts.append('set:%s' % r['tag'])
        parts += [r['start'], r['end']]
        if r.get('netmask'):
            parts.append(r['netmask'])
        parts.append(r.get('lease') or '12h')
        lines.append('dhcp-range=%s' % ','.join(parts))
    if settings.get('dhcp_authoritative'):
        lines.append('dhcp-authoritative')
    # External network boot: point PXE clients at a separate boot server. No
    # TFTP/proxy-DHCP here (that's the standalone appliance's job).
    boot = dhcp.get('boot') or {}
    if boot.get('filename'):
        srv = boot.get('server') or ''
        lines.append('dhcp-boot=%s%s' % (boot['filename'], ',,%s' % srv if srv else ''))
    lines.append('dhcp-hostsfile=%s' % DHCP_HOSTS_FILE)
    lines.append('dhcp-optsfile=%s' % DHCP_OPTS_FILE)
    return '\n'.join(lines) + '\n'


def render_dhcp_hosts(dhcp, settings):
    lines = [HEADER]
    if settings.get('dhcp_enabled'):
        for s in _enabled(dhcp.get('static_leases', [])):
            parts = [s['mac']]
            if s.get('tag'):
                parts.append('set:%s' % s['tag'])
            parts.append(s['ip'])
            if s.get('hostname'):
                parts.append(s['hostname'])
            lines.append(','.join(parts))
    return '\n'.join(lines) + '\n'


def render_dhcp_opts(dhcp, settings):
    lines = [HEADER]
    if settings.get('dhcp_enabled'):
        for o in _enabled(dhcp.get('options', [])):
            parts = []
            if o.get('tag'):
                parts.append('tag:%s' % o['tag'])
            opt = str(o['option'])
            parts.append(opt if (opt.startswith('option') or opt.isdigit()) else 'option:' + opt)
            if o.get('value'):
                parts.append(str(o['value']))
            lines.append(','.join(parts))
    return '\n'.join(lines) + '\n'


def render_extra(settings):
    text = settings.get('extra_options') or ''
    return HEADER + (text.rstrip('\n') + '\n' if text.strip() else '# (no extra options)\n')


def render_all(stores=None):
    """Render every managed file → {relpath-under-RENDER_DIR: text}."""
    if stores is None:
        stores = {name: load_store(name) for name in STORE_NAMES}
    s, d, h = stores['settings'], stores['dns'], stores['dhcp']
    return {
        'dnsmasq.d/00-main.conf': render_main(s),
        'dnsmasq.d/10-dns.conf': render_dns(d),
        'dnsmasq.d/20-dhcp.conf': render_dhcp(h, s),
        'dnsmasq.d/90-extra.conf': render_extra(s),
        'hosts.d/managed-hosts': render_hosts(d),
        'dhcp-hosts': render_dhcp_hosts(h, s),
        'dhcp-opts': render_dhcp_opts(h, s),
    }


# ─── Validation / diff / write / apply ────────────────────────────────

def validate_render(rendered):
    """`dnsmasq --test` the rendered fragments in a temp dir (rootless).
    Returns (ok, output)."""
    tmp = tempfile.mkdtemp(prefix='nexus-dnsmasq-validate-')
    try:
        confd = os.path.join(tmp, 'dnsmasq.d')
        os.makedirs(confd)
        for rel, text in rendered.items():
            if rel.startswith('dnsmasq.d/'):
                with open(os.path.join(confd, os.path.basename(rel)), 'w') as f:
                    f.write(text)
        testconf = os.path.join(tmp, 'test.conf')
        with open(testconf, 'w') as f:
            f.write('conf-dir=%s,*.conf\n' % confd)
        out, e, rc = run([DNSMASQ_BIN, '--test', '-C', testconf], no_sudo=True, timeout=15)
        return rc == 0, (e or out).strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def diff_render(rendered):
    """'none' | 'reload' (only SIGHUP-refreshable files changed) | 'restart'."""
    changed = []
    for rel, text in rendered.items():
        try:
            with open(os.path.join(RENDER_DIR, rel)) as f:
                if f.read() == text:
                    continue
        except OSError:
            pass
        changed.append(rel)
    if not changed:
        return 'none', changed
    if all(rel in HUP_ONLY for rel in changed):
        return 'reload', changed
    return 'restart', changed


def write_render(rendered):
    for rel, text in rendered.items():
        path = os.path.join(RENDER_DIR, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text_atomic(path, text, 0o644)


def ensure_render():
    rendered = render_all()
    missing = {rel: text for rel, text in rendered.items()
               if not os.path.exists(os.path.join(RENDER_DIR, rel))}
    if missing:
        write_render(missing)


# ─── Service control (systemd only — argument-pinned sudoers) ─────────

def svc_status():
    out, _, rc = run(['systemctl', 'is-active', DNSMASQ_UNIT], no_sudo=True)
    return {'running': out.strip() == 'active', 'state': out.strip() or 'unknown'}


def svc_restart():
    _, e, rc = run(['systemctl', 'restart', DNSMASQ_UNIT], timeout=30)
    return rc == 0, e


def svc_reload():
    _, e, rc = run(['systemctl', 'kill', '-s', 'HUP', DNSMASQ_UNIT])
    return rc == 0, e


def svc_logs(lines=200):
    out, e, rc = run(['journalctl', '-u', DNSMASQ_UNIT, '-n', str(int(lines)), '--no-pager'])
    return out if rc == 0 else (e or 'journalctl failed')


def dnsmasq_version():
    out, _, rc = run([DNSMASQ_BIN, '--version'], no_sudo=True, timeout=10)
    if rc == 0 and out:
        return out.splitlines()[0].replace('Dnsmasq version', '').strip().split()[0]
    return None


DROPIN = '/etc/dnsmasq.d/zz-%s.conf' % UNIT_PREFIX


def module_status():
    """Degradation-aware status (caddy `_caddy_status` pattern): what works on
    this node. Cheap enough for the summary hook."""
    st = svc_status()
    settings = load_store('settings')
    st.update({
        'installed': bool(shutil.which(DNSMASQ_BIN)),
        'version': dnsmasq_version(),
        'dropin_present': os.path.exists(DROPIN),
        'dns_enabled': settings.get('dns_enabled', True),
        'dhcp_enabled': settings.get('dhcp_enabled', False),
        'render_dir': RENDER_DIR,
    })
    return st


def _preconditions():
    """Mutation guard: refuse cleanly when the host can't actually serve the
    managed config. Returns error-response or None."""
    if not shutil.which(DNSMASQ_BIN):
        return err('dnsmasq is not installed on this node — `apt install dnsmasq` '
                   '(the module manages an existing dnsmasq; it does not install it)')
    if not os.path.exists(DROPIN):
        return err('The dnsmasq conf-dir drop-in (%s) is missing — it ships with '
                   'fresh installs; existing fleet nodes need it added by hand '
                   '(see the install docs)' % DROPIN, 409)
    return None


# ─── Apply pipeline ───────────────────────────────────────────────────

def apply_change(mutate, sections=('settings',)):
    """Single choke point: snapshot → mutate → render → `dnsmasq --test` →
    rollback on reject → atomic swap → reload/restart → status recheck."""
    with STORE_LOCK:
        snapshot = {n: copy.deepcopy(load_store(n)) for n in STORE_NAMES}
        mutate()
        rendered = render_all()
        ok, output = validate_render(rendered)
        if not ok:
            for n in STORE_NAMES:
                save_store(n, snapshot[n])
            return err('dnsmasq rejected the configuration: %s' % output, 400)
        action, changed = diff_render(rendered)
        write_render(rendered)
        for n in set(sections) & set(STORE_NAMES):
            bump_serial(n, load_store(n))

    service_ok, detail = True, ''
    if action == 'reload':
        service_ok, detail = svc_reload()
    elif action == 'restart':
        service_ok, detail = svc_restart()
    if action != 'none':
        time.sleep(0.5)
        if not svc_status().get('running'):
            service_ok, detail = False, detail or 'dnsmasq did not come back after %s' % action
    return {'action': action, 'changed': changed,
            'service_ok': service_ok, 'service_detail': detail}


# ─── Live leases ──────────────────────────────────────────────────────

def parse_leases(path=LEASES_FILE):
    leases = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                try:
                    expiry = int(parts[0])
                except ValueError:
                    continue
                leases.append({'expiry': expiry, 'mac': parts[1], 'ip': parts[2],
                               'hostname': parts[3] if parts[3] != '*' else '',
                               'client_id': parts[4] if len(parts) > 4 else ''})
    except OSError:
        pass
    return leases


# ─── CHAOS stats + history hook ───────────────────────────────────────
CHAOS_NAMES = {'cachesize': 'cachesize.bind', 'insertions': 'insertions.bind',
               'evictions': 'evictions.bind', 'hits': 'hits.bind', 'misses': 'misses.bind'}
COUNTER_KEYS = ('hits', 'misses', 'evictions', 'insertions')


def _build_query(name, qtype=16, qclass=3):
    qid = secrets.randbits(16)
    header = struct.pack('!HHHHHH', qid, 0, 1, 0, 0, 0)
    qname = b''.join(bytes([len(p)]) + p.encode() for p in name.split('.')) + b'\x00'
    return header + qname + struct.pack('!HH', qtype, qclass), qid


def _skip_name(buf, pos):
    while pos < len(buf):
        ln = buf[pos]
        if ln == 0:
            return pos + 1
        if ln & 0xC0 == 0xC0:
            return pos + 2
        pos += 1 + ln
    return pos


def _parse_txt(buf, qid):
    if len(buf) < 12:
        return None
    rid, flags, qd, an = struct.unpack('!HHHH', buf[:8])
    if rid != qid or an < 1:
        return None
    pos = 12
    for _ in range(qd):
        pos = _skip_name(buf, pos) + 4
    pos = _skip_name(buf, pos)
    if pos + 10 > len(buf):
        return None
    rtype, rclass, _ttl, rdlen = struct.unpack('!HHIH', buf[pos:pos + 10])
    pos += 10
    if rtype != 16 or pos + rdlen > len(buf):
        return None
    strings, end = [], pos + rdlen
    while pos < end:
        ln = buf[pos]
        strings.append(buf[pos + 1:pos + 1 + ln].decode(errors='replace'))
        pos += 1 + ln
    return strings


def chaos_txt(name, server='127.0.0.1', port=DNS_PORT, timeout=1.0):
    query, qid = _build_query(name)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(query, (server, port))
            buf, _ = s.recvfrom(4096)
        strings = _parse_txt(buf, qid)
        return strings[0] if strings else None
    except OSError:
        return None


def collect_dns_counters():
    vals = {}
    for key, name in CHAOS_NAMES.items():
        v = chaos_txt(name)
        if v is None:
            return {}
        try:
            vals[key] = int(v)
        except ValueError:
            return {}
    return vals


def pool_utilization(dhcp=None, leases=None):
    if dhcp is None:
        dhcp = load_store('dhcp')
    if leases is None:
        leases = parse_leases()
    lease_ips = []
    for l in leases:
        try:
            lease_ips.append(int(ipaddress.IPv4Address(l['ip'])))
        except (ValueError, KeyError):
            pass
    pools = []
    for r in dhcp.get('ranges', []):
        if not r.get('enabled', True):
            continue
        try:
            lo = int(ipaddress.IPv4Address(r['start']))
            hi = int(ipaddress.IPv4Address(r['end']))
        except ValueError:
            continue
        size = hi - lo + 1
        used = sum(1 for ip in lease_ips if lo <= ip <= hi)
        pools.append({'tag': r.get('tag') or r['start'], 'start': r['start'], 'end': r['end'],
                      'size': size, 'used': used,
                      'pct': round(used * 100.0 / size, 1) if size else 0.0})
    return pools


def collect_history_samples():
    """(metric, label, value) rows for the shared history store. Called by
    core/history.py's timer tick (only when the module is enabled). DNS
    counters are cumulative → per-tick deltas via an on-disk cursor."""
    rows = []
    settings = load_store('settings')
    if settings.get('dns_enabled', True):
        vals = collect_dns_counters()
        if vals:
            rows.append(('dns_cache_size', '', vals['cachesize']))
            cursor = load_store('stats_cursor')
            have_last = bool(cursor.get('ts'))
            for key in COUNTER_KEYS:
                cur = vals[key]
                if have_last:
                    delta = cur - int(cursor.get(key, 0))
                    rows.append(('dns_%s' % key, '', cur if delta < 0 else delta))
                cursor[key] = cur
            cursor['ts'] = int(time.time())
            save_store('stats_cursor', cursor)
    rows.append(('dhcp_leases', '', len(parse_leases())))
    return rows


# ─── Module hooks (summary + alerts; registry calls only when enabled) ─

def summary():
    """CHEAP dashboard-summary block — no CHAOS calls. Runs on every 30s
    /api/summary fan-out, so keep it to is-active + a lease-file read."""
    st = svc_status()
    settings = load_store('settings')
    leases = parse_leases()
    return {'installed': bool(shutil.which(DNSMASQ_BIN)), 'active': st['running'],
            'dns_enabled': settings.get('dns_enabled', True),
            'dhcp_enabled': settings.get('dhcp_enabled', False),
            'leases': len(leases), 'hosts': len(load_store('dns').get('hosts', []))}


def alerts():
    # Only alert on a node that is ACTUALLY wired to serve dnsmasq via our
    # drop-in (avoids noise on a freshly-enabled-but-unconfigured module, and
    # keeps it host-deterministic in tests).
    if not os.path.exists(DROPIN) or not shutil.which(DNSMASQ_BIN):
        return []
    settings = load_store('settings')
    if not (settings.get('dns_enabled', True) or settings.get('dhcp_enabled')):
        return []
    if svc_status().get('running'):
        return []
    return [{'key': 'dnsmasq-down', 'message': 'dnsmasq is enabled but not running'}]


# ─── DHCP probe (privileged; runs as `app.py dhcp-probe`) ─────────────
PROBE_TIMEOUT = 2.0
SO_BINDTODEVICE = 25


def _iface_mac(iface):
    try:
        with open('/sys/class/net/%s/address' % iface) as f:
            return bytes.fromhex(f.read().strip().replace(':', ''))
    except (OSError, ValueError):
        return None


def build_discover(xid, mac):
    pkt = struct.pack('!BBBBIHH', 1, 1, 6, 0, xid, 0, 0x8000)
    pkt += b'\x00' * 16
    pkt += mac + b'\x00' * (16 - len(mac))
    pkt += b'\x00' * 64 + b'\x00' * 128
    pkt += b'\x63\x82\x53\x63'
    pkt += bytes([53, 1, 1]) + bytes([55, 3, 1, 3, 6]) + bytes([255])
    return pkt + b'\x00' * max(0, 300 - len(pkt))


def parse_offer(buf, xid):
    if len(buf) < 244 or buf[0] != 2:
        return None
    if struct.unpack('!I', buf[4:8])[0] != xid or buf[236:240] != b'\x63\x82\x53\x63':
        return None
    offer_ip = socket.inet_ntoa(buf[16:20])
    msg_type, server_id, pos = None, None, 240
    while pos + 1 < len(buf):
        opt = buf[pos]
        if opt == 255:
            break
        if opt == 0:
            pos += 1
            continue
        ln = buf[pos + 1]
        val = buf[pos + 2:pos + 2 + ln]
        if opt == 53 and ln >= 1:
            msg_type = val[0]
        elif opt == 54 and ln >= 4:
            server_id = socket.inet_ntoa(val[:4])
        pos += 2 + ln
    if msg_type != 2:
        return None
    return {'offer_ip': offer_ip, 'server_id': server_id}


def probe_interface(iface=None, timeout=PROBE_TIMEOUT):
    xid = secrets.randbits(32)
    mac = (_iface_mac(iface) if iface else None) or (bytes([0x02, 0x00]) + secrets.token_bytes(4))
    servers = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if iface:
                s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b'\x00')
            s.bind(('', 68))
            s.sendto(build_discover(xid, mac), ('255.255.255.255', 67))
            deadline = time.time() + timeout
            while time.time() < deadline:
                s.settimeout(max(0.05, deadline - time.time()))
                try:
                    buf, addr = s.recvfrom(2048)
                except socket.timeout:
                    break
                offer = parse_offer(buf, xid)
                if offer:
                    server = offer['server_id'] or addr[0]
                    servers.setdefault(server, {'server': server,
                                                'offer_ip': offer['offer_ip'], 'iface': iface or ''})
        finally:
            s.close()
    except OSError as e:
        return [], str(e)
    return list(servers.values()), None


def cli_dhcp_probe(argv=None):
    """`app.py dhcp-probe [iface ...]` — prints JSON, always exits 0. Runs
    privileged (sudo, port-68 bind) via a pinned sudoers line."""
    ifaces = [a for a in (argv[2:] if argv else [])
              if _re.match(r'^[A-Za-z0-9._@-]{1,15}$', a)]
    all_servers, errors = [], []
    for iface in (ifaces or [None]):
        srv, e = probe_interface(iface)
        all_servers.extend(srv)
        if e:
            errors.append('%s: %s' % (iface or 'default', e))
    print(json.dumps({'servers': all_servers, 'errors': errors}))
    return 0


def _local_ipv4s():
    out, _, _ = run(['ip', '-4', '-o', 'addr', 'show'], no_sudo=True)
    return set(_re.findall(r'inet (\d+\.\d+\.\d+\.\d+)', out or ''))


def probe_for_foreign_dhcp(interfaces):
    import sys
    cmd = [sys.executable, os.path.join(APP_DIR, 'app.py'), 'dhcp-probe'] + list(interfaces)
    out, e, rc = run(cmd, timeout=15)
    if rc != 0:
        return {'servers': [], 'error': (e or 'probe failed').strip().splitlines()[-1]}
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'servers': [], 'error': 'unparseable probe output'}
    local = _local_ipv4s()
    foreign = [s for s in data.get('servers', []) if s.get('server') not in local]
    return {'servers': foreign, 'error': '; '.join(data.get('errors') or []) or None}


# ─── Routes ───────────────────────────────────────────────────────────

@bp.route('/api/dnsmasq/status')
def route_status():
    return jsonify(module_status())


@bp.route('/api/dnsmasq/config')
def route_config():
    files = {}
    for rel in sorted(render_all().keys()):
        try:
            with open(os.path.join(RENDER_DIR, rel)) as f:
                files[rel] = f.read()
        except OSError:
            files[rel] = '(not rendered yet)'
    return jsonify({'files': files, 'render_dir': RENDER_DIR})


@bp.route('/api/dnsmasq/validate', methods=['POST'])
def route_validate():
    with STORE_LOCK:
        rendered = render_all()
    ok, output = validate_render(rendered)
    action, changed = diff_render(rendered)
    return jsonify({'success': True, 'valid': ok, 'output': output or 'syntax check OK',
                    'pending_action': action, 'pending_files': changed})


@bp.route('/api/dnsmasq/apply', methods=['POST'])
def route_apply():
    pre = _preconditions()
    if pre:
        return pre
    with STORE_LOCK:
        rendered = render_all()
        ok, output = validate_render(rendered)
        if not ok:
            return err('dnsmasq rejected the configuration: %s' % output, 400)
        write_render(rendered)
    service_ok, detail = svc_restart()
    return jsonify({'success': True, 'service_ok': service_ok, 'service_detail': detail})


@bp.route('/api/dnsmasq/restart', methods=['POST'])
def route_restart():
    pre = _preconditions()
    if pre:
        return pre
    service_ok, detail = svc_restart()
    if not service_ok:
        return err('Restart failed: %s' % detail, 500)
    return jsonify({'success': True})


@bp.route('/api/dnsmasq/logs')
def route_logs():
    return jsonify({'logs': svc_logs()})


@bp.route('/api/dnsmasq/stats')
def route_stats():
    settings = load_store('settings')
    out = {'dns': None, 'dhcp': None}
    if settings.get('dns_enabled', True):
        vals = collect_dns_counters()
        if vals:
            total = vals['hits'] + vals['misses']
            vals['hit_ratio'] = round(vals['hits'] * 100.0 / total, 1) if total else None
            out['dns'] = vals
    leases = parse_leases()
    out['dhcp'] = {'active_leases': len(leases), 'pools': pool_utilization(leases=leases)}
    return jsonify(out)


# ---- DNS CRUD ----
DNS_COLLS = ('hosts', 'cnames', 'addresses', 'forwards')
BOILERPLATE_NAMES = {'localhost', 'localhost.localdomain', 'localhost4', 'localhost6',
                     'broadcasthost', 'ip6-localhost', 'ip6-loopback', 'ip6-localnet',
                     'ip6-mcastprefix', 'ip6-allnodes', 'ip6-allrouters', 'ip6-allhosts'}
MAX_IMPORT_BYTES = 2_000_000


def _dns_validate(coll, data):
    rec = {'enabled': bool(data.get('enabled', True)), 'comment': str(data.get('comment') or '')}
    if not RE_COMMENT.match(rec['comment']):
        return None, 'Invalid comment'
    if coll == 'hosts':
        name = (data.get('name') or '').strip()
        a = (data.get('a') or '').strip()
        aaaa = (data.get('aaaa') or '').strip()
        if not valid_hostname_fqdn(name):
            return None, 'Invalid hostname'
        if not a and not aaaa:
            return None, 'At least one of A (IPv4) or AAAA (IPv6) is required'
        if a and not is_ipv4(a):
            return None, 'Invalid IPv4 address'
        if aaaa and not is_ipv6(aaaa):
            return None, 'Invalid IPv6 address'
        rec.update({'name': name, 'a': a, 'aaaa': aaaa})
    elif coll == 'cnames':
        alias = (data.get('alias') or '').strip()
        target = (data.get('target') or '').strip()
        if not valid_hostname_fqdn(alias) or not valid_hostname_fqdn(target):
            return None, 'Invalid alias or target'
        rec.update({'alias': alias, 'target': target})
    elif coll == 'addresses':
        domain = (data.get('domain') or '').strip()
        ip = (data.get('ip') or '').strip()
        if not RE_DOMAIN.match(domain):
            return None, 'Invalid domain'
        if not is_ip(ip):
            return None, 'Invalid IP address'
        rec.update({'domain': domain, 'ip': ip})
    elif coll == 'forwards':
        domain = (data.get('domain') or '').strip()
        upstream = (data.get('upstream') or '').strip()
        if not RE_DOMAIN.match(domain):
            return None, 'Invalid domain'
        if not is_upstream(upstream):
            return None, 'Invalid upstream (use IP or IP#port)'
        rec.update({'domain': domain, 'upstream': upstream})
    return rec, None


def _dns_section(coll):
    return 'hosts' if coll == 'hosts' else 'dns'


@bp.route('/api/dnsmasq/dns')
def route_dns_get():
    return jsonify(load_store('dns'))


def parse_hosts_text(text, skip_boilerplate=True):
    entries, skipped, invalid = [], 0, 0
    for raw_line in text.splitlines():
        line = raw_line.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            invalid += 1
            continue
        ip = parts[0]
        if is_ipv4(ip):
            key = 'a'
        elif is_ipv6(ip):
            key = 'aaaa'
        else:
            invalid += 1
            continue
        for name in parts[1:]:
            name = name.rstrip('.')
            if skip_boilerplate and name.lower() in BOILERPLATE_NAMES:
                skipped += 1
                continue
            if not valid_hostname_fqdn(name):
                invalid += 1
                continue
            entries.append((name, key, ip))
    return entries, skipped, invalid


@bp.route('/api/dnsmasq/dns/import', methods=['POST'])
def route_dns_import():
    data = request.get_json() or {}
    text = str(data.get('text') or '')
    if not text.strip():
        return err('Nothing to import')
    if len(text) > MAX_IMPORT_BYTES:
        return err('Import too large (max 2 MB)')
    replace = bool(data.get('replace'))
    entries, skipped, invalid = parse_hosts_text(text, bool(data.get('skip_boilerplate', True)))
    if not entries:
        return err('No usable host entries found (%d invalid, %d boilerplate skipped)'
                   % (invalid, skipped))
    counts = {'added': 0, 'updated': 0, 'unchanged': 0, 'skipped': skipped, 'invalid': invalid}

    def mutate():
        d = load_store('dns')
        hosts = [] if replace else list(d['hosts'])
        by_name = {h['name']: h for h in hosts}
        for name, key, ip in entries:
            rec = by_name.get(name)
            if rec is None:
                rec = {'id': new_id('h'), 'name': name, 'a': '', 'aaaa': '',
                       'enabled': True, 'comment': 'imported'}
                rec[key] = ip
                hosts.append(rec)
                by_name[name] = rec
                counts['added'] += 1
            elif rec.get(key) != ip:
                rec[key] = ip
                counts['updated'] += 1
            else:
                counts['unchanged'] += 1
        d['hosts'] = hosts
        save_store('dns', d)

    res = apply_change(mutate, sections=['hosts'])
    if isinstance(res, tuple):
        return res
    return jsonify({'success': True, **counts, **res})


@bp.route('/api/dnsmasq/dns/<coll>', methods=['POST'])
def route_dns_add(coll):
    if coll not in DNS_COLLS:
        return err('Unknown collection', 404)
    rec, e = _dns_validate(coll, request.get_json() or {})
    if e:
        return err(e)
    rec['id'] = new_id(coll[0])

    def mutate():
        d = load_store('dns')
        d[coll].append(rec)
        save_store('dns', d)

    res = apply_change(mutate, sections=[_dns_section(coll)])
    return res if isinstance(res, tuple) else jsonify({'success': True, 'id': rec['id'], **res})


@bp.route('/api/dnsmasq/dns/<coll>/<rid>', methods=['POST'])
def route_dns_update(coll, rid):
    if coll not in DNS_COLLS:
        return err('Unknown collection', 404)
    if not find_record(load_store('dns')[coll], rid):
        return err('No such record', 404)
    rec, e = _dns_validate(coll, request.get_json() or {})
    if e:
        return err(e)
    rec['id'] = rid

    def mutate():
        d = load_store('dns')
        d[coll] = [rec if it.get('id') == rid else it for it in d[coll]]
        save_store('dns', d)

    res = apply_change(mutate, sections=[_dns_section(coll)])
    return res if isinstance(res, tuple) else jsonify({'success': True, **res})


@bp.route('/api/dnsmasq/dns/<coll>/<rid>', methods=['DELETE'])
def route_dns_delete(coll, rid):
    if coll not in DNS_COLLS:
        return err('Unknown collection', 404)
    if not find_record(load_store('dns')[coll], rid):
        return err('No such record', 404)

    def mutate():
        d = load_store('dns')
        d[coll] = [it for it in d[coll] if it.get('id') != rid]
        save_store('dns', d)

    res = apply_change(mutate, sections=[_dns_section(coll)])
    return res if isinstance(res, tuple) else jsonify({'success': True, **res})


# ---- DHCP CRUD ----
DHCP_COLLS = ('ranges', 'static_leases', 'options')


def _dhcp_validate(coll, data):
    rec = {'enabled': bool(data.get('enabled', True)), 'comment': str(data.get('comment') or '')}
    if not RE_COMMENT.match(rec['comment']):
        return None, 'Invalid comment'
    tag = (data.get('tag') or '').strip()
    if tag and not RE_TAG.match(tag):
        return None, 'Invalid tag'
    rec['tag'] = tag
    if coll == 'ranges':
        start = (data.get('start') or '').strip()
        end = (data.get('end') or '').strip()
        netmask = (data.get('netmask') or '').strip()
        lease = (data.get('lease') or '12h').strip()
        iface = (data.get('interface') or '').strip()
        if not is_ipv4(start) or not is_ipv4(end):
            return None, 'Start and end must be IPv4 addresses'
        if int(ipaddress.IPv4Address(end)) < int(ipaddress.IPv4Address(start)):
            return None, 'Range end is before its start'
        if netmask and not is_ipv4(netmask):
            return None, 'Invalid netmask'
        if not RE_LEASE.match(lease):
            return None, 'Invalid lease time (e.g. 12h, 90m, infinite)'
        if iface and not RE_IFACE.match(iface):
            return None, 'Invalid interface'
        if iface and rec.get('tag'):
            return None, 'A range can have a tag or an interface, not both'
        rec.update({'start': start, 'end': end, 'netmask': netmask, 'lease': lease,
                    'interface': iface})
    elif coll == 'static_leases':
        mac = (data.get('mac') or '').strip().lower()
        ip = (data.get('ip') or '').strip()
        hostname = (data.get('hostname') or '').strip()
        if not RE_MAC.match(mac):
            return None, 'Invalid MAC address'
        if not is_ipv4(ip):
            return None, 'Invalid IPv4 address'
        if hostname and not RE_HOSTNAME.match(hostname):
            return None, 'Invalid hostname'
        rec.update({'mac': mac, 'ip': ip, 'hostname': hostname})
    elif coll == 'options':
        option = str(data.get('option') or '').strip()
        value = str(data.get('value') or '').strip()
        if not RE_DHCP_OPTION.match(option):
            return None, 'Invalid option (number or option:name)'
        if value and not RE_OPT_VALUE.match(value):
            return None, 'Invalid option value'
        rec.update({'option': option, 'value': value})
    return rec, None


def _dhcp_dup(coll, items, rec, skip_id=None):
    if coll == 'static_leases':
        for it in items:
            if it.get('id') != skip_id and it.get('mac') == rec['mac']:
                return 'A static lease for %s already exists' % rec['mac']
    return None


@bp.route('/api/dnsmasq/dhcp')
def route_dhcp_get():
    return jsonify(load_store('dhcp'))


@bp.route('/api/dnsmasq/dhcp/leases')
def route_dhcp_leases():
    now = int(time.time())
    leases = parse_leases()
    statics = {s['mac'] for s in load_store('dhcp').get('static_leases', [])}
    for l in leases:
        l['expires_in'] = max(0, l['expiry'] - now) if l['expiry'] else None
        l['static'] = l['mac'] in statics
    return jsonify({'leases': leases, 'count': len(leases)})


@bp.route('/api/dnsmasq/dhcp/boot', methods=['POST'])
def route_dhcp_boot():
    data = request.get_json() or {}
    filename = (data.get('filename') or '').strip()
    server = (data.get('server') or '').strip()
    if filename and not RE_BOOT_FILE.match(filename):
        return err('Invalid boot filename')
    if server and not (is_ipv4(server) or valid_hostname_fqdn(server)):
        return err('Boot server must be an IPv4 address or hostname')
    if server and not filename:
        return err('A boot filename is required when a boot server is set')

    def mutate():
        d = load_store('dhcp')
        d['boot'] = {'filename': filename, 'server': server}
        save_store('dhcp', d)

    res = apply_change(mutate, sections=['dhcp'])
    return res if isinstance(res, tuple) else jsonify({'success': True, **res})


@bp.route('/api/dnsmasq/dhcp/leases/reserve', methods=['POST'])
def route_dhcp_reserve():
    rec, e = _dhcp_validate('static_leases', request.get_json() or {})
    if e:
        return err(e)
    dup = _dhcp_dup('static_leases', load_store('dhcp')['static_leases'], rec)
    if dup:
        return err(dup, 409)
    rec['id'] = new_id('s')

    def mutate():
        d = load_store('dhcp')
        d['static_leases'].append(rec)
        save_store('dhcp', d)

    res = apply_change(mutate, sections=['dhcp'])
    return res if isinstance(res, tuple) else jsonify({'success': True, 'id': rec['id'], **res})


@bp.route('/api/dnsmasq/dhcp/<coll>', methods=['POST'])
def route_dhcp_add(coll):
    if coll not in DHCP_COLLS:
        return err('Unknown collection', 404)
    rec, e = _dhcp_validate(coll, request.get_json() or {})
    if e:
        return err(e)
    dup = _dhcp_dup(coll, load_store('dhcp')[coll], rec)
    if dup:
        return err(dup, 409)
    rec['id'] = new_id(coll[0])

    def mutate():
        d = load_store('dhcp')
        d[coll].append(rec)
        save_store('dhcp', d)

    res = apply_change(mutate, sections=['dhcp'])
    return res if isinstance(res, tuple) else jsonify({'success': True, 'id': rec['id'], **res})


@bp.route('/api/dnsmasq/dhcp/<coll>/<rid>', methods=['POST'])
def route_dhcp_update(coll, rid):
    if coll not in DHCP_COLLS:
        return err('Unknown collection', 404)
    if not find_record(load_store('dhcp')[coll], rid):
        return err('No such record', 404)
    rec, e = _dhcp_validate(coll, request.get_json() or {})
    if e:
        return err(e)
    dup = _dhcp_dup(coll, load_store('dhcp')[coll], rec, skip_id=rid)
    if dup:
        return err(dup, 409)
    rec['id'] = rid

    def mutate():
        d = load_store('dhcp')
        d[coll] = [rec if it.get('id') == rid else it for it in d[coll]]
        save_store('dhcp', d)

    res = apply_change(mutate, sections=['dhcp'])
    return res if isinstance(res, tuple) else jsonify({'success': True, **res})


@bp.route('/api/dnsmasq/dhcp/<coll>/<rid>', methods=['DELETE'])
def route_dhcp_delete(coll, rid):
    if coll not in DHCP_COLLS:
        return err('Unknown collection', 404)
    if not find_record(load_store('dhcp')[coll], rid):
        return err('No such record', 404)

    def mutate():
        d = load_store('dhcp')
        d[coll] = [it for it in d[coll] if it.get('id') != rid]
        save_store('dhcp', d)

    res = apply_change(mutate, sections=['dhcp'])
    return res if isinstance(res, tuple) else jsonify({'success': True, **res})


# ---- Settings + toggles ----
BOOL_KEYS = ('expand_hosts', 'bind_interfaces', 'no_resolv', 'domain_needed',
             'bogus_priv', 'dnssec', 'dhcp_authoritative', 'log_queries', 'log_dhcp')
MAX_EXTRA = 20000


def _settings_validate(data, cur):
    s = dict(cur)
    if 'domain' in data:
        dom = (data['domain'] or '').strip()
        if dom and not RE_DOMAIN.match(dom):
            return None, 'Invalid domain'
        s['domain'] = dom
    if 'interfaces' in data:
        ifaces = [str(i).strip() for i in (data['interfaces'] or []) if str(i).strip()]
        if any(not RE_IFACE.match(i) for i in ifaces):
            return None, 'Invalid interface name'
        s['interfaces'] = ifaces
    if 'listen_addresses' in data:
        addrs = [str(a).strip() for a in (data['listen_addresses'] or []) if str(a).strip()]
        if any(not is_ip(a) for a in addrs):
            return None, 'Invalid listen address'
        s['listen_addresses'] = addrs
    if 'upstreams' in data:
        ups = [str(u).strip() for u in (data['upstreams'] or []) if str(u).strip()]
        if any(not is_upstream(u) for u in ups):
            return None, 'Invalid upstream server (use IP or IP#port)'
        s['upstreams'] = ups
    if 'cache_size' in data:
        try:
            n = int(data['cache_size'])
        except (TypeError, ValueError):
            return None, 'Invalid cache size'
        if not 0 <= n <= 10_000_000:
            return None, 'Cache size out of range'
        s['cache_size'] = n
    if 'extra_options' in data:
        extra = str(data['extra_options'] or '')
        if len(extra) > MAX_EXTRA or '\x00' in extra:
            return None, 'Extra options too large'
        s['extra_options'] = extra
    for k in BOOL_KEYS:
        if k in data:
            s[k] = bool(data[k])
    return s, None


@bp.route('/api/dnsmasq/settings')
def route_settings_get():
    return jsonify(load_store('settings'))


@bp.route('/api/dnsmasq/settings', methods=['POST'])
def route_settings_save():
    cur = load_store('settings')
    new, e = _settings_validate(request.get_json() or {}, cur)
    if e:
        return err(e)
    res = apply_change(lambda: save_store('settings', new), sections=['settings'])
    return res if isinstance(res, tuple) else jsonify({'success': True,
                                                       'settings': load_store('settings'), **res})


@bp.route('/api/dnsmasq/settings/toggles', methods=['POST'])
def route_toggles():
    data = request.get_json() or {}
    cur = load_store('settings')
    probe_note = None
    if data.get('dhcp_enabled') and not cur.get('dhcp_enabled') and not data.get('force'):
        result = probe_for_foreign_dhcp(cur.get('interfaces') or [])
        if result['servers']:
            names = ', '.join(s['server'] for s in result['servers'])
            return jsonify({'success': False, 'conflict': True, 'servers': result['servers'],
                            'error': 'Another DHCP server is already active on this '
                                     'network: %s' % names}), 409
        probe_note = result.get('error')
    for k in ('dns_enabled', 'dhcp_enabled'):
        if k in data:
            cur[k] = bool(data[k])
    res = apply_change(lambda: save_store('settings', cur), sections=['settings'])
    return res if isinstance(res, tuple) else jsonify(
        {'success': True, 'dns_enabled': cur['dns_enabled'],
         'dhcp_enabled': cur['dhcp_enabled'], 'probe_note': probe_note, **res})


# ─── Module descriptor ────────────────────────────────────────────────
MODULE = {
    'id': 'dnsmasq',
    'label': 'DNS & DHCP',
    'category': 'DNS',
    'blueprint': bp,
    'summary': summary,
    'alerts': alerts,
    'cli': {'dhcp-probe': cli_dhcp_probe},
    'default_enabled': False,
}
