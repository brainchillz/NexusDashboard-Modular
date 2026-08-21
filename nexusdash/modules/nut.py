"""NUT (Network UPS Tools) server — the node the UPS is actually plugged into.

NUT splits into two halves that live on different machines, so this repo
splits them into two modules:

  * THIS module (`nut`, "UPS Server") manages the server half — the UPS
    devices in `ups.conf` (one driver process each), who may connect in
    `upsd.conf`/`upsd.users`, and the `nut.conf` MODE that decides which
    daemons run at all. It belongs on the one node with the USB/serial cable.
  * `upsmon.py` (`upsmon`, "UPS Monitor") manages the client half — the
    MONITOR lines, shutdown behaviour and notification matrix in
    `upsmon.conf`. That belongs on EVERY node the UPS feeds, which is why it
    is a separate toggle: a node with no UPS attached still needs to react
    when the one across the room goes on battery.

Both halves share this file's primitives (config-dir detection, the NUT
config lexer, the read/write path through the root helper, `upsc` snapshots)
— `upsmon.py` imports them, the smb/minidlna `_yn` precedent.

Three facts about NUT drove the design, all of them observed on the fleet
rather than assumed:

  * THE CONFIG DIR IS NOT THE SAME EVERYWHERE. Debian/Ubuntu use /etc/nut,
    RHEL/Rocky use /etc/ups. Both are detected; neither is hard-coded.
  * THE FILES ARE root:nut 0640, so the dashboard user cannot read them at
    all — not even to render a read-only page. Reads try a direct open first
    (the AI nodes run as root, and a node could put the service user in the
    `nut` group) and fall back to the root-owned <prefix>-nut helper. Writes
    ALWAYS go through the helper: it is the trust boundary.
  * THEY CONTAIN PLAINTEXT PASSWORDS — upsd.users, and every MONITOR line.
    Those never leave the process: the API reports only whether a password is
    set, and an update that omits one keeps the stored value (the Hugging
    Face token precedent). That is also why there is no raw-file editor here
    the way there is for Caddy: a textarea over upsmon.conf would hand back
    the passwords it was meant to hide, and SHUTDOWNCMD is run by root.

Every rewrite is a MERGE, never a regeneration. A packaged upsmon.conf is
usually wired to `upssched` through NOTIFYCMD, and ups.conf sections carry
vendorid/productid matching the UI does not surface; unmanaged directives,
section parameters, comments and ordering all survive a save untouched (the
Samba registry-share precedent).
"""
import json
import os
import re
import shutil

from flask import Blueprint, jsonify, request

from ..core.config import HELPER_PREFIX
from ..core.runcmd import run, err
from ..core.validators import RE_NUM

bp = Blueprint('nut', __name__)

NUT_HELPER = HELPER_PREFIX + '-nut'

# Distro-dependent config dir (see the module docstring). Ordered by how
# likely a hit is to be the real one; the env var wins outright.
_CONF_DIR_CANDIDATES = ('/etc/nut', '/etc/ups', '/usr/local/etc/nut',
                        '/usr/local/ups/etc')
# The complete set of files this module and upsmon.py will ever read or write.
# The helper enforces the same list — the app's copy is UI convenience, the
# helper's is the boundary.
MANAGED_FILES = ('nut.conf', 'ups.conf', 'upsd.conf', 'upsd.users',
                 'upsmon.conf')

NUT_SERVER_SERVICE = 'nut-server'          # upsd
NUT_MONITOR_SERVICE = 'nut-monitor'        # upsmon (same name on both families)
NUT_DRIVER_TARGET = 'nut-driver.target'
NUT_ENUMERATOR_SERVICE = 'nut-driver-enumerator'

# nut.conf MODE — which daemons the boot scripts start. `none` is the stock
# post-install value (NUT installed but deliberately inert).
NUT_MODES = ('none', 'standalone', 'netserver', 'netclient')

# Driver binaries move around by family (RHEL drops them straight in
# /usr/sbin, Debian keeps them under /lib/nut), so availability is probed by
# name across every location rather than by listing one blessed directory.
_DRIVER_DIRS = ('/lib/nut', '/usr/lib/nut', '/usr/libexec/nut',
                '/usr/local/libexec/nut', '/usr/sbin', '/usr/local/sbin',
                '/usr/bin')
# The drivers worth offering in a picker: USB/serial consumer and small
# rack UPSes plus the network protocols. NUT ships ~60; a list this size
# covers everything on this fleet and stays readable. A driver the file
# already names is always shown even when it is not in here.
KNOWN_DRIVERS = (
    'usbhid-ups', 'nutdrv_qx', 'blazer_usb', 'blazer_ser', 'apcupsd-ups',
    'apcsmart', 'apcsmart-old', 'bcmxcp', 'bcmxcp_usb', 'belkin', 'belkinunv',
    'bestups', 'bestups', 'dummy-ups', 'genericups', 'liebert', 'liebert-esp2',
    'mge-shut', 'mge-utalk', 'microdowell', 'nutdrv_atcl_usb', 'oneac',
    'optiups', 'powercom', 'powerpanel', 'richcomm_usb', 'riello_ser',
    'riello_usb', 'snmp-ups', 'netxml-ups', 'tripplite', 'tripplite_usb',
    'tripplitesu', 'upscode2', 'usbhid-ups', 'victronups')

# ─── Validators (\Z-anchored, repo convention) ────────────────────────
# A NUT section name — the [name] in ups.conf / upsd.users. NUT itself is
# looser, but these become systemd instance names (nut-driver@<name>).
RE_UPS_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z')
RE_NUT_USER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z')
RE_NUT_DRIVER = re.compile(r'^[a-z][a-z0-9_.-]{0,31}\Z')
# `port` is 'auto', a device node (/dev/ttyUSB0 — hence the leading slash in
# the first character class), or a host[:port] for the network drivers.
RE_NUT_PORT = re.compile(r'^[A-Za-z0-9/][A-Za-z0-9/:._@-]{0,127}\Z')
RE_NUT_DESC = re.compile(r'^[^\n\r"\\]{0,96}\Z')
# LISTEN takes an address only — `*`, an IPv4/IPv6 literal, or a hostname.
RE_NUT_LISTEN = re.compile(r'^(\*|[A-Za-z0-9][A-Za-z0-9:.\[\]_-]{0,127})\Z')
# Passwords are written verbatim into a config file NUT lexes, so quotes,
# backslashes, whitespace and '#' are out; everything else printable is fine.
RE_NUT_PASSWORD = re.compile(r'^[A-Za-z0-9!$%()*+,./:;<=>?@^_{|}~\[\]-]{1,128}\Z')
# Free-form driver parameters (vendorid, productid, offdelay, community, …).
RE_NUT_PARAM_KEY = re.compile(r'^[a-z][a-z0-9_.-]{0,31}\Z')
RE_NUT_PARAM_VAL = re.compile(r'^[^\n\r"\\]{0,128}\Z')

# upsd.users `actions` / `instcmds` are keyword lists NUT matches literally.
RE_NUT_ACTIONS = re.compile(r'^[A-Za-z0-9_. -]{0,128}\Z')

# Parameters ups.conf sections use that this module surfaces as first-class
# fields; anything else in a section is preserved as an "extra" parameter.
_UPS_CORE_KEYS = ('driver', 'port', 'desc')


# ─── Config location + privileged read/write ──────────────────────────
def conf_dir():
    """The NUT config directory on this host. Env override first, then the
    first candidate that actually holds a NUT config file, then the first
    that merely exists (a freshly installed NUT with an empty dir), then the
    Debian default so paths in error messages are never blank."""
    env = os.environ.get('DASHBOARD_NUT_CONF_DIR')
    if env:
        return env
    for d in _CONF_DIR_CANDIDATES:
        if any(os.path.exists(os.path.join(d, f)) for f in MANAGED_FILES):
            return d
    for d in _CONF_DIR_CANDIDATES:
        if os.path.isdir(d):
            return d
    return _CONF_DIR_CANDIDATES[0]


def conf_path(name):
    return os.path.join(conf_dir(), name)


def helper_present():
    return os.path.exists(NUT_HELPER)


def _read_direct(name):
    """The file's text, or None when it is unreadable — which is the NORMAL
    case (root:nut 0640) and not an error."""
    try:
        with open(conf_path(name)) as f:
            return f.read()
    except OSError:
        return None


def read_all():
    """{name: text} for every managed file that exists, in ONE privileged
    call. Files the dashboard user can already read are read directly; the
    helper is invoked only if at least one file needs it, so a node whose
    service user is in the `nut` group (or runs as root) never shells out.
    A missing/unreadable file is simply absent from the dict."""
    out, need_helper = {}, False
    for name in MANAGED_FILES:
        path = conf_path(name)
        text = _read_direct(name)
        if text is not None:
            out[name] = text
        elif os.path.exists(path) or not os.path.isdir(conf_dir()):
            # Exists but unreadable — or the whole dir is hidden from us.
            need_helper = True
    if need_helper and helper_present():
        raw, _e, rc = run([NUT_HELPER, 'read'], timeout=30)
        if rc == 0:
            try:
                payload = json.loads(raw)
                for name, text in (payload.get('files') or {}).items():
                    if name in MANAGED_FILES and isinstance(text, str):
                        out.setdefault(name, text)
            except (ValueError, AttributeError, TypeError):
                pass          # a garbled helper reply degrades to read-only
    return out


def write_conf(name, text):
    """Hand a complete candidate file to the root helper. It writes atomically
    with owner/mode preserved, applies it to the units that consume that file,
    and RESTORES the previous file (exiting non-zero) if the daemon refuses to
    come back — so the file on disk and the running config never diverge.
    Returns (out, err, rc)."""
    if name not in MANAGED_FILES:
        return '', 'refusing to write unmanaged file %r' % name, 1
    return run([NUT_HELPER, 'write', name], input_data=text, timeout=120)


def write_or_err(name, text):
    out, errout, rc = write_conf(name, text)
    if rc != 0:
        return err((errout or out).strip()[-2000:]
                   or 'writing %s failed' % name, 500)
    return jsonify({'success': True})


def editable_or_err(files):
    """Common precondition for every mutation: NUT present and the helper
    installed. Returns (files, None) or (None, error response)."""
    if not helper_present():
        return None, err(
            'The NUT helper is missing on this node — it ships with fresh '
            'installs; older nodes need the helper and its sudoers line '
            'added (deploy/fleet-deploy.sh --helpers)')
    if not os.path.isdir(conf_dir()):
        return None, err('%s does not exist — is NUT installed?' % conf_dir())
    return files, None


# ─── NUT config lexer / renderer ──────────────────────────────────────
def nut_tokens(line):
    """Split a NUT directive line into tokens the way NUT's own parser does:
    whitespace-separated, double quotes group, backslash escapes the next
    character. `SHUTDOWNCMD "/sbin/shutdown -h +0"` is ONE argument."""
    out, cur, quoted, esc, started = [], [], False, False, False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if ch == '\\':
            esc, started = True, True
            continue
        if ch == '"':
            quoted, started = not quoted, True
            continue
        if not quoted and ch in ' \t':
            if started:
                out.append(''.join(cur))
                cur, started = [], False
            continue
        cur.append(ch)
        started = True
    if started:
        out.append(''.join(cur))
    return out


def nut_quote(value):
    """Render one token, quoting when NUT's lexer would otherwise split or
    mis-read it."""
    s = '' if value is None else str(value)
    if s == '' or any(c in s for c in ' \t"\\#'):
        return '"%s"' % s.replace('\\', '\\\\').replace('"', '\\"')
    return s


def parse_directives(text):
    """[(KEY, [args])] in file order for the directive-style files
    (upsd.conf, upsmon.conf). Comments and blanks are dropped — this is the
    VIEW; rewrites go through merge_directives, which preserves them."""
    out = []
    for line in (text or '').splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        toks = nut_tokens(s)
        if toks:
            out.append((toks[0].upper(), toks[1:]))
    return out


def directive_map(text):
    """{KEY: [[args], ...]} — every occurrence, so repeating directives
    (MONITOR, LISTEN, NOTIFYFLAG) keep all of their values."""
    out = {}
    for key, args in parse_directives(text):
        out.setdefault(key, []).append(args)
    return out


def merge_directives(text, updates):
    """Rewrite a directive-style file, replacing ONLY the managed keys.

    `updates` maps an upper-case KEY to the complete list of rendered lines
    that key should have ([] deletes it entirely). The first occurrence of a
    key is replaced in place by its whole new block and later occurrences are
    dropped; comments, blank lines, ordering and every directive not named in
    `updates` survive verbatim. A key the file does not have yet is appended.
    """
    out, emitted = [], set()
    for line in (text or '').splitlines():
        s = line.strip()
        key = None
        if s and not s.startswith('#'):
            toks = nut_tokens(s)
            if toks and toks[0].upper() in updates:
                key = toks[0].upper()
        if key is None:
            out.append(line)
            continue
        if key in emitted:
            continue                      # duplicate of a key already rewritten
        emitted.add(key)
        out.extend(updates[key])
    for key, lines in updates.items():
        if key not in emitted and lines:
            out.extend(lines)
    return '\n'.join(out).rstrip('\n') + '\n'


RE_NUT_SECTION = re.compile(r'^\[([^\]\s]+)\]\s*\Z')


def parse_sections(text):
    """(head, [{'name', 'params'}]) for the section-style files (ups.conf,
    upsd.users).

    `head` is every line before the first [section] — ups.conf's global
    directives (pollinterval, maxretry, driverpath) and the packaged comment
    banner — kept verbatim on rewrite. Each param is (key, value, sep) where
    sep is '=' for `password = x` and ' ' for the bare-directive form
    upsd.users uses (`upsmon primary`)."""
    head, sections, cur = [], [], None
    for line in (text or '').splitlines():
        m = RE_NUT_SECTION.match(line.strip())
        if m:
            cur = {'name': m.group(1), 'params': []}
            sections.append(cur)
            continue
        if cur is None:
            head.append(line)
            continue
        s = line.strip()
        if not s or s.startswith('#'):
            cur['params'].append(('#', line, '#'))     # comment, kept in place
            continue
        if '=' in s:
            k, _sep, rest = s.partition('=')
            cur['params'].append((k.strip(), ' '.join(nut_tokens(rest)), '='))
        else:
            toks = nut_tokens(s)
            cur['params'].append((toks[0], ' '.join(toks[1:]), ' '))
    return head, sections


def section_params(sections, name):
    for s in sections:
        if s['name'] == name:
            return s['params']
    return None


def param_value(params, key, default=''):
    for k, v, sep in params or ():
        if sep != '#' and k.lower() == key.lower():
            return v
    return default


def has_param(params, key):
    return any(sep != '#' and k.lower() == key.lower()
               for k, v, sep in params or ())


def merge_params(existing, managed):
    """Merge managed (key, value, sep) triples into a section's parameters,
    preserving every parameter this module does not manage and the original
    order. A managed value of None removes the key. Same param-level merge
    the SMB registry backend does, and for the same reason: ups.conf sections
    carry vendorid/productid/product matching that the UI never shows."""
    out, seen = [], set()
    upd = {k.lower(): (k, v, sep) for k, v, sep in managed}
    for k, v, sep in existing or ():
        if sep == '#':
            out.append((k, v, sep))
            continue
        lk = k.lower()
        if lk not in upd:
            out.append((k, v, sep))
            continue
        if lk in seen:
            continue                       # drop duplicate managed keys
        seen.add(lk)
        nk, nv, nsep = upd[lk]
        if nv is not None:
            out.append((nk, nv, nsep))
    for k, v, sep in managed:
        if k.lower() not in seen and v is not None:
            out.append((k, v, sep))
    return out


def render_section(name, params):
    lines = ['[%s]' % name]
    for k, v, sep in params:
        if sep == '#':
            lines.append(v)
        elif sep == '=':
            lines.append('    %s = %s' % (k, nut_quote(v)))
        elif v == '':
            lines.append('    %s' % k)
        else:
            lines.append('    %s %s' % (k, nut_quote(v)))
    return lines


def merge_sections(text, name, params):
    """Replace, insert or delete ONE section, leaving the file's head, its
    other sections and their unmanaged parameters untouched. `params` of None
    deletes the section; a name the file does not have is appended."""
    head, sections = parse_sections(text)
    out = list(head)
    found = False
    for s in sections:
        if s['name'] == name:
            found = True
            if params is None:
                continue
            out.extend(render_section(name, params))
        else:
            out.extend(render_section(s['name'], s['params']))
    if not found and params is not None:
        if out and out[-1].strip():
            out.append('')
        out.extend(render_section(name, params))
    return '\n'.join(out).rstrip('\n') + '\n'


# ─── nut.conf (MODE) — shared by both halves ──────────────────────────
RE_NUT_MODE_LINE = re.compile(r'^\s*MODE\s*=', re.I)


def parse_mode(text):
    for line in (text or '').splitlines():
        s = line.strip()
        if s.startswith('#') or not RE_NUT_MODE_LINE.match(s):
            continue
        val = s.split('=', 1)[1].strip().strip('"').lower()
        if val:
            return val
    return ''


def render_mode(text, mode):
    """nut.conf is KEY=VALUE with no space, so it does not go through the
    directive merger. Other settings the packaged file carries
    (UPSD_OPTIONS, UPSMON_OPTIONS, POWEROFF_WAIT) are preserved."""
    out, done = [], False
    for line in (text or '').splitlines():
        s = line.strip()
        if not s.startswith('#') and RE_NUT_MODE_LINE.match(s):
            if done:
                continue
            out.append('MODE=%s' % mode)
            done = True
            continue
        out.append(line)
    if not done:
        out.append('MODE=%s' % mode)
    return '\n'.join(out).rstrip('\n') + '\n'


def set_mode(files, mode):
    """Apply a nut.conf MODE change. Shared: the server page offers
    standalone/netserver, the monitor page offers netclient, and a node that
    is both writes the same file."""
    if mode not in NUT_MODES:
        return err('MODE must be one of: %s' % ', '.join(NUT_MODES))
    return write_or_err('nut.conf', render_mode(files.get('nut.conf', ''), mode))


# ─── Live status (`upsc` — unprivileged, over the network) ────────────
# upsc speaks the NUT protocol to upsd on 3493 as any user, so every status
# read on both pages is privilege-free. This is why a client node can show
# full battery telemetry for a UPS it has no config access to.
_UPSC_TIMEOUT = 10

# ups.status flags, in the order a human wants to read them.
UPS_STATUS_LABELS = {
    'OL': 'online', 'OB': 'on battery', 'LB': 'low battery',
    'HB': 'high battery', 'RB': 'replace battery', 'CHRG': 'charging',
    'DISCHRG': 'discharging', 'BYPASS': 'bypass', 'CAL': 'calibrating',
    'OFF': 'off', 'OVER': 'overload', 'TRIM': 'trimming voltage',
    'BOOST': 'boosting voltage', 'FSD': 'forced shutdown',
    'ALARM': 'alarm', 'TEST': 'self-test',
}


def upsc_list(host=None):
    """UPS names known to an upsd ([] when it is unreachable)."""
    args = ['upsc', '-l'] + ([host] if host else [])
    out, _e, rc = run(args, no_sudo=True, timeout=_UPSC_TIMEOUT)
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def upsc_vars(target):
    """({var: value}, None) or (None, error). `target` is `ups` or
    `ups@host[:port]`. Lines without a colon (the "Init SSL …" notice NUT
    prints on TLS-capable clients) are skipped, not treated as data."""
    out, errout, rc = run(['upsc', target], no_sudo=True, timeout=_UPSC_TIMEOUT)
    if rc != 0:
        return None, ((errout or out).strip().splitlines() or ['unreachable'])[-1]
    data = {}
    for line in out.splitlines():
        k, sep, v = line.partition(':')
        if sep and k.strip():
            data[k.strip()] = v.strip()
    return (data, None) if data else (None, 'no data returned')


def _f(data, key):
    try:
        return float(data[key])
    except (KeyError, TypeError, ValueError):
        return None


def ups_snapshot(target):
    """The normalized handful of variables the UI, the dashboard card and the
    alerts use — vendor-independent, so no page has to know that a CyberPower
    reports `battery.runtime` in seconds while some others do not report it
    at all."""
    data, errmsg = upsc_vars(target)
    snap = {'target': target, 'reachable': data is not None, 'error': errmsg,
            'status': [], 'status_text': '', 'charge': None, 'runtime': None,
            'load': None, 'input_voltage': None, 'output_voltage': None,
            'battery_voltage': None, 'model': '', 'mfr': '', 'serial': '',
            'realpower_nominal': None}
    if data is None:
        return snap
    flags = (data.get('ups.status') or '').split()
    snap['status'] = flags
    snap['status_text'] = ', '.join(UPS_STATUS_LABELS.get(f, f) for f in flags)
    snap['charge'] = _f(data, 'battery.charge')
    snap['runtime'] = _f(data, 'battery.runtime')
    snap['load'] = _f(data, 'ups.load')
    snap['input_voltage'] = _f(data, 'input.voltage')
    snap['output_voltage'] = _f(data, 'output.voltage')
    snap['battery_voltage'] = _f(data, 'battery.voltage')
    snap['realpower_nominal'] = _f(data, 'ups.realpower.nominal')
    snap['model'] = data.get('ups.model') or data.get('device.model') or ''
    snap['mfr'] = data.get('ups.mfr') or data.get('device.mfr') or ''
    snap['serial'] = data.get('ups.serial') or data.get('device.serial') or ''
    return snap


def on_battery(snap):
    return 'OB' in (snap.get('status') or [])


def low_battery(snap):
    return 'LB' in (snap.get('status') or [])


# ─── Service state ────────────────────────────────────────────────────
def unit_state(unit):
    active = (run(['systemctl', 'is-active', unit])[0] or '').strip() or 'inactive'
    enabled = (run(['systemctl', 'is-enabled', unit])[0] or '').strip() or 'disabled'
    return {'unit': unit, 'active': active, 'enabled': enabled}


def _installed():
    """A NUT SERVER is present when upsd exists. The client half (upsc/upsmon)
    ships in a separate package and is checked by the upsmon module."""
    return bool(shutil.which('upsd'))


def available_drivers(configured=()):
    """Driver names that actually exist on this host, plus any driver the
    config already names (a driver from a package we do not know about must
    still round-trip through the edit form rather than vanish)."""
    found = []
    for name in KNOWN_DRIVERS:
        if name in found:
            continue
        if any(os.path.exists(os.path.join(d, name)) for d in _DRIVER_DIRS):
            found.append(name)
    for name in configured:
        if name and name not in found:
            found.append(name)
    return sorted(found)


# ─── Views ────────────────────────────────────────────────────────────
def _device_view(section, live=True):
    """One ups.conf section as the API shape. Extra parameters are surfaced
    (read/write) rather than hidden, so vendorid/productid/offdelay can be
    corrected without a shell — but they are preserved regardless."""
    params = section['params']
    extras = [{'key': k, 'value': v} for k, v, sep in params
              if sep != '#' and k.lower() not in _UPS_CORE_KEYS]
    view = {'name': section['name'],
            'driver': param_value(params, 'driver'),
            'port': param_value(params, 'port'),
            'desc': param_value(params, 'desc'),
            'extras': extras,
            'driver_service': 'nut-driver@%s' % section['name']}
    if live:
        view['live'] = ups_snapshot(section['name'])
    return view


def _listen_view(dmap):
    out = []
    for args in dmap.get('LISTEN', []):
        if args:
            out.append({'address': args[0],
                        'port': args[1] if len(args) > 1 else ''})
    return out


def _users_view(sections):
    """upsd.users, WITHOUT passwords. `upsmon primary|secondary` is a bare
    directive inside the section, not a key=value pair — hence the sep-aware
    parser."""
    out = []
    for s in sections:
        params = s['params']
        out.append({'name': s['name'],
                    'password_set': bool(param_value(params, 'password')),
                    'upsmon': param_value(params, 'upsmon'),
                    'actions': param_value(params, 'actions'),
                    'instcmds': param_value(params, 'instcmds')})
    return out


def nut_status(live=True):
    files = read_all()
    dmap = directive_map(files.get('upsd.conf', ''))
    _head, devices = parse_sections(files.get('ups.conf', ''))
    _uhead, users = parse_sections(files.get('upsd.users', ''))
    dev_views = [_device_view(s, live=live) for s in devices]
    return {
        'installed': _installed(),
        'conf_dir': conf_dir(),
        'editable': helper_present(),
        'config_readable': bool(files) or not os.path.isdir(conf_dir()),
        'mode': parse_mode(files.get('nut.conf', '')),
        'modes': list(NUT_MODES),
        'services': {'server': unit_state(NUT_SERVER_SERVICE),
                     'enumerator': unit_state(NUT_ENUMERATOR_SERVICE),
                     'monitor': unit_state(NUT_MONITOR_SERVICE)},
        'devices': dev_views,
        'listen': _listen_view(dmap),
        'maxage': (dmap.get('MAXAGE') or [['']])[0][0] if dmap.get('MAXAGE') else '',
        'maxconn': (dmap.get('MAXCONN') or [['']])[0][0] if dmap.get('MAXCONN') else '',
        'users': _users_view(users),
        'drivers': available_drivers([d['driver'] for d in dev_views]),
    }, files


# ─── Routes ───────────────────────────────────────────────────────────
@bp.route('/api/nut')
def nut_get():
    status, _files = nut_status()
    return jsonify(status)


@bp.route('/api/nut/ups/<name>')
def nut_ups_vars(name):
    """Every variable one UPS reports — the detail view behind a device row."""
    if not RE_UPS_NAME.match(name or ''):
        return err('Invalid UPS name')
    data, errmsg = upsc_vars(name)
    if data is None:
        return err(errmsg or 'UPS is not reachable', 502)
    return jsonify({'name': name, 'vars': data,
                    'snapshot': ups_snapshot(name)})


def _device_params(data, existing=None):
    """Validate a posted device and merge it onto its existing parameters.
    Returns (params, error)."""
    driver = (data.get('driver') or '').strip()
    port = (data.get('port') or '').strip()
    desc = (data.get('desc') or '').strip()
    if not RE_NUT_DRIVER.match(driver):
        return None, 'Driver must be a NUT driver name, e.g. usbhid-ups'
    if not RE_NUT_PORT.match(port):
        return None, ('Port must be "auto", a device node like /dev/ttyUSB0, '
                      'or host[:port] for a network driver')
    if not RE_NUT_DESC.match(desc):
        return None, 'Description may not contain quotes or line breaks'
    managed = [('driver', driver, '='), ('port', port, '='),
               ('desc', desc or None, '=')]
    seen = set()
    for entry in (data.get('extras') or []):
        k = (entry.get('key') or '').strip().lower()
        v = (entry.get('value') or '').strip()
        if not k:
            continue
        if not RE_NUT_PARAM_KEY.match(k):
            return None, 'Invalid parameter name: %s' % k
        if k in _UPS_CORE_KEYS:
            return None, '%s is edited in its own field, not as a parameter' % k
        if not RE_NUT_PARAM_VAL.match(v):
            return None, 'Invalid value for %s' % k
        seen.add(k)
        managed.append((k, v, '='))
    # A parameter the operator cleared from the form is removed; one the form
    # never showed (there are none today — extras carries them all) stays.
    for k, _v, sep in (existing or ()):
        if sep != '#' and k.lower() not in _UPS_CORE_KEYS and k.lower() not in seen:
            managed.append((k, None, '='))
    return merge_params(existing or [], managed), None


@bp.route('/api/nut/device', methods=['POST'])
def nut_device_add():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not RE_UPS_NAME.match(name):
        return err('UPS name must be letters, digits, dot, dash or underscore')
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    text = files.get('ups.conf', '')
    _head, sections = parse_sections(text)
    if section_params(sections, name) is not None:
        return err('A UPS named %s is already defined' % name)
    params, bad_input = _device_params(data)
    if bad_input:
        return err(bad_input)
    return write_or_err('ups.conf', merge_sections(text, name, params))


@bp.route('/api/nut/device/update', methods=['POST'])
def nut_device_update():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not RE_UPS_NAME.match(name):
        return err('Invalid UPS name')
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    text = files.get('ups.conf', '')
    _head, sections = parse_sections(text)
    existing = section_params(sections, name)
    if existing is None:
        return err('No UPS named %s — refresh and retry' % name, 404)
    params, bad_input = _device_params(data, existing)
    if bad_input:
        return err(bad_input)
    return write_or_err('ups.conf', merge_sections(text, name, params))


@bp.route('/api/nut/device/delete', methods=['POST'])
def nut_device_delete():
    name = ((request.get_json() or {}).get('name') or '').strip()
    if not RE_UPS_NAME.match(name):
        return err('Invalid UPS name')
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    text = files.get('ups.conf', '')
    _head, sections = parse_sections(text)
    if section_params(sections, name) is None:
        return err('No UPS named %s — refresh and retry' % name, 404)
    return write_or_err('ups.conf', merge_sections(text, name, None))


@bp.route('/api/nut/server', methods=['POST'])
def nut_server_settings():
    """upsd.conf: which addresses upsd listens on and how stale a reading may
    get. Adding a LAN address here is what lets other nodes' upsmon connect —
    upsd binds localhost only by default."""
    data = request.get_json() or {}
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    listen_lines = []
    for entry in (data.get('listen') or []):
        if isinstance(entry, str):
            entry = {'address': entry}
        addr = (entry.get('address') or '').strip()
        port = str(entry.get('port') or '').strip()
        if not addr:
            continue
        if not RE_NUT_LISTEN.match(addr):
            return err('Invalid listen address: %s' % addr)
        if port and (not RE_NUM.match(port) or not 1 <= int(port) <= 65535):
            return err('Listen port must be between 1 and 65535')
        listen_lines.append('LISTEN %s%s' % (nut_quote(addr),
                                             ' ' + port if port else ''))
    if not listen_lines:
        return err('At least one LISTEN address is required — removing them '
                   'all would leave upsd unreachable, including by this host')
    updates = {'LISTEN': listen_lines}
    # A key ABSENT from the payload is left alone; a key present but blank is
    # cleared so NUT's own default applies. Conflating the two would let a
    # partial POST silently delete settings it never mentioned.
    for key, lo, hi in (('MAXAGE', 1, 3600), ('MAXCONN', 1, 65535)):
        if key.lower() not in data:
            continue
        raw = str(data.get(key.lower()) or '').strip()
        if not raw:
            updates[key] = []
            continue
        if not RE_NUM.match(raw) or not lo <= int(raw) <= hi:
            return err('%s must be between %d and %d' % (key, lo, hi))
        updates[key] = ['%s %s' % (key, raw)]
    return write_or_err('upsd.conf',
                        merge_directives(files.get('upsd.conf', ''), updates))


@bp.route('/api/nut/mode', methods=['POST'])
def nut_mode_set():
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    return set_mode(files, ((request.get_json() or {}).get('mode') or '').strip().lower())


def _user_params(data, existing=None):
    """Validate a posted upsd.users entry. The password is WRITE-ONLY: an
    omitted/blank one keeps whatever is on disk, so saving a role change never
    silently blanks the credential upsmon is authenticating with."""
    role = (data.get('upsmon') or '').strip().lower()
    if role and role not in ('primary', 'secondary', 'master', 'slave'):
        return None, 'upsmon role must be primary or secondary'
    actions = (data.get('actions') or '').strip().upper()
    instcmds = (data.get('instcmds') or '').strip().upper()
    if not RE_NUT_ACTIONS.match(actions) or not RE_NUT_ACTIONS.match(instcmds):
        return None, 'actions/instcmds must be space-separated keywords'
    password = data.get('password')
    managed = [('upsmon', role or None, ' '),
               ('actions', actions or None, '='),
               ('instcmds', instcmds or None, '=')]
    if password:
        if not RE_NUT_PASSWORD.match(password):
            return None, ('Password must be 1-128 characters and may not '
                          'contain spaces, quotes, backslashes or #')
        managed.append(('password', password, '='))
    elif not has_param(existing, 'password'):
        return None, 'A password is required'
    return merge_params(existing or [], managed), None


@bp.route('/api/nut/user', methods=['POST'])
def nut_user_save():
    """Create or update an upsd user. One `upsmon primary` user for the server
    host and one `upsmon secondary` for the clients is the usual shape."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not RE_NUT_USER.match(name):
        return err('User name must be letters, digits, dot, dash or underscore')
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    text = files.get('upsd.users', '')
    _head, sections = parse_sections(text)
    existing = section_params(sections, name)
    params, bad_input = _user_params(data, existing)
    if bad_input:
        return err(bad_input)
    return write_or_err('upsd.users', merge_sections(text, name, params))


@bp.route('/api/nut/user/delete', methods=['POST'])
def nut_user_delete():
    name = ((request.get_json() or {}).get('name') or '').strip()
    if not RE_NUT_USER.match(name):
        return err('Invalid user name')
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    text = files.get('upsd.users', '')
    _head, sections = parse_sections(text)
    if section_params(sections, name) is None:
        return err('No user named %s — refresh and retry' % name, 404)
    return write_or_err('upsd.users', merge_sections(text, name, None))


# ─── Aggregator hooks ─────────────────────────────────────────────────
def summary():
    """CHEAP block for the 30s /api/summary fan-out: the device COUNT comes
    from the config (already read), and telemetry is left to the upsmon
    module's hook so a node running both halves does not poll upsd twice."""
    files = read_all()
    _head, devices = parse_sections(files.get('ups.conf', ''))
    return {'installed': _installed(),
            'active': unit_state(NUT_SERVER_SERVICE)['active'] == 'active',
            'devices': len(devices),
            'mode': parse_mode(files.get('nut.conf', ''))}


def alerts():
    """Server-side alerts only — the POWER alerts (on battery, low battery)
    belong to the upsmon module, which is the half that runs on every node.
    Alerting here is limited to "this host is configured to serve UPS data and
    is not doing it", so a NUT-less node stays silent."""
    files = read_all()
    _head, devices = parse_sections(files.get('ups.conf', ''))
    if not devices or not _installed():
        return []
    if parse_mode(files.get('nut.conf', '')) not in ('standalone', 'netserver'):
        return []
    if unit_state(NUT_SERVER_SERVICE)['active'] == 'active':
        return []
    return [{'key': 'nut-server-down',
             'message': 'UPS server (upsd) is not running but %d UPS device(s) '
                        'are configured' % len(devices)}]


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {
    'id': 'nut', 'order': 250,
    'label': 'UPS Server',
    'category': 'Power',
    'nav': {'cat': 'power', 'cat_order': 90, 'pages': [
        {'id': 'nut', 'label': 'UPS Server', 'icon': 'plug',
         'admin_only': True}]},
    'blueprint': bp,
    'summary': summary,
    'alerts': alerts,
    # The UPS data server — present on exactly one node per UPS by design, so
    # a stopped/absent unit is not an operational emergency on its own
    # (alert=False); this module raises its own alert when it IS configured.
    # The dict KEY must be the module id: the Services page hides a row whose
    # module is disabled by looking the key up in the disabled set (see
    # tests/test_modules.py::test_service_keys_are_module_ids). The systemd
    # unit is the `service` field, which is a different name here.
    'services': {'nut': {'name': 'NUT Server (upsd)',
                         'service': NUT_SERVER_SERVICE, 'pkg': 'nut-server',
                         'binary': '/usr/sbin/upsd', 'alert': False}},
}
