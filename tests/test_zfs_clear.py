"""ZFS pool recovery surface (3.4.1): the `zpool status` parser's status/action
text + per-member READ/WRITE/CKSUM columns, the `zpool clear` endpoint, and the
SUSPENDED alert wording. All zpool output is faked; no ZFS or root needed.

Regression origin (2026-08-28, node3): a raidz1 lost a second member when the
wrong drive was pulled during a replacement and the pool went SUSPENDED. The
dashboard showed a bare red badge, dropped the status/action lines that said
"run 'zpool clear'", hid the write errors on the still-"ONLINE" pulled disk,
and had no Clear action at all — Replace/Add simply relayed zpool's refusal.
"""
import os
import pytest

import app

FIX = os.path.join(os.path.dirname(__file__), 'fixtures')


def _fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def _fake_run(stdout='', rc=0):
    calls = []

    def fake(args, **kw):
        calls.append(list(args))
        return (stdout, '', rc)

    return fake, calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── parser ──────────────────────────────────────────────────────────────

def test_suspended_pool_parses_state_status_action_and_counters():
    """The real suspended capture: a leading pool-less `errors:` line (must be
    ignored), single-line status/action, the `see:` URL (not part of action),
    and a member ZFS still calls ONLINE with 3 read / 7 write errors."""
    pools = app.parse_zpool_status(_fixture('zpool_status_suspended.txt'))
    assert list(pools) == ['tank']
    t = pools['tank']
    assert t['state'] == 'SUSPENDED'
    assert t['status'] == 'One or more devices are faulted in response to IO failures.'
    assert t['action'] == "Make sure the affected devices are connected, then run 'zpool clear'."
    assert 'openzfs' not in t['action']
    assert t['errors'] == "1 data errors, use '-v' for a list"
    names = [d['name'] for d in t['devices']]
    assert names[:2] == ['tank', 'raidz1-0'] and len(names) == 7
    pulled = next(d for d in t['devices'] if d['name'].endswith('PAHYTE2T'))
    assert (pulled['state'], pulled['read'], pulled['write'], pulled['cksum']) == ('ONLINE', 3, 7, 0)
    dead = next(d for d in t['devices'] if d['name'].endswith('K7H574AL'))
    assert dead['state'] == 'REMOVED'
    assert next(d for d in t['devices'] if d['name'] == 'raidz1-0')['write'] == 4


def test_wrapped_status_and_action_lines_are_joined_and_multiple_pools_split():
    pools = app.parse_zpool_status(_fixture('zpool_status_degraded.txt'))
    assert list(pools) == ['tank', 'rpool']
    t = pools['tank']
    assert t['status'] == ('One or more devices have been removed. Sufficient replicas exist '
                           'for the pool to continue functioning in a degraded state.')
    assert t['action'] == ("Online the device using zpool online' or replace the device with "
                           "'zpool replace'.")
    # The continuation lines must NOT leak into config/devices.
    assert len(t['config']) == 7 and len(t['devices']) == 7
    assert all(not d['name'].startswith(('Sufficient', "'zpool")) for d in t['devices'])
    r = pools['rpool']
    assert r['state'] == 'ONLINE' and r['status'] == '' and r['action'] == ''
    assert [d['name'] for d in r['devices']] == ['rpool', 'mirror-0', 'sda3', 'sdb3']
    assert next(d for d in r['devices'] if d['name'] == 'sda3')['note'] == '(resilvering)'


def test_config_lines_are_still_returned_verbatim():
    """`config` is what older consumers render — unchanged shape, `devices` is additive."""
    pools = app.parse_zpool_status(_fixture('zpool_status_suspended.txt'))
    assert pools['tank']['config'][2].split()[:5] == \
        ['ata-Hitachi_HUS724040ALE641_PAHYTE2T', 'ONLINE', '3', '7', '0']


def test_config_line_without_counters_defaults_to_zero():
    d = app._parse_zpool_config_line('\t    sdz  UNAVAIL  cannot open')
    assert d == {'name': 'sdz', 'state': 'UNAVAIL', 'read': 0, 'write': 0, 'cksum': 0,
                 'note': 'cannot open'}


# ─── clear endpoint ───────────────────────────────────────────────────────

def test_clear_pool_argv(client, monkeypatch):
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/zfs/pools/tank/clear', json={})
    assert r.status_code == 200 and r.get_json()['success']
    assert calls == [['zpool', 'clear', 'tank']]


def test_clear_device_argv(client, monkeypatch):
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/zfs/pools/tank/clear',
                    json={'device': 'ata-Hitachi_HUS724040ALE641_PAHYTE2T'})
    assert r.status_code == 200 and r.get_json()['success']
    assert calls == [['zpool', 'clear', 'tank', 'ata-Hitachi_HUS724040ALE641_PAHYTE2T']]


def test_clear_without_body_is_pool_wide(client, monkeypatch):
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/zfs/pools/tank/clear')
    assert r.status_code == 200
    assert calls == [['zpool', 'clear', 'tank']]


@pytest.mark.parametrize('bad', ['sda; reboot', '../etc', 'a b', '-f'])
def test_clear_rejects_junk_device(client, monkeypatch, bad):
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/zfs/pools/tank/clear', json={'device': bad})
    assert r.status_code == 400 and calls == []


def test_clear_rejects_bad_pool_name(client, monkeypatch):
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/zfs/pools/-tank/clear', json={})
    assert r.status_code == 400 and calls == []


def test_clear_surfaces_zpool_failure(client, monkeypatch):
    """Clearing with the device still missing fails again — the stderr must
    reach the UI, not a generic 'ok'."""
    def fake(args, **kw):
        return ('', "cannot clear errors for tank: I/O error", 1)
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/zfs/pools/tank/clear', json={})
    body = r.get_json()
    assert r.status_code == 200 and body['success'] is False
    assert 'I/O error' in body['stderr']


def test_clear_is_gated_by_the_zfs_module_toggle(client, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'zfs'})
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/zfs/pools/tank/clear', json={})
    assert r.status_code == 403 and calls == []


# ─── alert wording ────────────────────────────────────────────────────────

def test_suspended_alert_says_io_is_blocked_and_how_to_clear():
    m = app._zpool_health_message('tank', 'SUSPENDED')
    assert m.startswith('ZFS pool tank is SUSPENDED')
    assert 'blocked' in m and 'clear' in m


def test_other_health_alert_wording_unchanged():
    assert app._zpool_health_message('tank', 'DEGRADED') == 'ZFS pool tank is DEGRADED'
