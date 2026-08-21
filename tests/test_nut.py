"""NUT modules — the config lexer, the merge-don't-regenerate rewrites, the
write-only password rule, and the helper-mediated apply flow.

Nothing here needs NUT, root, or a UPS: the config dir is a tmp_path pointed
at by DASHBOARD_NUT_CONF_DIR (which exercises the real direct-read path), and
`run` is faked so `upsc`, systemctl and the root helper never execute. The
fixtures are shaped exactly like the fleet's real files — a CyberPower on a
Rocky server publishing to Debian clients — with placeholder credentials.
"""
import pytest

import app
from nexusdash.modules import nut, upsmon


# Server side, as packaged + configured (RHEL layout). Note the comment banner,
# the global directive above the first section, and the vendorid/productid
# matching the UI never shows — all of which must survive an edit.
UPS_CONF = """# Network UPS Tools - UPS definitions
pollinterval = 2

[cyberpower]
    driver = usbhid-ups
    port = auto
    vendorid = 0764
    productid = 0501
    product = "LX1500GAVR"
    desc = "CyberPower LX1500GAVR - node1"
"""

UPSD_CONF = """MAXAGE 15
LISTEN 127.0.0.1 3493
LISTEN 192.168.10.6 3493
"""

UPSD_USERS = """[upsmon]
    password = serverpw
    upsmon primary

[upsmon-secondary]
    password = clientpw
    upsmon secondary
"""

# Client side (Debian layout), with the upssched wiring this module does NOT
# manage and must not drop.
UPSMON_CONF = """# Network UPS Tools - upsmon configuration
RUN_AS_USER nut
MONITOR cyberpower@192.168.10.6 1 upsmon-secondary clientpw secondary
MINSUPPLIES 1
SHUTDOWNCMD "/sbin/shutdown -h +0"
POWERDOWNFLAG /etc/killpower
NOTIFYCMD /usr/sbin/upssched
POLLFREQ 5
POLLFREQALERT 5
HOSTSYNC 15
DEADTIME 15
FINALDELAY 5
NOCOMMWARNTIME 300
RBWARNTIME 43200
NOTIFYFLAG ONLINE   SYSLOG+WALL
NOTIFYFLAG ONBATT   SYSLOG+WALL
NOTIFYFLAG LOWBATT  SYSLOG+WALL
NOTIFYFLAG COMMOK   SYSLOG
"""

NUT_CONF = """# Network UPS Tools - mode
MODE=netclient
UPSD_OPTIONS=""
"""

# Real `upsc` output shape, including the notice line with no colon that a
# TLS-capable client prints before the data.
UPSC_OUT = """Init SSL without certificate database
battery.charge: 100
battery.charge.low: 10
battery.runtime: 825
device.mfr: CPS
device.model: LX1500GAVR
input.voltage: 121.0
ups.load: 43
ups.mfr: CPS
ups.model: LX1500GAVR
ups.realpower.nominal: 900
ups.serial: QBSPZ7000047
ups.status: OL
"""


def _fake_run(upsc_out=UPSC_OUT, upsc_rc=0, helper_rc=0):
    """run() stub: records argv + stdin, answers upsc/systemctl, and stands in
    for the root helper. Returns (fn, calls)."""
    calls = []

    def fake(args, input_data=None, **kw):
        calls.append((list(args), input_data))
        if args[0] == 'upsc':
            if args[1:2] == ['-l']:
                return ('cyberpower\n', '', 0)
            return (upsc_out, '' if upsc_rc == 0 else 'Error: no such host', upsc_rc)
        if args[:2] == ['systemctl', 'is-active']:
            return ('active\n', '', 0)
        if args[:2] == ['systemctl', 'is-enabled']:
            return ('enabled\n', '', 0)
        return ('applied', '' if helper_rc == 0 else 'apply failed', helper_rc)
    return fake, calls


def _written(calls, name):
    """The candidate text handed to the helper for `name` (last write)."""
    for args, data in reversed(calls):
        if args[1:3] == ['write', name]:
            return data
    return None


@pytest.fixture
def nut_env(tmp_path, monkeypatch):
    """A full NUT config dir the dashboard user can read directly, plus a
    present-but-never-executed helper."""
    monkeypatch.setenv('DASHBOARD_NUT_CONF_DIR', str(tmp_path))
    for name, text in (('nut.conf', NUT_CONF), ('ups.conf', UPS_CONF),
                       ('upsd.conf', UPSD_CONF), ('upsd.users', UPSD_USERS),
                       ('upsmon.conf', UPSMON_CONF)):
        (tmp_path / name).write_text(text)
    helper = tmp_path / 'helper'
    helper.write_text('#!/bin/sh\n')
    monkeypatch.setattr(nut, 'NUT_HELPER', str(helper))
    fake, calls = _fake_run()
    monkeypatch.setattr(nut, 'run', fake)
    monkeypatch.setattr(nut.shutil, 'which', lambda n: '/usr/sbin/' + n)
    monkeypatch.setattr(upsmon.shutil, 'which', lambda n: '/usr/sbin/' + n)
    return {'dir': tmp_path, 'calls': calls}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── Lexer ───────────────────────────────────────────────────────────────

def test_tokens_handle_quotes_and_escapes():
    assert nut.nut_tokens('MONITOR ups@host 1 u p primary') == \
        ['MONITOR', 'ups@host', '1', 'u', 'p', 'primary']
    # The whole quoted command is ONE argument — splitting it would corrupt
    # SHUTDOWNCMD into a bare `/sbin/shutdown`.
    assert nut.nut_tokens('SHUTDOWNCMD "/sbin/shutdown -h +0"') == \
        ['SHUTDOWNCMD', '/sbin/shutdown -h +0']
    assert nut.nut_tokens('desc "a \\"quoted\\" name"') == ['desc', 'a "quoted" name']
    assert nut.nut_tokens('   ') == []
    assert nut.nut_tokens('KEY\t\tvalue') == ['KEY', 'value']


def test_quote_roundtrips_through_the_lexer():
    for value in ('auto', '/sbin/shutdown -h +0', 'a"b', 'has space', '', 'x#y'):
        line = 'K ' + nut.nut_quote(value)
        assert nut.nut_tokens(line) == ['K', value]


# ─── Directive files: parse + merge ──────────────────────────────────────

def test_directive_map_keeps_every_occurrence():
    dmap = nut.directive_map(UPSD_CONF)
    assert dmap['LISTEN'] == [['127.0.0.1', '3493'], ['192.168.10.6', '3493']]
    assert dmap['MAXAGE'] == [['15']]


def test_merge_directives_replaces_in_place_and_preserves_the_rest():
    out = nut.merge_directives(UPSMON_CONF, {'POLLFREQ': ['POLLFREQ 10']})
    lines = out.splitlines()
    # Comment, unmanaged directives and ORDER all survive.
    assert lines[0] == '# Network UPS Tools - upsmon configuration'
    assert 'NOTIFYCMD /usr/sbin/upssched' in lines
    assert 'RUN_AS_USER nut' in lines
    assert 'POLLFREQ 10' in lines and 'POLLFREQ 5' not in lines
    # Replaced in place, not appended: POLLFREQ still sits above POLLFREQALERT.
    assert lines.index('POLLFREQ 10') < lines.index('POLLFREQALERT 5')


def test_merge_directives_collapses_duplicates_and_appends_new_keys():
    out = nut.merge_directives(UPSD_CONF, {'LISTEN': ['LISTEN 0.0.0.0 3493'],
                                           'MAXCONN': ['MAXCONN 64']})
    lines = out.splitlines()
    assert [l for l in lines if l.startswith('LISTEN')] == ['LISTEN 0.0.0.0 3493']
    assert lines[-1] == 'MAXCONN 64'          # a key the file lacked is appended


def test_merge_directives_empty_list_deletes():
    out = nut.merge_directives(UPSD_CONF, {'MAXAGE': []})
    assert 'MAXAGE' not in out
    assert 'LISTEN 127.0.0.1 3493' in out


def test_merge_directives_leaves_commented_out_lines_alone():
    text = '# POLLFREQ 5\nPOLLFREQ 5\n'
    out = nut.merge_directives(text, {'POLLFREQ': ['POLLFREQ 9']})
    assert out == '# POLLFREQ 5\nPOLLFREQ 9\n'


# ─── Section files: parse + merge ────────────────────────────────────────

def test_parse_sections_splits_head_and_bodies():
    head, sections = nut.parse_sections(UPS_CONF)
    assert 'pollinterval = 2' in [l.strip() for l in head]
    assert [s['name'] for s in sections] == ['cyberpower']
    params = sections[0]['params']
    assert nut.param_value(params, 'driver') == 'usbhid-ups'
    assert nut.param_value(params, 'desc') == 'CyberPower LX1500GAVR - node1'
    assert nut.param_value(params, 'vendorid') == '0764'


def test_parse_sections_reads_the_bare_directive_form():
    """upsd.users mixes `password = x` with a bare `upsmon primary`; a parser
    that only understood `=` would silently drop every role."""
    _head, sections = nut.parse_sections(UPSD_USERS)
    by_name = {s['name']: s['params'] for s in sections}
    assert nut.param_value(by_name['upsmon'], 'upsmon') == 'primary'
    assert nut.param_value(by_name['upsmon-secondary'], 'upsmon') == 'secondary'
    assert ('upsmon', 'primary', ' ') in by_name['upsmon']


def test_merge_params_preserves_unmanaged_keys():
    _head, sections = nut.parse_sections(UPS_CONF)
    merged = nut.merge_params(sections[0]['params'],
                              [('driver', 'nutdrv_qx', '=')])
    assert nut.param_value(merged, 'driver') == 'nutdrv_qx'
    for kept in ('vendorid', 'productid', 'product', 'port', 'desc'):
        assert nut.has_param(merged, kept), kept


def test_merge_sections_roundtrip_is_reparseable():
    _head, sections = nut.parse_sections(UPS_CONF)
    out = nut.merge_sections(UPS_CONF, 'cyberpower', sections[0]['params'])
    head2, sections2 = nut.parse_sections(out)
    assert 'pollinterval = 2' in [l.strip() for l in head2]
    assert nut.param_value(sections2[0]['params'], 'desc') == \
        'CyberPower LX1500GAVR - node1'


def test_merge_sections_delete_and_append():
    gone = nut.merge_sections(UPS_CONF, 'cyberpower', None)
    assert '[cyberpower]' not in gone
    assert 'pollinterval = 2' in gone          # globals kept
    added = nut.merge_sections(gone, 'apc', [('driver', 'usbhid-ups', '='),
                                             ('port', 'auto', '=')])
    _h, secs = nut.parse_sections(added)
    assert [s['name'] for s in secs] == ['apc']


# ─── nut.conf MODE ───────────────────────────────────────────────────────

def test_mode_parse_and_render_preserve_other_settings():
    assert nut.parse_mode(NUT_CONF) == 'netclient'
    out = nut.render_mode(NUT_CONF, 'netserver')
    assert 'MODE=netserver' in out and 'MODE=netclient' not in out
    assert 'UPSD_OPTIONS=""' in out            # unmanaged setting survives
    assert out.splitlines()[0].startswith('#')
    # A file with no MODE at all gains one.
    assert 'MODE=standalone' in nut.render_mode('# empty\n', 'standalone')


def test_mode_parse_ignores_comments():
    assert nut.parse_mode('#MODE=netserver\nMODE=none\n') == 'none'
    assert nut.parse_mode('') == ''


# ─── upsc snapshots ──────────────────────────────────────────────────────

def test_ups_snapshot_normalizes_and_skips_the_ssl_notice(nut_env):
    snap = nut.ups_snapshot('cyberpower')
    assert snap['reachable'] and snap['status'] == ['OL']
    assert snap['status_text'] == 'online'
    assert snap['charge'] == 100.0 and snap['runtime'] == 825.0
    assert snap['load'] == 43.0 and snap['model'] == 'LX1500GAVR'
    # The colon-less "Init SSL ..." line is not data.
    assert 'Init SSL without certificate database' not in repr(snap)


def test_ups_snapshot_reports_unreachable(tmp_path, monkeypatch):
    fake, _calls = _fake_run(upsc_out='', upsc_rc=1)
    monkeypatch.setattr(nut, 'run', fake)
    snap = nut.ups_snapshot('gone@192.168.10.9')
    assert snap['reachable'] is False and snap['charge'] is None
    assert 'no such host' in snap['error']


@pytest.mark.parametrize('status,ob,lb', [
    ('OL', False, False), ('OB DISCHRG', True, False),
    ('OB LB', True, True), ('OL CHRG', False, False)])
def test_battery_flag_helpers(status, ob, lb):
    snap = {'status': status.split()}
    assert nut.on_battery(snap) is ob
    assert nut.low_battery(snap) is lb


# ─── UPS Server: read ────────────────────────────────────────────────────

def test_nut_get_shape(nut_env, client):
    d = client.get('/api/nut').get_json()
    assert d['editable'] and d['conf_dir'] == str(nut_env['dir'])
    assert d['mode'] == 'netclient'
    assert [x['name'] for x in d['devices']] == ['cyberpower']
    dev = d['devices'][0]
    assert dev['driver'] == 'usbhid-ups' and dev['port'] == 'auto'
    assert {'key': 'vendorid', 'value': '0764'} in dev['extras']
    assert dev['live']['charge'] == 100.0
    assert d['listen'] == [{'address': '127.0.0.1', 'port': '3493'},
                           {'address': '192.168.10.6', 'port': '3493'}]
    assert d['maxage'] == '15'


def test_nut_get_never_returns_passwords(nut_env, client):
    body = client.get('/api/nut').get_data(as_text=True)
    assert 'serverpw' not in body and 'clientpw' not in body
    users = client.get('/api/nut').get_json()['users']
    assert {u['name']: u['password_set'] for u in users} == \
        {'upsmon': True, 'upsmon-secondary': True}
    assert users[0]['upsmon'] == 'primary'
    assert 'password' not in users[0]


def test_nut_ups_vars(nut_env, client):
    d = client.get('/api/nut/ups/cyberpower').get_json()
    assert d['vars']['ups.model'] == 'LX1500GAVR'
    assert d['snapshot']['status'] == ['OL']
    assert client.get('/api/nut/ups/bad%20name').status_code == 400


# ─── UPS Server: writes ──────────────────────────────────────────────────

def test_device_update_preserves_unmanaged_parameters(nut_env, client):
    r = client.post('/api/nut/device/update', json={
        'name': 'cyberpower', 'driver': 'usbhid-ups', 'port': '/dev/ttyUSB0',
        'desc': 'moved to serial',
        'extras': [{'key': 'vendorid', 'value': '0764'},
                   {'key': 'productid', 'value': '0501'},
                   {'key': 'product', 'value': 'LX1500GAVR'}]})
    assert r.status_code == 200
    text = _written(nut_env['calls'], 'ups.conf')
    _head, secs = nut.parse_sections(text)
    p = secs[0]['params']
    assert nut.param_value(p, 'port') == '/dev/ttyUSB0'
    assert nut.param_value(p, 'desc') == 'moved to serial'
    assert nut.param_value(p, 'product') == 'LX1500GAVR'
    assert 'pollinterval = 2' in text          # global directive survives


def test_device_add_and_duplicate_refused(nut_env, client):
    r = client.post('/api/nut/device', json={
        'name': 'apc', 'driver': 'usbhid-ups', 'port': 'auto', 'desc': ''})
    assert r.status_code == 200
    _h, secs = nut.parse_sections(_written(nut_env['calls'], 'ups.conf'))
    assert [s['name'] for s in secs] == ['cyberpower', 'apc']
    dup = client.post('/api/nut/device', json={
        'name': 'cyberpower', 'driver': 'usbhid-ups', 'port': 'auto'})
    assert dup.status_code == 400
    assert 'already defined' in dup.get_json()['error']


@pytest.mark.parametrize('payload', [
    {'name': 'bad name', 'driver': 'usbhid-ups', 'port': 'auto'},
    {'name': 'ok', 'driver': 'rm -rf /', 'port': 'auto'},
    {'name': 'ok', 'driver': 'usbhid-ups', 'port': 'a b'},
    {'name': 'ok', 'driver': 'usbhid-ups', 'port': 'auto', 'desc': 'has "quote"'},
    {'name': 'ok', 'driver': 'usbhid-ups', 'port': 'auto',
     'extras': [{'key': 'driver', 'value': 'x'}]},          # core key via extras
    {'name': 'ok', 'driver': 'usbhid-ups', 'port': 'auto',
     'extras': [{'key': 'BAD KEY', 'value': 'x'}]},
])
def test_device_validation_rejects(nut_env, client, payload):
    assert client.post('/api/nut/device', json=payload).status_code == 400


def test_device_delete(nut_env, client):
    assert client.post('/api/nut/device/delete',
                       json={'name': 'cyberpower'}).status_code == 200
    assert '[cyberpower]' not in _written(nut_env['calls'], 'ups.conf')
    missing = client.post('/api/nut/device/delete', json={'name': 'nope'})
    assert missing.status_code == 404


def test_server_settings_write(nut_env, client):
    r = client.post('/api/nut/server', json={
        'listen': [{'address': '127.0.0.1', 'port': '3493'},
                   {'address': '192.168.10.6', 'port': ''}],
        'maxage': '20', 'maxconn': ''})
    assert r.status_code == 200
    text = _written(nut_env['calls'], 'upsd.conf')
    assert 'LISTEN 127.0.0.1 3493' in text
    assert 'LISTEN 192.168.10.6' in text
    assert 'MAXAGE 20' in text
    assert 'MAXCONN' not in text               # blank clears rather than guesses


def test_server_settings_refuse_empty_listen(nut_env, client):
    r = client.post('/api/nut/server', json={'listen': []})
    assert r.status_code == 400
    assert 'At least one LISTEN' in r.get_json()['error']


def test_server_settings_reject_bad_input(nut_env, client):
    assert client.post('/api/nut/server', json={
        'listen': [{'address': 'a b'}]}).status_code == 400
    assert client.post('/api/nut/server', json={
        'listen': [{'address': '127.0.0.1', 'port': '99999'}]}).status_code == 400
    assert client.post('/api/nut/server', json={
        'listen': [{'address': '127.0.0.1'}], 'maxage': 'soon'}).status_code == 400


def test_user_password_is_write_only(nut_env, client):
    """Saving a role change with no password must keep the stored one — the
    whole point of never sending it to the browser."""
    r = client.post('/api/nut/user', json={'name': 'upsmon-secondary',
                                           'upsmon': 'secondary',
                                           'actions': '', 'instcmds': ''})
    assert r.status_code == 200
    text = _written(nut_env['calls'], 'upsd.users')
    _h, secs = nut.parse_sections(text)
    by_name = {s['name']: s['params'] for s in secs}
    assert nut.param_value(by_name['upsmon-secondary'], 'password') == 'clientpw'
    assert nut.param_value(by_name['upsmon'], 'password') == 'serverpw'


def test_user_new_requires_a_password(nut_env, client):
    r = client.post('/api/nut/user', json={'name': 'fresh', 'upsmon': 'secondary'})
    assert r.status_code == 400
    assert 'password is required' in r.get_json()['error'].lower()


def test_user_password_change_and_charset(nut_env, client):
    assert client.post('/api/nut/user', json={
        'name': 'upsmon', 'password': 'NewPass123', 'upsmon': 'primary'}).status_code == 200
    text = _written(nut_env['calls'], 'upsd.users')
    assert 'password = NewPass123' in text
    for bad in ('has space', 'has"quote', 'back\\slash', 'hash#tag'):
        r = client.post('/api/nut/user', json={'name': 'upsmon', 'password': bad})
        assert r.status_code == 400, bad


def test_user_delete(nut_env, client):
    assert client.post('/api/nut/user/delete',
                       json={'name': 'upsmon-secondary'}).status_code == 200
    text = _written(nut_env['calls'], 'upsd.users')
    assert '[upsmon-secondary]' not in text and '[upsmon]' in text
    assert client.post('/api/nut/user/delete',
                       json={'name': 'ghost'}).status_code == 404


def test_mode_endpoint_validates(nut_env, client):
    assert client.post('/api/nut/mode', json={'mode': 'netserver'}).status_code == 200
    assert 'MODE=netserver' in _written(nut_env['calls'], 'nut.conf')
    assert client.post('/api/nut/mode', json={'mode': 'wide-open'}).status_code == 400


def test_mutations_refused_without_the_helper(nut_env, client, monkeypatch):
    monkeypatch.setattr(nut, 'NUT_HELPER', str(nut_env['dir'] / 'absent'))
    r = client.post('/api/nut/device', json={'name': 'x', 'driver': 'usbhid-ups',
                                             'port': 'auto'})
    assert r.status_code == 400
    assert 'helper is missing' in r.get_json()['error']
    # …but reading still works, so the page renders read-only.
    assert client.get('/api/nut').get_json()['editable'] is False


def test_helper_failure_surfaces(nut_env, client, monkeypatch):
    fake, calls = _fake_run(helper_rc=1)
    monkeypatch.setattr(nut, 'run', fake)
    r = client.post('/api/nut/mode', json={'mode': 'netserver'})
    assert r.status_code == 500
    assert 'apply failed' in r.get_json()['error']


def test_write_conf_refuses_unmanaged_names(nut_env):
    _out, errout, rc = nut.write_conf('/etc/passwd', 'root::0:0::/:/bin/sh\n')
    assert rc == 1 and 'unmanaged' in errout


# ─── UPS Monitor ─────────────────────────────────────────────────────────

def test_upsmon_get_shape(nut_env, client):
    d = client.get('/api/upsmon').get_json()
    assert d['editable'] and d['mode'] == 'netclient'
    assert len(d['monitors']) == 1
    m = d['monitors'][0]
    assert m['ups'] == 'cyberpower' and m['host'] == '192.168.10.6'
    assert m['user'] == 'upsmon-secondary' and m['type'] == 'secondary'
    assert m['password_set'] is True and 'password' not in m
    assert m['live']['charge'] == 100.0
    st = d['settings']
    assert st['shutdowncmd'] == '/sbin/shutdown -h +0'   # quoted value kept whole
    assert st['pollfreq'] == '5' and st['run_as_user'] == 'nut'
    assert d['notify']['ONBATT'] == ['SYSLOG', 'WALL']
    assert d['notify']['COMMOK'] == ['SYSLOG']


def test_upsmon_get_never_returns_passwords(nut_env, client):
    assert 'clientpw' not in client.get('/api/upsmon').get_data(as_text=True)


def test_monitor_update_keeps_the_password(nut_env, client):
    r = client.post('/api/upsmon/monitor/update', json={
        'original': 'cyberpower@192.168.10.6',
        'system': 'cyberpower@192.168.10.6', 'type': 'secondary',
        'powervalue': '1', 'user': 'upsmon-secondary'})
    assert r.status_code == 200
    text = _written(nut_env['calls'], 'upsmon.conf')
    mons = upsmon.parse_monitors(text)
    assert mons[0]['password'] == 'clientpw'
    # Everything this module does not manage is still there.
    assert 'NOTIFYCMD /usr/sbin/upssched' in text
    assert 'NOTIFYFLAG ONBATT   SYSLOG+WALL' in text


def test_monitor_add_and_delete(nut_env, client):
    r = client.post('/api/upsmon/monitor', json={
        'system': 'apc@node2.example.com', 'type': 'secondary',
        'powervalue': '1', 'user': 'upsmon-secondary', 'password': 'otherpw'})
    assert r.status_code == 200
    mons = upsmon.parse_monitors(_written(nut_env['calls'], 'upsmon.conf'))
    assert [m['system'] for m in mons] == ['cyberpower@192.168.10.6',
                                           'apc@node2.example.com']
    dup = client.post('/api/upsmon/monitor', json={
        'system': 'cyberpower@192.168.10.6', 'type': 'secondary',
        'powervalue': '1', 'user': 'u', 'password': 'p'})
    assert dup.status_code == 400


def test_monitor_delete_refuses_to_empty_the_list(nut_env, client):
    """upsmon will not start with no MONITOR line, so deleting the last one
    would look like a save and leave the service dead."""
    r = client.post('/api/upsmon/monitor/delete',
                    json={'system': 'cyberpower@192.168.10.6'})
    assert r.status_code == 400
    assert 'last MONITOR' in r.get_json()['error']
    assert client.post('/api/upsmon/monitor/delete',
                       json={'system': 'ghost@x'}).status_code == 404


@pytest.mark.parametrize('system', [
    'noatsign', 'ups@', '@host', 'ups@host with space', 'ups@host;reboot',
    'ups@host:99999x'])
def test_monitor_system_validation(nut_env, client, system):
    r = client.post('/api/upsmon/monitor', json={
        'system': system, 'type': 'secondary', 'powervalue': '1',
        'user': 'u', 'password': 'p'})
    assert r.status_code == 400, system


def test_settings_write_and_clear(nut_env, client):
    r = client.post('/api/upsmon/settings', json={
        'pollfreq': '10', 'deadtime': '20', 'shutdowncmd': '/usr/sbin/shutdown -h +0',
        'finaldelay': '', 'run_as_user': 'nut'})
    assert r.status_code == 200
    text = _written(nut_env['calls'], 'upsmon.conf')
    assert 'POLLFREQ 10' in text and 'DEADTIME 20' in text
    assert 'SHUTDOWNCMD "/usr/sbin/shutdown -h +0"' in text
    assert 'FINALDELAY' not in text            # blank removes it
    assert 'NOTIFYCMD /usr/sbin/upssched' in text
    # Round-trips: what we wrote parses back to what we meant.
    assert upsmon._scalars(text)['shutdowncmd'] == '/usr/sbin/shutdown -h +0'


@pytest.mark.parametrize('payload', [
    {'shutdowncmd': 'shutdown -h now'},                  # not absolute
    {'shutdowncmd': '/sbin/shutdown -h +0; curl evil'},  # shell chaining
    {'shutdowncmd': '/sbin/sh -c "$(x)"'},               # substitution
    {'notifycmd': '/usr/sbin/upssched && rm -rf /'},
    {'powerdownflag': 'killpower'},
    {'run_as_user': 'root; id'},
    {'pollfreq': '0'},
    {'pollfreq': 'soon'},
    {'deadtime': '99999'},
])
def test_settings_validation_rejects(nut_env, client, payload):
    assert client.post('/api/upsmon/settings', json=payload).status_code == 400


def test_notify_matrix_write(nut_env, client):
    r = client.post('/api/upsmon/notify', json={'notify': {
        'ONBATT': ['SYSLOG', 'WALL', 'EXEC'],
        'ONLINE': [],                       # nothing ticked -> IGNORE
    }})
    assert r.status_code == 200
    text = _written(nut_env['calls'], 'upsmon.conf')
    flags = upsmon.parse_notify(text)
    assert flags['ONBATT'] == ['SYSLOG', 'WALL', 'EXEC']
    assert flags['ONLINE'] == ['IGNORE']
    # An event the form did not send keeps the value it already had.
    assert flags['LOWBATT'] == ['SYSLOG', 'WALL']
    assert flags['COMMOK'] == ['SYSLOG']


def test_notify_rejects_unknown_flags(nut_env, client):
    r = client.post('/api/upsmon/notify',
                    json={'notify': {'ONBATT': ['SYSLOG', 'EMAIL']}})
    assert r.status_code == 400
    assert client.post('/api/upsmon/notify', json={'notify': 'all'}).status_code == 400


# ─── Aggregator hooks ────────────────────────────────────────────────────

def test_summary_blocks(nut_env):
    s = nut.summary()
    assert s['devices'] == 1 and s['mode'] == 'netclient' and s['active'] is True
    u = upsmon.summary()
    assert u['monitors'] == 1 and u['reachable'] == 1
    assert u['on_battery'] is False and u['charge'] == 100.0
    assert u['ups'] == 'cyberpower@192.168.10.6'


def test_summary_picks_the_ups_closest_to_trouble(nut_env, monkeypatch):
    snaps = {'a@h': {'target': 'a@h', 'reachable': True, 'status': ['OL'],
                     'status_text': 'online', 'charge': 100.0, 'runtime': 900,
                     'load': 10},
             'b@h': {'target': 'b@h', 'reachable': True, 'status': ['OB'],
                     'status_text': 'on battery', 'charge': 55.0, 'runtime': 300,
                     'load': 20}}
    monkeypatch.setattr(upsmon, '_monitor_snapshots',
                        lambda: [({'system': k, 'ups': k.split('@')[0]}, v)
                                 for k, v in snaps.items()])
    u = upsmon.summary()
    assert u['on_battery'] is True and u['ups'] == 'b@h' and u['charge'] == 55.0


def _snap(**kw):
    base = {'target': 'cyberpower@192.168.10.6', 'reachable': True,
            'status': ['OL'], 'status_text': 'online', 'charge': 100.0,
            'runtime': 900.0, 'load': 20.0, 'error': None}
    base.update(kw)
    return base


@pytest.mark.parametrize('snap,expect', [
    (_snap(), []),
    (_snap(status=['OB', 'DISCHRG'], runtime=600.0), ['upsmon-onbatt']),
    (_snap(status=['OB', 'LB']), ['upsmon-lowbatt']),
    (_snap(status=['OL', 'RB']), ['upsmon-replbatt']),
    (_snap(status=['OB', 'LB', 'RB']), ['upsmon-lowbatt', 'upsmon-replbatt']),
    (_snap(reachable=False, status=[], error='connect failed'), ['upsmon-nocomm']),
])
def test_upsmon_alerts(nut_env, monkeypatch, snap, expect):
    monkeypatch.setattr(upsmon, '_monitor_snapshots',
                        lambda: [({'system': snap['target'],
                                   'ups': 'cyberpower'}, snap)])
    keys = [a['key'].rsplit('-', 1)[0] for a in upsmon.alerts()]
    assert keys == expect


def test_onbatt_alert_names_the_remaining_runtime(nut_env, monkeypatch):
    snap = _snap(status=['OB'], runtime=600.0)
    monkeypatch.setattr(upsmon, '_monitor_snapshots',
                        lambda: [({'system': snap['target'], 'ups': 'c'}, snap)])
    assert '10 min runtime left' in upsmon.alerts()[0]['message']


def test_nut_server_alert_only_when_it_should_be_serving(nut_env, monkeypatch):
    # MODE=netclient in the fixture: a client node never raises the server alert
    # even though upsd is "not running" as far as this module can tell.
    monkeypatch.setattr(nut, 'run', _fake_run()[0])
    monkeypatch.setattr(nut, 'unit_state',
                        lambda u: {'unit': u, 'active': 'inactive',
                                   'enabled': 'disabled'})
    assert nut.alerts() == []
    (nut_env['dir'] / 'nut.conf').write_text('MODE=netserver\n')
    assert [a['key'] for a in nut.alerts()] == ['nut-server-down']
    # …and stays silent once it IS running.
    monkeypatch.setattr(nut, 'unit_state',
                        lambda u: {'unit': u, 'active': 'active',
                                   'enabled': 'enabled'})
    assert nut.alerts() == []


def test_history_labels_are_queryable(nut_env):
    """History labels are charset-validated on the way back out, so a label
    carrying the '@' of a MONITOR system could never be read again."""
    from nexusdash.core.history import RE_HISTORY_LABEL
    rows = upsmon.collect_history_samples()
    assert {m for m, _l, _v in rows} == {'ups_charge', 'ups_load', 'ups_runtime'}
    assert {m for m, _l, _v in rows} <= app._DESCRIPTORS['upsmon']['history_metrics']
    for _m, label, _v in rows:
        assert RE_HISTORY_LABEL.match(label), label


def test_history_hook_is_wired_and_toggle_aware(monkeypatch):
    from nexusdash.core import registry
    assert registry._DESCRIPTORS['upsmon']['history'] is upsmon.collect_history_samples
    assert 'upsmon' in [mid for mid, _ in registry.module_hooks('history')]
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'upsmon'})
    assert 'upsmon' not in [mid for mid, _ in registry.module_hooks('history')]


# ─── Degradation ─────────────────────────────────────────────────────────

def test_pages_degrade_without_nut(tmp_path, monkeypatch, client):
    """A node with no NUT at all answers cleanly instead of erroring — the
    Containers-without-LXD contract."""
    monkeypatch.setenv('DASHBOARD_NUT_CONF_DIR', str(tmp_path / 'absent'))
    monkeypatch.setattr(nut, 'NUT_HELPER', str(tmp_path / 'no-helper'))
    monkeypatch.setattr(nut, 'run', _fake_run()[0])
    monkeypatch.setattr(nut.shutil, 'which', lambda n: None)
    monkeypatch.setattr(upsmon.shutil, 'which', lambda n: None)
    d = client.get('/api/nut').get_json()
    assert d['installed'] is False and d['devices'] == [] and d['editable'] is False
    u = client.get('/api/upsmon').get_json()
    assert u['installed'] is False and u['monitors'] == []
    assert nut.alerts() == [] and upsmon.alerts() == []


def test_module_gate_403s_when_disabled(nut_env, client, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'nut'})
    assert client.get('/api/nut').status_code == 403
    assert client.get('/api/upsmon').status_code == 200      # separate toggle


# ─── Regressions ─────────────────────────────────────────────────────────

def test_serial_device_port_is_accepted(nut_env, client):
    """A leading '/' in the port pattern: /dev/ttyUSB0 is the commonest
    non-USB port and was rejected outright until the character class allowed
    it."""
    r = client.post('/api/nut/device', json={
        'name': 'serialups', 'driver': 'blazer_ser', 'port': '/dev/ttyUSB0'})
    assert r.status_code == 200
    _h, secs = nut.parse_sections(_written(nut_env['calls'], 'ups.conf'))
    assert nut.param_value(secs[-1]['params'], 'port') == '/dev/ttyUSB0'


def test_partial_post_never_deletes_settings_it_did_not_mention(nut_env, client):
    """An omitted key means "leave alone"; a key present-but-blank means
    "clear". Conflating them made a POLLFREQ-only save wipe NOTIFYCMD."""
    assert client.post('/api/upsmon/settings',
                       json={'pollfreq': '10'}).status_code == 200
    text = _written(nut_env['calls'], 'upsmon.conf')
    assert 'POLLFREQ 10' in text
    for kept in ('NOTIFYCMD /usr/sbin/upssched', 'POWERDOWNFLAG /etc/killpower',
                 'RUN_AS_USER nut', 'FINALDELAY 5', 'MINSUPPLIES 1',
                 'SHUTDOWNCMD "/sbin/shutdown -h +0"'):
        assert kept in text, kept


def test_server_partial_post_keeps_maxage(nut_env, client):
    assert client.post('/api/nut/server', json={
        'listen': [{'address': '127.0.0.1', 'port': '3493'}]}).status_code == 200
    assert 'MAXAGE 15' in _written(nut_env['calls'], 'upsd.conf')
