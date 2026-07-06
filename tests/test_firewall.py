"""Firewall module (ufw) — status/rule parsing, the dashboard-port lockout
guard, and the verify-before-delete flow. All ufw output is faked; no ufw or
root needed to run these."""
import pytest

import app


UFW_NUMBERED = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 8443/tcp                   ALLOW IN    Anywhere                   # Nexus Dashboard
[ 2] 22/tcp                     LIMIT IN    192.168.10.0/24            # SSH
[ 3] 445                        DENY IN     10.0.0.5
[ 4] 8443/tcp (v6)              ALLOW IN    Anywhere (v6)              # Nexus Dashboard
"""

UFW_VERBOSE_ACTIVE = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
8443/tcp                   ALLOW IN    Anywhere
"""


def _fake_run(responses):
    """run() stub keyed on the ufw subcommand; records every argv."""
    calls = []
    def fake(args, **kw):
        calls.append(list(args))
        for key, resp in responses.items():
            if args[:len(key)] == list(key):
                return resp
        return ('', '', 0)
    return fake, calls


# ─── Parsing ─────────────────────────────────────────────────────────────

def test_parse_numbered_rules():
    rules = app._parse_ufw_numbered(UFW_NUMBERED)
    assert [r['number'] for r in rules] == [1, 2, 3, 4]
    r1, r2, r3, r4 = rules
    assert r1 == {'number': 1, 'to': '8443/tcp', 'action': 'ALLOW',
                  'direction': 'IN', 'from': 'Anywhere',
                  'comment': 'Nexus Dashboard', 'v6': False}
    assert r2['action'] == 'LIMIT' and r2['from'] == '192.168.10.0/24'
    assert r3['to'] == '445' and r3['action'] == 'DENY' and r3['comment'] == ''
    assert r4['v6'] is True and r4['to'] == '8443/tcp' and r4['from'] == 'Anywhere'


def test_parse_numbered_ignores_noise():
    assert app._parse_ufw_numbered('Status: inactive\n') == []
    assert app._parse_ufw_numbered('') == []
    assert app._parse_ufw_numbered(None) == []


def test_status_active_with_defaults(monkeypatch):
    fake, calls = _fake_run({
        ('ufw', 'status', 'verbose'): (UFW_VERBOSE_ACTIVE, '', 0),
        ('ufw', 'status', 'numbered'): (UFW_NUMBERED, '', 0),
    })
    monkeypatch.setattr(app, 'run', fake)
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/sbin/ufw')
    st = app._ufw_status()
    assert st['available'] and st['status_known'] and st['active']
    assert st['defaults'] == {'incoming': 'deny', 'outgoing': 'allow'}
    assert len(st['rules']) == 4


def test_status_unknown_when_sudo_refused(monkeypatch):
    # No sudoers rule yet: sudo -n fails -> status_known False, never "inactive".
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/sbin/ufw')
    monkeypatch.setattr(app, 'run',
                        lambda *a, **k: ('', 'sudo: a password is required', 1))
    st = app._ufw_status()
    assert st['available'] is True
    assert st['status_known'] is False
    assert st['active'] is False


def test_status_not_available_without_ufw(monkeypatch):
    monkeypatch.setattr(app.shutil, 'which', lambda n: None)
    st = app._ufw_status()
    assert st == {'available': False, 'status_known': False, 'active': False,
                  'defaults': {}, 'rules': []}


# ─── Rule validation & lockout guard ─────────────────────────────────────

def test_validate_rule_accepts_normal_rules():
    assert app._validate_rule('allow', 445, 'tcp', '192.168.10.0/24', 'smb') is None
    assert app._validate_rule('deny', 23, 'any', '', '') is None
    assert app._validate_rule('limit', 22, 'tcp', '', 'SSH') is None


@pytest.mark.parametrize('action,port,proto,source,comment', [
    ('drop', 80, 'tcp', '', ''),            # unknown action
    ('allow', 0, 'tcp', '', ''),            # port out of range
    ('allow', 70000, 'tcp', '', ''),
    ('allow', 80, 'icmp', '', ''),          # bad proto
    ('allow', 80, 'tcp', 'not a subnet', ''),
    ('allow', 80, 'tcp', '', 'bad\ncomment'),
    ('allow', 80, 'tcp', '10.0.0.0/24; rm -rf', ''),
])
def test_validate_rule_rejects_bad_input(action, port, proto, source, comment):
    assert app._validate_rule(action, port, proto, source, comment) is not None


def test_blocks_dashboard_detection():
    port = app.DASHBOARD_PORT
    for action in ('deny', 'reject'):
        for proto in ('any', 'tcp'):
            assert app._blocks_dashboard(action, port, proto) is True
    # UDP on the same number is unrelated to the HTTPS UI.
    assert app._blocks_dashboard('deny', port, 'udp') is False
    assert app._blocks_dashboard('allow', port, 'tcp') is False
    assert app._blocks_dashboard('deny', port + 1, 'tcp') is False
    # Validation itself no longer refuses it — routes warn instead.
    assert app._validate_rule('deny', port, 'tcp', '', '') is None


# ─── Routes (through the facade test client) ─────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


def test_rule_add_builds_exact_argv(client, monkeypatch):
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/firewall/rule', json={
        'action': 'allow', 'port': 445, 'proto': 'tcp',
        'source': '192.168.10.0/24', 'comment': 'smb lab'})
    assert r.status_code == 200 and r.get_json()['success']
    assert calls == [['ufw', 'allow', 'proto', 'tcp',
                      'from', '192.168.10.0/24', 'to', 'any', 'port', '445',
                      'comment', 'smb lab']]


def test_rule_add_any_proto_omits_proto_args(client, monkeypatch):
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    client.post('/api/firewall/rule', json={'action': 'deny', 'port': 23})
    assert calls == [['ufw', 'deny', 'from', 'any', 'to', 'any', 'port', '23']]


def test_rule_add_blocking_dashboard_needs_confirm(client, monkeypatch):
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    body = {'action': 'deny', 'port': app.DASHBOARD_PORT, 'proto': 'tcp'}
    r = client.post('/api/firewall/rule', json=body)
    j = r.get_json()
    assert r.status_code == 200 and not j['success'] and j['needs_confirm']
    assert 'dashboard' in j['error']
    assert calls == []                      # nothing ran without confirmation
    # Re-submitted with confirm -> executes.
    r = client.post('/api/firewall/rule', json=dict(body, confirm=True))
    assert r.get_json()['success']
    assert calls == [['ufw', 'deny', 'proto', 'tcp',
                      'from', 'any', 'to', 'any',
                      'port', str(app.DASHBOARD_PORT)]]


def test_enable_allows_dashboard_port_first(client, monkeypatch):
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/sbin/ufw')
    r = client.post('/api/firewall/enable', json={'allow_ssh': True})
    assert r.get_json()['success']
    allow = ['ufw', 'allow', '%d/tcp' % app.DASHBOARD_PORT, 'comment', 'Nexus Dashboard']
    assert allow in calls and calls.index(allow) < calls.index(['ufw', '--force', 'enable'])
    assert ['ufw', 'allow', '22/tcp', 'comment', 'SSH'] in calls
    assert calls[-1] == ['ufw', '--force', 'enable']


def test_enable_can_skip_ssh(client, monkeypatch):
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/sbin/ufw')
    client.post('/api/firewall/enable', json={'allow_ssh': False})
    assert not any(c[:3] == ['ufw', 'allow', '22/tcp'] for c in calls)


def test_default_deny_re_allows_dashboard_port(client, monkeypatch):
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    client.post('/api/firewall/policy', json={'incoming': 'deny'})
    assert ['ufw', 'allow', '%d/tcp' % app.DASHBOARD_PORT,
            'comment', 'Nexus Dashboard'] in calls
    assert calls[-1] == ['ufw', 'default', 'deny', 'incoming']


def test_guard_respects_existing_scoped_allow(client, monkeypatch):
    """An operator's source-restricted allow for the dashboard port (e.g. an
    internet-facing node locked to one address) must NOT be widened to
    Anywhere by the auto-allow guard."""
    added = ('ufw allow from 203.0.113.99 to any port %d\n'
             'ufw allow 22/tcp\n' % app.DASHBOARD_PORT)
    fake, calls = _fake_run({('ufw', 'show', 'added'): (added, '', 0)})
    monkeypatch.setattr(app, 'run', fake)
    client.post('/api/firewall/policy', json={'incoming': 'deny'})
    assert not any(c[:2] == ['ufw', 'allow'] for c in calls)   # nothing widened
    assert calls[-1] == ['ufw', 'default', 'deny', 'incoming']


def test_dashboard_port_has_allow_matches(monkeypatch):
    port = app.DASHBOARD_PORT
    cases = [
        ('ufw allow from 1.2.3.4 to any port %d' % port, True),
        ('ufw allow %d/tcp' % port, True),
        ("ufw allow %d/tcp comment 'Nexus Dashboard'" % port, True),
        ('ufw allow 1%d/tcp' % port, False),                 # 18443 != 8443
        ('ufw deny %d/tcp' % port, False),
        ('ufw allow 22/tcp', False),
        ('', False),
    ]
    for text, expect in cases:
        fake, _ = _fake_run({('ufw', 'show', 'added'): (text, '', 0)})
        monkeypatch.setattr(app, 'run', fake)
        assert app._dashboard_port_has_allow() is expect, text
    # ufw itself failing (no sudoers rule) -> assume no allow, stay safe.
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', 'denied', 1))
    assert app._dashboard_port_has_allow() is False


def test_policy_rejects_bad_value(client, monkeypatch):
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/firewall/policy', json={'incoming': 'shields-up'})
    assert r.status_code == 400 and calls == []


def test_delete_verifies_rule_before_deleting(client, monkeypatch):
    st = {'available': True, 'status_known': True, 'active': True,
          'defaults': {'incoming': 'deny', 'outgoing': 'allow'},
          'rules': app._parse_ufw_numbered(UFW_NUMBERED)}
    monkeypatch.setattr(app, '_ufw_status', lambda: st)
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/firewall/rule/delete', json={
        'number': 3, 'expect': {'to': '445', 'action': 'DENY', 'from': '10.0.0.5'}})
    assert r.status_code == 200
    assert calls == [['ufw', '--force', 'delete', '3']]


def test_delete_409_when_rules_shifted(client, monkeypatch):
    st = {'available': True, 'status_known': True, 'active': True,
          'defaults': {}, 'rules': app._parse_ufw_numbered(UFW_NUMBERED)}
    monkeypatch.setattr(app, '_ufw_status', lambda: st)
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    # Client saw rule 2 as something else -> the table changed under it.
    r = client.post('/api/firewall/rule/delete', json={
        'number': 2, 'expect': {'to': '445', 'action': 'DENY', 'from': '10.0.0.5'}})
    assert r.status_code == 409 and calls == []
    # Unknown number -> 409 as well.
    r = client.post('/api/firewall/rule/delete', json={'number': 99, 'expect': {}})
    assert r.status_code == 409


def test_delete_last_dashboard_allow_needs_confirm(client, monkeypatch):
    st = {'available': True, 'status_known': True, 'active': True,
          'defaults': {'incoming': 'deny', 'outgoing': 'allow'},
          'rules': app._parse_ufw_numbered(UFW_NUMBERED)}
    monkeypatch.setattr(app, '_ufw_status', lambda: st)
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    body = {'number': 1,
            'expect': {'to': '8443/tcp', 'action': 'ALLOW', 'from': 'Anywhere'}}
    # The v6 twin (rule 4) does not keep v4 clients in -> this IS the last
    # v4 allow, so it warns instead of deleting.
    r = client.post('/api/firewall/rule/delete', json=body)
    j = r.get_json()
    assert r.status_code == 200 and not j['success'] and j['needs_confirm']
    assert 'dashboard' in j['error']
    assert calls == []
    # Confirmed -> deletes. Warn, don't forbid: the operator decides.
    r = client.post('/api/firewall/rule/delete', json=dict(body, confirm=True))
    assert r.get_json()['success']
    assert calls == [['ufw', '--force', 'delete', '1']]
    # Under default ALLOW incoming, removing it is harmless — no nag.
    calls.clear()
    st['defaults'] = {'incoming': 'allow', 'outgoing': 'allow'}
    r = client.post('/api/firewall/rule/delete', json=body)
    assert r.get_json()['success'] and calls == [['ufw', '--force', 'delete', '1']]


def test_delete_dashboard_allow_quiet_when_scoped_allow_remains(client, monkeypatch):
    """The scope-down flow: once a source-restricted allow for the dashboard
    port exists, deleting the broad Anywhere rule needs no confirmation."""
    numbered = UFW_NUMBERED + \
        '[ 5] 8443/tcp                   ALLOW IN    203.0.113.99               # lab only\n'
    st = {'available': True, 'status_known': True, 'active': True,
          'defaults': {'incoming': 'deny', 'outgoing': 'allow'},
          'rules': app._parse_ufw_numbered(numbered)}
    monkeypatch.setattr(app, '_ufw_status', lambda: st)
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/firewall/rule/delete', json={
        'number': 1, 'expect': {'to': '8443/tcp', 'action': 'ALLOW', 'from': 'Anywhere'}})
    assert r.get_json()['success']
    assert calls == [['ufw', '--force', 'delete', '1']]


def test_enable_can_skip_dashboard_allow(client, monkeypatch):
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/sbin/ufw')
    r = client.post('/api/firewall/enable',
                    json={'allow_ssh': False, 'allow_dashboard': False})
    assert r.get_json()['success']
    assert calls == [['ufw', '--force', 'enable']]


# ─── Rule edit (update = verified delete + re-add) ───────────────────────

def test_update_replaces_rule(client, monkeypatch):
    st = {'available': True, 'status_known': True, 'active': True,
          'defaults': {'incoming': 'deny', 'outgoing': 'allow'},
          'rules': app._parse_ufw_numbered(UFW_NUMBERED)}
    monkeypatch.setattr(app, '_ufw_status', lambda: st)
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/firewall/rule/update', json={
        'number': 3, 'expect': {'to': '445', 'action': 'DENY', 'from': '10.0.0.5'},
        'rule': {'action': 'deny', 'port': 445, 'proto': 'tcp',
                 'source': '10.0.0.0/24', 'comment': 'smb block'}})
    assert r.get_json()['success']
    assert calls == [
        ['ufw', '--force', 'delete', '3'],
        ['ufw', 'deny', 'proto', 'tcp', 'from', '10.0.0.0/24',
         'to', 'any', 'port', '445', 'comment', 'smb block'],
    ]


def test_update_verifies_expect_and_validates(client, monkeypatch):
    st = {'available': True, 'status_known': True, 'active': True,
          'defaults': {}, 'rules': app._parse_ufw_numbered(UFW_NUMBERED)}
    monkeypatch.setattr(app, '_ufw_status', lambda: st)
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    # Stale expect -> 409, nothing ran.
    r = client.post('/api/firewall/rule/update', json={
        'number': 2, 'expect': {'to': '445', 'action': 'DENY', 'from': '10.0.0.5'},
        'rule': {'action': 'allow', 'port': 22}})
    assert r.status_code == 409 and calls == []
    # Bad replacement rule -> 400 before any ufw call.
    r = client.post('/api/firewall/rule/update', json={
        'number': 3, 'expect': {}, 'rule': {'action': 'allow', 'port': 22,
                                            'source': 'not a subnet'}})
    assert r.status_code == 400 and calls == []


def test_update_scoping_dashboard_rule_needs_confirm(client, monkeypatch):
    """The internet-facing-node case: narrowing the last broad dashboard allow to an
    explicit source warns once (you could pick the wrong source), then goes
    through with confirm — it is never refused."""
    st = {'available': True, 'status_known': True, 'active': True,
          'defaults': {'incoming': 'deny', 'outgoing': 'allow'},
          'rules': app._parse_ufw_numbered(UFW_NUMBERED)}
    monkeypatch.setattr(app, '_ufw_status', lambda: st)
    fake, calls = _fake_run({})
    monkeypatch.setattr(app, 'run', fake)
    body = {'number': 1,
            'expect': {'to': '8443/tcp', 'action': 'ALLOW', 'from': 'Anywhere'},
            'rule': {'action': 'allow', 'port': app.DASHBOARD_PORT,
                     'proto': 'tcp', 'source': '203.0.113.99',
                     'comment': 'Nexus Dashboard lab only'}}
    r = client.post('/api/firewall/rule/update', json=body)
    j = r.get_json()
    assert r.status_code == 200 and not j['success'] and j['needs_confirm']
    assert calls == []
    r = client.post('/api/firewall/rule/update', json=dict(body, confirm=True))
    assert r.get_json()['success']
    assert calls == [
        ['ufw', '--force', 'delete', '1'],
        ['ufw', 'allow', 'proto', 'tcp', 'from', '203.0.113.99',
         'to', 'any', 'port', str(app.DASHBOARD_PORT),
         'comment', 'Nexus Dashboard lab only'],
    ]
    # An edit that keeps the unconditional allow (e.g. comment change) is quiet.
    calls.clear()
    body['rule'] = {'action': 'allow', 'port': app.DASHBOARD_PORT,
                    'proto': 'tcp', 'source': '', 'comment': 'renamed'}
    r = client.post('/api/firewall/rule/update', json=body)
    assert r.get_json()['success'] and calls[0] == ['ufw', '--force', 'delete', '1']


def test_update_rolls_back_when_readd_fails(client, monkeypatch):
    st = {'available': True, 'status_known': True, 'active': True,
          'defaults': {}, 'rules': app._parse_ufw_numbered(UFW_NUMBERED)}
    monkeypatch.setattr(app, '_ufw_status', lambda: st)
    # Delete succeeds; the new 'reject' add fails; the rollback re-add (a
    # 'deny', rebuilt from the parsed row) works.
    fake, calls = _fake_run({('ufw', 'reject'): ('', 'boom', 1)})
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/firewall/rule/update', json={
        'number': 3, 'expect': {'to': '445', 'action': 'DENY', 'from': '10.0.0.5'},
        'rule': {'action': 'reject', 'port': 446}})
    assert r.status_code == 400
    assert 're-added' in r.get_json()['error']
    assert calls[-1] == ['ufw', 'deny', 'from', '10.0.0.5',
                         'to', 'any', 'port', '445']


# ─── Alerts hook & registration ──────────────────────────────────────────

def test_alert_only_when_boot_enabled_but_inactive(monkeypatch):
    inactive = {'available': True, 'status_known': True, 'active': False,
                'defaults': {}, 'rules': []}
    monkeypatch.setattr(app, '_ufw_status', lambda: dict(inactive))
    # Supposed to be on (ENABLED=yes) but off -> alert.
    monkeypatch.setattr(app, '_ufw_boot_enabled', lambda: True)
    alerts = app._firewall_alerts()
    assert alerts and alerts[0]['key'] == 'firewall_inactive'
    # Never turned on (ENABLED=no) -> intentional, quiet. This is the fleet
    # norm today; alerting here would light up every node on rollout.
    monkeypatch.setattr(app, '_ufw_boot_enabled', lambda: False)
    assert app._firewall_alerts() == []


def test_ufw_boot_enabled_reads_conf(tmp_path, monkeypatch):
    conf = tmp_path / 'ufw.conf'
    conf.write_text('# comment\nENABLED=yes\nLOGLEVEL=low\n')
    monkeypatch.setattr(app, 'UFW_CONF', str(conf))
    assert app._ufw_boot_enabled() is True
    conf.write_text('ENABLED=no\n')
    assert app._ufw_boot_enabled() is False
    monkeypatch.setattr(app, 'UFW_CONF', str(tmp_path / 'missing.conf'))
    assert app._ufw_boot_enabled() is False


def test_no_alert_when_status_unknown_or_absent(monkeypatch):
    monkeypatch.setattr(app, '_ufw_boot_enabled', lambda: True)
    for st in ({'available': True, 'status_known': False, 'active': False},
               {'available': False, 'status_known': False, 'active': False},
               {'available': True, 'status_known': True, 'active': True}):
        monkeypatch.setattr(app, '_ufw_status',
                            lambda st=st: dict(st, defaults={}, rules=[]))
        assert app._firewall_alerts() == []


def test_firewall_module_registered():
    assert 'firewall' in app.MODULE_IDS
    desc = app._DESCRIPTORS['firewall']
    assert desc['category'] == 'System' and desc['alerts'] is app._firewall_alerts
