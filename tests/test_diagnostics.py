"""Diagnostics page (3.6.0): each enabled module's prerequisites on this node
with pass/fail and the fix. The probes (sudo -l, systemctl, /etc/group, the
process's groups, helpers on disk, cert) are faked; the CATALOG and the
state logic are what is pinned."""
import os
import pytest

import app
from nexusdash.core import diagnostics as dg


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    """Neutral host: no helpers, no timers, no groups, sudo lists systemctl only."""
    monkeypatch.setattr(dg, 'APP_DIR', str(tmp_path))
    monkeypatch.setattr(dg, 'HELPER_PREFIX', str(tmp_path / 'nexus-dashboard'))
    monkeypatch.setattr(dg, '_unit_state', lambda unit: 'inactive')
    monkeypatch.setattr(dg, '_sudo_grants', lambda: {'systemctl'})
    monkeypatch.setattr(dg, '_process_groups', lambda: set())
    monkeypatch.setattr(dg, '_etc_group_members', lambda user: set())
    monkeypatch.setattr(dg, '_group_exists', lambda g: True)
    monkeypatch.setattr(dg, '_cert_days_left', lambda *a: (200, {'present': True, 'self_signed': True}))
    monkeypatch.setattr(dg, '_service_user', lambda: 'dashboard')
    monkeypatch.setattr(dg, '_MODULE_PROBES', {})
    monkeypatch.setattr(dg, '_timer_wanted', lambda n: (True, ''))
    return tmp_path


def _by(res, cid, module=None):
    for c in res['checks']:
        if c['id'] == cid and (module is None or c['module'] == module):
            return c
    raise AssertionError('no check %s/%s' % (module, cid))


def test_helper_missing_is_a_fail_with_the_fix(quiet):
    res = dg.run_checks(enabled={'caddy'}, euid=1000)
    c = _by(res, 'helper:caddy', 'caddy')
    assert c['state'] == 'fail' and 'missing' in c['detail'] and '--helpers' in c['fix']
    (quiet / 'nexus-dashboard-caddy').write_text('#!/bin/sh\n')
    os.chmod(quiet / 'nexus-dashboard-caddy', 0o755)
    assert _by(dg.run_checks(enabled={'caddy'}, euid=1000), 'helper:caddy')['state'] == 'ok'


def test_disabled_modules_are_not_checked(quiet):
    res = dg.run_checks(enabled={'zfs'}, euid=1000)
    assert not [c for c in res['checks'] if c['module'] in ('caddy', 'docker', 'gpu')]
    assert _by(res, 'helper:netplan', 'network')['state'] == 'fail'        # core page: always checked


def test_group_states_member_restart_needed_absent(quiet, monkeypatch):
    def run(proc, etc):
        monkeypatch.setattr(dg, '_process_groups', lambda: proc)
        monkeypatch.setattr(dg, '_etc_group_members', lambda u: etc)
        return _by(dg.run_checks(enabled={'docker'}, euid=1000), 'group:docker', 'docker')
    assert run({'docker'}, {'docker'})['state'] == 'ok'
    c = run(set(), {'docker'})
    assert c['state'] == 'warn' and 'not effective' in c['detail'] and 'systemctl restart' in c['fix']
    c = run(set(), set())
    assert c['state'] == 'fail' and 'usermod -aG docker dashboard' in c['fix']
    monkeypatch.setattr(dg, '_group_exists', lambda g: False)
    assert run(set(), set())['state'] == 'warn'                              # group does not exist on the host


def test_group_alternatives_and_root_skip(quiet, monkeypatch):
    monkeypatch.setattr(dg, '_process_groups', lambda: {'incus-admin'})
    assert _by(dg.run_checks(enabled={'instances'}, euid=1000), 'group:lxd', 'instances')['state'] == 'ok'
    res = dg.run_checks(enabled={'instances', 'zfs'}, euid=0)
    assert _by(res, 'group:lxd', 'instances')['state'] == 'skip'
    assert _by(res, 'sudo', 'core')['state'] == 'skip'
    assert not [c for c in res['checks'] if c['id'].startswith('sudo:')]


def test_sudo_grants_per_module(quiet, monkeypatch):
    monkeypatch.setattr(dg, '_sudo_grants', lambda: {'systemctl', 'zpool', 'zfs', 'smartctl', 'testparm', 'smbpasswd', 'smbstatus', 'net'})
    res = dg.run_checks(enabled={'zfs', 'disks', 'smb'}, euid=1000)
    assert _by(res, 'sudo:smb', 'smb')['state'] == 'ok'
    assert _by(res, 'sudo:zfs', 'zfs')['state'] == 'ok'
    c = _by(res, 'sudo:disks', 'disks')
    assert c['state'] == 'fail' and 'lsblk' in c['detail'] and 'wipefs' in c['detail']
    monkeypatch.setattr(dg, '_sudo_grants', lambda: None)
    c = _by(dg.run_checks(enabled={'zfs'}, euid=1000), 'sudo', 'core')
    assert c['state'] == 'fail' and 'refused' in c['detail']


def test_timers_and_cert(quiet, monkeypatch):
    res = dg.run_checks(enabled={'schedules'}, euid=1000)
    assert _by(res, 'timer:history', 'core')['state'] == 'warn'
    assert 'enable --now' in _by(res, 'timer:autosnap', 'schedules')['fix']
    # An inactive timer with nothing scheduled for it is not a warning.
    monkeypatch.setattr(dg, '_timer_wanted', lambda n: (n == 'history', 'nothing scheduled'))
    res = dg.run_checks(enabled={'schedules'}, euid=1000)
    assert _by(res, 'timer:autosnap', 'schedules')['state'] == 'skip'
    assert _by(res, 'timer:alerts', 'core')['state'] == 'skip'
    assert _by(res, 'timer:history', 'core')['state'] == 'warn'
    monkeypatch.setattr(dg, '_timer_wanted', lambda n: (True, ''))
    monkeypatch.setattr(dg, '_unit_state', lambda u: 'active')
    assert _by(dg.run_checks(enabled=set(), euid=1000), 'timer:alerts')['state'] == 'ok'
    monkeypatch.setattr(dg, '_cert_days_left', lambda *a: (12, {'present': True, 'issuer': 'CN=x'}))
    c = _by(dg.run_checks(enabled=set(), euid=1000), 'cert')
    assert c['state'] == 'warn' and '12 days left' in c['detail']
    monkeypatch.setattr(dg, '_cert_days_left', lambda *a: (-3, {'present': True}))
    assert _by(dg.run_checks(enabled=set(), euid=1000), 'cert')['state'] == 'fail'


def test_counts_and_a_broken_probe_is_a_warning(quiet, monkeypatch):
    def boom():
        raise RuntimeError('socket gone')
    monkeypatch.setattr(dg, '_MODULE_PROBES', {'docker': boom})
    res = dg.run_checks(enabled={'docker'}, euid=1000)
    c = _by(res, 'probe', 'docker')
    assert c['state'] == 'warn' and 'socket gone' in c['detail']
    assert res['counts']['fail'] + res['counts']['warn'] + res['counts']['ok'] + res['counts']['skip'] == len(res['checks'])


def test_sudo_grant_parser():
    out = ('Matching Defaults entries for dashboard on node1:\n'
           '    env_reset\n\n'
           'User dashboard may run the following commands on node1:\n'
           '    (ALL) NOPASSWD: /usr/bin/systemctl, /usr/sbin/zpool, /usr/sbin/zfs\n'
           '    (ALL) NOPASSWD: /usr/local/sbin/nexus-dashboard-caddy *\n'
           '    (root) NOPASSWD: /usr/sbin/smartctl\n')
    calls = []
    orig = dg.run
    dg.run = lambda a, **k: calls.append(a) or (out, '', 0)
    try:
        assert dg._sudo_grants() == {'systemctl', 'zpool', 'zfs', 'nexus-dashboard-caddy', 'smartctl'}
        assert calls[0] == ['sudo', '-n', '-l']
        dg.run = lambda a, **k: ('', 'a password is required', 1)
        assert dg._sudo_grants() is None
    finally:
        dg.run = orig


def test_route_is_admin_only_and_nav_registered(monkeypatch):
    monkeypatch.setattr(dg, 'run_checks', lambda: {'checks': [], 'counts': {}})
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('v', 'readonly'))
    assert app.app.test_client().get('/api/diagnostics').status_code == 403
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('a', 'admin'))
    assert app.app.test_client().get('/api/diagnostics').status_code == 200
    assert 'function page_diagnostics(' in open('static/js/system.js').read()


def test_timer_wanted_reads_the_real_config_shapes(monkeypatch):
    from nexusdash.modules import schedules, replication, maintenance
    from nexusdash.core import alerts
    monkeypatch.setattr(schedules, 'load_schedules', lambda: {'schedules': []})
    monkeypatch.setattr(replication, 'load_replication', lambda: {'jobs': []})
    monkeypatch.setattr(maintenance, 'load_maintenance', lambda: {'scrubs': [], 'smart': []})
    monkeypatch.setattr(alerts, 'load_notifications', lambda: {})
    monkeypatch.setattr(alerts, '_notifications_enabled', lambda cfg: False)
    assert dg._timer_wanted('history') == (True, '')
    assert dg._timer_wanted('autosnap')[0] is False           # the loader's empty dict is NOT "configured"
    assert dg._timer_wanted('replicate')[0] is False
    assert dg._timer_wanted('maintenance')[0] is False
    assert dg._timer_wanted('alerts')[0] is False
    monkeypatch.setattr(schedules, 'load_schedules', lambda: {'schedules': [{'dataset': 'tank'}]})
    assert dg._timer_wanted('autosnap') == (True, 'no snapshot schedule configured')
    assert set(n for names in dg.TIMERS.values() for n in names) == {'history', 'alerts', 'autosnap', 'replicate', 'maintenance'}
