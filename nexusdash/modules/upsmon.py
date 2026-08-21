"""upsmon — the UPS monitoring client half of NUT.

`nut.py` manages the node the UPS cable is plugged into; THIS module manages
every node the UPS feeds, which on this fleet is nearly all of them. upsmon
connects to an upsd (local or across the LAN), watches `ups.status`, warns on
the way down and runs SHUTDOWNCMD when the battery is nearly gone. It is a
separate module — and a separate toggle — precisely because the two halves
have different footprints: one server, many clients.

It reads and writes exactly one file, `upsmon.conf`, plus the shared
`nut.conf` MODE (a client node wants MODE=netclient, and a node that is also
the server wants netserver — same file, so both pages can set it).

Design notes that are specific to this half:

  * MONITOR LINES CARRY A PLAINTEXT PASSWORD. The API returns `password_set`
    and never the value; an update that omits the password keeps the one on
    disk. Editing the poll interval must not silently blank the credential
    upsmon authenticates with.
  * SHUTDOWNCMD AND NOTIFYCMD ARE RUN BY ROOT, which is why this page is
    admin-only and why both are charset-validated rather than free text.
    (An admin on this dashboard already has blanket systemctl sudo, so this
    is not a new privilege boundary — but it should not be a casually wide
    one either.)
  * THE NOTIFY MATRIX IS THE POINT OF THE PAGE for most operators: which of
    the ten power events log, wall, or run NOTIFYCMD. It is surfaced as a
    grid rather than as raw NOTIFYFLAG lines.
  * upssched.conf is deliberately NOT managed. NOTIFYCMD is often
    `/usr/sbin/upssched`, whose own config is a timer/command DSL that
    deserves its own treatment; this module surfaces the NOTIFYCMD path and
    leaves upssched.conf alone rather than half-managing it.

Live status comes from `upsc`, which speaks the NUT protocol as any user —
so the battery telemetry, the dashboard card and the on-battery alert all
work on a node with no privileged access to any NUT file at all.
"""
import os
import re
import shutil

from flask import Blueprint, jsonify, request

from ..core.runcmd import err
from ..core.validators import RE_NUM
from .nut import (NUT_MODES, NUT_MONITOR_SERVICE, directive_map,
                  editable_or_err, helper_present, low_battery,
                  merge_directives, nut_quote, on_battery, parse_mode,
                  read_all, set_mode, ups_snapshot, unit_state, write_or_err)

bp = Blueprint('upsmon', __name__)

# ─── Validators (\Z-anchored, repo convention) ────────────────────────
# A MONITOR system: <upsname>@<hostname-or-ip>[:port]. The name half is a
# NUT section name; the host half may be a hostname, an IPv4 literal, or a
# bracketed IPv6 literal.
RE_MON_SYSTEM = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}'
    r'@(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9][A-Za-z0-9.-]{0,253})(:\d{1,5})?\Z')
RE_MON_USER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z')
RE_MON_PASSWORD = re.compile(r'^[A-Za-z0-9!$%()*+,./:;<=>?@^_{|}~\[\]-]{1,128}\Z')
# SHUTDOWNCMD/NOTIFYCMD are handed to the shell by upsmon, so the charset is
# deliberately narrow: an absolute path plus plain word/flag arguments. No
# metacharacters, no substitution, no chaining.
RE_UPSMON_CMD = re.compile(r'^/[A-Za-z0-9 /._:@+=-]{1,199}\Z')
RE_POWERDOWNFLAG = re.compile(r'^/[A-Za-z0-9/._-]{1,127}\Z')
RE_RUN_AS_USER = re.compile(r'^[a-z_][a-z0-9_-]{0,31}\Z')

# The power events upsmon can notify on, in the order NUT documents them.
NOTIFY_EVENTS = ('ONLINE', 'ONBATT', 'LOWBATT', 'FSD', 'COMMOK', 'COMMBAD',
                 'SHUTDOWN', 'REPLBATT', 'NOCOMM', 'NOPARENT', 'CAL',
                 'NOTCAL', 'OFF', 'NOTOFF', 'BYPASS', 'NOTBYPASS')
NOTIFY_EVENT_LABELS = {
    'ONLINE': 'UPS is back on line power',
    'ONBATT': 'UPS switched to battery',
    'LOWBATT': 'UPS battery is low',
    'FSD': 'Forced shutdown started by the primary',
    'COMMOK': 'Communication with the UPS restored',
    'COMMBAD': 'Communication with the UPS lost',
    'SHUTDOWN': 'This system is shutting down',
    'REPLBATT': 'UPS battery needs replacing',
    'NOCOMM': 'UPS unreachable since startup',
    'NOPARENT': 'upsmon parent died — shutdown impossible',
    'CAL': 'UPS is calibrating',
    'NOTCAL': 'UPS finished calibrating',
    'OFF': 'UPS output is off',
    'NOTOFF': 'UPS output is back on',
    'BYPASS': 'UPS is on bypass',
    'NOTBYPASS': 'UPS is off bypass',
}
NOTIFY_FLAGS = ('SYSLOG', 'WALL', 'EXEC', 'IGNORE')

# Scalar directives surfaced on the page: (KEY, low, high) for the numeric
# ones. Everything else in upsmon.conf is preserved untouched.
UPSMON_NUMBERS = (('MINSUPPLIES', 0, 64), ('POLLFREQ', 1, 3600),
                  ('POLLFREQALERT', 1, 3600), ('HOSTSYNC', 0, 3600),
                  ('DEADTIME', 1, 3600), ('RBWARNTIME', 1, 604800),
                  ('NOCOMMWARNTIME', 1, 604800), ('FINALDELAY', 0, 3600))
UPSMON_NUMBER_KEYS = tuple(k for k, _lo, _hi in UPSMON_NUMBERS)


def conf_text(files=None):
    return (files if files is not None else read_all()).get('upsmon.conf', '')


def _installed():
    """The CLIENT half. upsmon lives in /usr/sbin on RHEL but under /lib/nut
    on Debian (with a wrapper in /usr/sbin), so probe both; `upsc` alone is
    enough for the read-only status view."""
    return bool(shutil.which('upsmon') or shutil.which('upsc')
                or os.path.exists('/lib/nut/upsmon')
                or os.path.exists('/usr/lib/nut/upsmon'))


def parse_monitors(text):
    """MONITOR <system> <powervalue> <user> <password> <primary|secondary>.

    The password is parsed (it has to be, so a later save can preserve it)
    but is never placed in an API response — `password_set` is."""
    out = []
    for args in directive_map(text).get('MONITOR', []):
        if not args:
            continue
        system = args[0]
        name, _sep, host = system.partition('@')
        out.append({
            'system': system, 'ups': name, 'host': host or 'localhost',
            'powervalue': args[1] if len(args) > 1 else '1',
            'user': args[2] if len(args) > 2 else '',
            'password': args[3] if len(args) > 3 else '',
            'type': (args[4] if len(args) > 4 else 'secondary').lower(),
        })
    return out


def _monitor_line(m):
    return 'MONITOR %s %s %s %s %s' % (
        nut_quote(m['system']), nut_quote(m['powervalue']),
        nut_quote(m['user']), nut_quote(m['password']), m['type'])


def _monitor_view(m, live=True):
    """The API shape — deliberately WITHOUT `password`."""
    view = {'system': m['system'], 'ups': m['ups'], 'host': m['host'],
            'powervalue': m['powervalue'], 'user': m['user'],
            'type': m['type'], 'password_set': bool(m['password'])}
    if live:
        view['live'] = ups_snapshot(m['system'])
    return view


def parse_notify(text):
    """{EVENT: [flags]} from the NOTIFYFLAG lines. Events the file does not
    mention keep NUT's built-in default, which the UI shows as such rather
    than inventing a value."""
    out = {}
    for args in directive_map(text).get('NOTIFYFLAG', []):
        if len(args) >= 2:
            out[args[0].upper()] = [f for f in args[1].upper().split('+') if f]
    return out


def _scalars(text):
    dmap = directive_map(text)

    def first(key):
        vals = dmap.get(key) or []
        return ' '.join(vals[0]) if vals and vals[0] else ''

    view = {k.lower(): first(k) for k in UPSMON_NUMBER_KEYS}
    view['shutdowncmd'] = first('SHUTDOWNCMD')
    view['notifycmd'] = first('NOTIFYCMD')
    view['powerdownflag'] = first('POWERDOWNFLAG')
    view['run_as_user'] = first('RUN_AS_USER')
    return view


def upsmon_status(live=True):
    files = read_all()
    text = conf_text(files)
    monitors = parse_monitors(text)
    return {
        'installed': _installed(),
        'editable': helper_present(),
        'conf_readable': bool(files),
        'mode': parse_mode(files.get('nut.conf', '')),
        'modes': list(NUT_MODES),
        'service': unit_state(NUT_MONITOR_SERVICE),
        'monitors': [_monitor_view(m, live=live) for m in monitors],
        'settings': _scalars(text),
        'notify': parse_notify(text),
        'notify_events': [{'event': e, 'label': NOTIFY_EVENT_LABELS[e]}
                          for e in NOTIFY_EVENTS],
        'notify_flags': list(NOTIFY_FLAGS),
    }, files


# ─── Routes ───────────────────────────────────────────────────────────
@bp.route('/api/upsmon')
def upsmon_get():
    status, _files = upsmon_status()
    return jsonify(status)


def _validate_monitor(data, existing=None):
    """(monitor dict, error). `existing` supplies the password when the form
    did not send one — the write-only credential rule."""
    system = (data.get('system') or '').strip()
    if not RE_MON_SYSTEM.match(system):
        return None, ('Monitor a UPS as name@host, e.g. cyberpower@192.0.2.5 '
                      '(add :port for a non-default upsd port)')
    powervalue = str(data.get('powervalue') or '1').strip()
    if not RE_NUM.match(powervalue) or not 0 <= int(powervalue) <= 64:
        return None, 'Power value must be a number between 0 and 64'
    user = (data.get('user') or '').strip()
    if not RE_MON_USER.match(user):
        return None, 'User must match an entry in the server\'s upsd.users'
    mtype = (data.get('type') or 'secondary').strip().lower()
    if mtype not in ('primary', 'secondary', 'master', 'slave'):
        return None, 'Type must be primary or secondary'
    password = data.get('password')
    if password:
        if not RE_MON_PASSWORD.match(password):
            return None, ('Password must be 1-128 characters and may not '
                          'contain spaces, quotes, backslashes or #')
    else:
        password = (existing or {}).get('password') or ''
        if not password:
            return None, 'A password is required'
    return {'system': system, 'powervalue': powervalue, 'user': user,
            'password': password, 'type': mtype}, None


def _save_monitors(text, monitors):
    return write_or_err('upsmon.conf', merge_directives(
        text, {'MONITOR': [_monitor_line(m) for m in monitors]}))


@bp.route('/api/upsmon/monitor', methods=['POST'])
def upsmon_monitor_add():
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    text = conf_text(files)
    monitors = parse_monitors(text)
    mon, bad_input = _validate_monitor(request.get_json() or {})
    if bad_input:
        return err(bad_input)
    if any(m['system'] == mon['system'] for m in monitors):
        return err('%s is already monitored' % mon['system'])
    return _save_monitors(text, monitors + [mon])


@bp.route('/api/upsmon/monitor/update', methods=['POST'])
def upsmon_monitor_update():
    data = request.get_json() or {}
    system = (data.get('original') or data.get('system') or '').strip()
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    text = conf_text(files)
    monitors = parse_monitors(text)
    idx = next((i for i, m in enumerate(monitors) if m['system'] == system), None)
    if idx is None:
        return err('%s is not monitored — refresh and retry' % system, 404)
    mon, bad_input = _validate_monitor(data, monitors[idx])
    if bad_input:
        return err(bad_input)
    if any(m['system'] == mon['system'] for i, m in enumerate(monitors) if i != idx):
        return err('%s is already monitored' % mon['system'])
    monitors[idx] = mon
    return _save_monitors(text, monitors)


@bp.route('/api/upsmon/monitor/delete', methods=['POST'])
def upsmon_monitor_delete():
    system = ((request.get_json() or {}).get('system') or '').strip()
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    text = conf_text(files)
    monitors = parse_monitors(text)
    kept = [m for m in monitors if m['system'] != system]
    if len(kept) == len(monitors):
        return err('%s is not monitored — refresh and retry' % system, 404)
    if not kept:
        return err('Removing the last MONITOR line would leave upsmon with '
                   'nothing to watch and it would refuse to start — disable '
                   'the nut-monitor service instead')
    return _save_monitors(text, kept)


@bp.route('/api/upsmon/settings', methods=['POST'])
def upsmon_settings_save():
    """The timing and shutdown scalars. A blank field REMOVES the directive so
    NUT's own default applies, rather than writing a guess of what that
    default is."""
    data = request.get_json() or {}
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    updates = {}
    # A key ABSENT from the payload is left alone; a key present but blank is
    # removed so NUT's own default applies. Without that distinction a POST
    # that only meant to change POLLFREQ would delete NOTIFYCMD, POWERDOWNFLAG
    # and every timing directive it happened not to mention.
    for key, lo, hi in UPSMON_NUMBERS:
        if key.lower() not in data:
            continue
        raw = str(data.get(key.lower()) or '').strip()
        if not raw:
            updates[key] = []
            continue
        if not RE_NUM.match(raw) or not lo <= int(raw) <= hi:
            return err('%s must be a number between %d and %d' % (key, lo, hi))
        updates[key] = ['%s %s' % (key, raw)]
    for key, pattern, msg in (
            ('SHUTDOWNCMD', RE_UPSMON_CMD,
             'Shutdown command must be an absolute path with plain arguments, '
             'e.g. /usr/sbin/shutdown -h +0'),
            ('NOTIFYCMD', RE_UPSMON_CMD,
             'Notify command must be an absolute path with plain arguments'),
            ('POWERDOWNFLAG', RE_POWERDOWNFLAG,
             'Power-down flag must be an absolute file path'),
            ('RUN_AS_USER', RE_RUN_AS_USER,
             'Run-as user must be a system user name')):
        if key.lower() not in data:
            continue
        raw = (data.get(key.lower()) or '').strip()
        if not raw:
            updates[key] = []
            continue
        if not pattern.match(raw):
            return err(msg)
        updates[key] = ['%s %s' % (key, nut_quote(raw))]
    return write_or_err('upsmon.conf',
                        merge_directives(conf_text(files), updates))


@bp.route('/api/upsmon/notify', methods=['POST'])
def upsmon_notify_save():
    """The NOTIFYFLAG matrix. An event with NO flags selected is written as
    IGNORE (NUT's way of saying "do nothing"), not dropped — dropping it would
    silently restore the built-in default instead of the operator's choice."""
    data = request.get_json() or {}
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    posted = data.get('notify')
    if not isinstance(posted, dict):
        return err('notify must be an object of event -> flags')
    lines = []
    for event in NOTIFY_EVENTS:
        if event not in posted:
            continue
        flags = posted[event] or []
        if isinstance(flags, str):
            flags = [f for f in flags.upper().split('+') if f]
        flags = [str(f).upper() for f in flags]
        for f in flags:
            if f not in NOTIFY_FLAGS:
                return err('Unknown notify flag: %s' % f)
        if 'IGNORE' in flags or not flags:
            flags = ['IGNORE']
        lines.append('NOTIFYFLAG %-8s %s' % (event, '+'.join(flags)))
    if not lines:
        return err('No notification settings were supplied')
    # Events the operator did not send keep their existing line: rebuild the
    # block from the file's own values for those, in NUT's documented order.
    text = conf_text(files)
    current = parse_notify(text)
    keep = ['NOTIFYFLAG %-8s %s' % (e, '+'.join(current[e]))
            for e in NOTIFY_EVENTS
            if e in current and e not in posted]
    return write_or_err('upsmon.conf',
                        merge_directives(text, {'NOTIFYFLAG': lines + keep}))


@bp.route('/api/upsmon/mode', methods=['POST'])
def upsmon_mode_set():
    """nut.conf MODE — the same shared file the UPS Server page writes. A
    client node wants `netclient`."""
    files, bad = editable_or_err(read_all())
    if bad:
        return bad
    return set_mode(files, ((request.get_json() or {}).get('mode') or '').strip().lower())


@bp.route('/api/upsmon/status')
def upsmon_live():
    """Just the live UPS telemetry — what the dashboard card polls, without
    the privileged config read behind /api/upsmon."""
    monitors = parse_monitors(conf_text())
    return jsonify({'monitors': [{'system': m['system'], 'ups': m['ups'],
                                  'host': m['host'],
                                  'live': ups_snapshot(m['system'])}
                                 for m in monitors]})


# ─── Aggregator hooks ─────────────────────────────────────────────────
def _monitor_snapshots():
    """(monitor, snapshot) for every MONITOR line. One `upsc` call each — the
    fleet monitors one UPS per node, so this is one network round trip on the
    30s summary fan-out."""
    return [(m, ups_snapshot(m['system'])) for m in parse_monitors(conf_text())]


def summary():
    """Dashboard block: the worst state across every monitored UPS, plus the
    headline numbers for the card (charge and runtime of the UPS closest to
    trouble — the one that matters when there is more than one)."""
    snaps = _monitor_snapshots()
    block = {'installed': _installed(),
             'active': unit_state(NUT_MONITOR_SERVICE)['active'] == 'active',
             'monitors': len(snaps), 'reachable': 0, 'on_battery': False,
             'low_battery': False, 'charge': None, 'runtime': None,
             'status': '', 'ups': ''}
    worst = None
    for _m, snap in snaps:
        if not snap['reachable']:
            continue
        block['reachable'] += 1
        block['on_battery'] = block['on_battery'] or on_battery(snap)
        block['low_battery'] = block['low_battery'] or low_battery(snap)
        # "Closest to trouble": on-battery beats on-line, then lowest charge.
        rank = (0 if low_battery(snap) else 1 if on_battery(snap) else 2,
                snap['charge'] if snap['charge'] is not None else 999)
        if worst is None or rank < worst[0]:
            worst = (rank, snap)
    if worst:
        snap = worst[1]
        block['charge'] = snap['charge']
        block['runtime'] = snap['runtime']
        block['status'] = snap['status_text']
        block['ups'] = snap['target']
    return block


def alerts():
    """The power alerts for the whole fleet live here, on the half that runs
    everywhere. Silent on a node with no MONITOR line, so enabling the module
    on a UPS-less node costs nothing."""
    out = []
    for m, snap in _monitor_snapshots():
        label = m['system']
        if not snap['reachable']:
            # Only complain once upsmon is actually supposed to be running —
            # an unreachable UPS with the service stopped is the operator's
            # own doing, not news.
            if unit_state(NUT_MONITOR_SERVICE)['active'] == 'active':
                out.append({'key': 'upsmon-nocomm-%s' % label,
                            'message': 'Cannot reach UPS %s (%s)'
                                       % (label, snap.get('error') or 'no response')})
            continue
        if low_battery(snap):
            out.append({'key': 'upsmon-lowbatt-%s' % label,
                        'message': 'UPS %s battery is LOW — shutdown imminent'
                                   % label})
        elif on_battery(snap):
            runtime = snap.get('runtime')
            left = (', %d min runtime left' % (runtime // 60)) if runtime else ''
            out.append({'key': 'upsmon-onbatt-%s' % label,
                        'message': 'UPS %s is on battery%s' % (label, left)})
        if 'RB' in snap['status']:
            out.append({'key': 'upsmon-replbatt-%s' % label,
                        'message': 'UPS %s reports its battery needs replacing'
                                   % label})
    return out


def collect_history_samples():
    """[(metric, label, value)] — one series per monitored UPS.

    The label is the UPS NAME, not the full `name@host`: history labels are
    charset-validated on the way back out (`RE_HISTORY_LABEL` has no '@'), so
    recording the system string would write rows that could never be queried.
    """
    out = []
    for m, snap in _monitor_snapshots():
        if not snap['reachable']:
            continue
        label = m['ups']
        if snap['charge'] is not None:
            out.append(('ups_charge', label, snap['charge']))
        if snap['load'] is not None:
            out.append(('ups_load', label, snap['load']))
        if snap['runtime'] is not None:
            out.append(('ups_runtime', label, snap['runtime']))
    return out


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {
    'id': 'upsmon', 'order': 251,
    'label': 'UPS Monitor',
    'category': 'Power',
    'nav': {'cat': 'power', 'cat_order': 90, 'pages': [
        {'id': 'upsmon', 'label': 'UPS Monitor', 'icon': 'batt',
         'admin_only': True}]},
    'blueprint': bp,
    'summary': summary,
    'alerts': alerts,
    # upsmon SHOULD be running wherever it is configured, but the unit is
    # absent on a node without nut-client at all — alert=False keeps the
    # Services page quiet there; the module's own alerts cover the real cases.
    # Keyed by MODULE ID, not unit name — see the note on nut.MODULE.
    'services': {'upsmon': {'name': 'UPS Monitor (upsmon)',
                            'service': NUT_MONITOR_SERVICE,
                            'pkg': 'nut-client', 'binary': '/usr/sbin/upsmon',
                            'alert': False}},
    # Battery charge is worth a history graph: it is the one metric that tells
    # you whether an outage was ridden out or barely survived.
    'history_metrics': {'ups_charge', 'ups_load', 'ups_runtime'},
    'history': collect_history_samples,
}
