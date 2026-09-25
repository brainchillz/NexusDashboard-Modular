"""ZFS pool space is USABLE space (3.4.4), not zpool's raw vdev capacity.

Regression origin (2026-09-25): node3 built a 5x20T raidz1 and the dashboard
reported it as 90.9T — `zpool list` SIZE/ALLOC/FREE count parity. node2's
8x10.9T raidz1 showed 87.3T for a 73.4T volume, and its raidz2 flash pool
said 43% full while the root dataset (thick zvol reservation) was 57%
committed. Every consumer — /api/zfs/pools, the summary card, the
zfs_full alert, /metrics, history sampling and the forecast — read the same
raw call. Now `zfs.pool_space()` overlays the root dataset's used+avail from
`zfs list -Hp -d 0` and keeps zpool's columns under raw_*.

Fixtures are the two nodes' real `-Hp` output, byte for byte.
"""
import re
import pytest

import app
from nexusdash.modules import zfs
from nexusdash.core import summary as summ, metrics as met, history as hist

# node3, 2026-09-25: 5x20T raidz1, empty.
NODE3_ZPOOL_P = 'tank\t99986838650880\t933888\t99986837716992\tONLINE\n'
NODE3_ZFS_P = 'tank\t746016\t79734859144672\n'
NODE3_ZPOOL_H = 'tank\t90.9T\t912K\t90.9T\t0%\t0%\t1.00x\tONLINE\t-\n'
NODE3_USABLE = 746016 + 79734859144672            # 72.5T

# node2, 2026-09-25: ALLFLASH is raidz2 flash with a thick zvol, volume01 an
# 8-wide raidz1. ALLFLASH's USED exceeds its raw ALLOC — a refreservation.
NODE2_ZPOOL_P = ('ALLFLASH\t15994458210304\t6947580493824\t9046877716480\tONLINE\n'
                 'volume01\t95983929131008\t26217343238144\t69766585892864\tONLINE\n')
NODE2_ZFS_P = ('ALLFLASH\t7100765732878\t5351495850432\n'
               'volume01\t22069677574192\t58591855065040\n')

_RESOURCES = {'uptime_seconds': 1, 'load': {'1': 0.5, '5': 0.25, '15': 0.125},
              'cpus': 4, 'cpu_pct': 12.5,
              'memory': {'total': 8 * 1024**3, 'available': 6 * 1024**3,
                         'used': 2 * 1024**3, 'pct': 25.0},
              'swap': {'total': 1024**3, 'used': 0, 'pct': 0.0}}


def _runner(zpool_p, zfs_p, zpool_h='', zfs_rc=0):
    """run() keyed on argv: zpool list -Hp / zfs list -Hp / zpool list -Ho."""
    calls = []

    def fake(args, **kw):
        calls.append(list(args))
        if args[:2] == ['zpool', 'list'] and '-Hp' in args:
            return (zpool_p, '', 0)
        if args[:2] == ['zfs', 'list']:
            return (zfs_p, '', zfs_rc)
        if args[:2] == ['zpool', 'list'] and '-Ho' in args:
            return (zpool_h, '', 0)
        return ('', '', 1)
    fake.calls = calls
    return fake


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── pool_space() ──────────────────────────────────────────────────────

def test_pool_space_reports_usable_and_keeps_raw(monkeypatch):
    fake = _runner(NODE3_ZPOOL_P, NODE3_ZFS_P)
    monkeypatch.setattr(app, 'run', fake)
    sp = zfs.pool_space()
    assert sp['tank']['size'] == NODE3_USABLE
    assert sp['tank']['alloc'] == 746016
    assert sp['tank']['free'] == 79734859144672
    assert sp['tank']['raw_size'] == 99986838650880
    assert sp['tank']['raw_alloc'] == 933888
    assert sp['tank']['health'] == 'ONLINE'
    assert ['zfs', 'list', '-Hp', '-d', '0', '-o', 'name,used,avail'] in fake.calls


def test_pool_space_used_may_exceed_raw_alloc_for_reservations(monkeypatch):
    monkeypatch.setattr(app, 'run', _runner(NODE2_ZPOOL_P, NODE2_ZFS_P))
    sp = zfs.pool_space()
    a = sp['ALLFLASH']
    assert a['alloc'] > a['raw_alloc']                       # refreservation counts as used
    assert zfs._pct(a['raw_alloc'], a['raw_size']) == 43     # what zpool said
    assert zfs._pct(a['alloc'], a['size']) == 57             # what the operator has


def test_pool_space_falls_back_to_raw_when_zfs_list_fails(monkeypatch):
    monkeypatch.setattr(app, 'run', _runner(NODE3_ZPOOL_P, '', zfs_rc=1))
    sp = zfs.pool_space()
    assert sp['tank']['size'] == sp['tank']['raw_size'] == 99986838650880


def test_pool_space_is_none_when_zpool_fails(monkeypatch):
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    assert zfs.pool_space() is None


def test_pool_space_ignores_junk_rows(monkeypatch):
    monkeypatch.setattr(app, 'run', _runner('tank\t1T\t500G\t500G\tONLINE\n' + NODE3_ZPOOL_P.replace('tank', 'ok'),
                                            'ok\tx\ty\n' + NODE3_ZFS_P.replace('tank', 'ok')))
    sp = zfs.pool_space()
    assert list(sp) == ['ok']                                # non-numeric zpool row dropped
    assert sp['ok']['size'] == NODE3_USABLE                  # junk zfs row skipped, good one applied


# ─── /api/zfs/pools ───────────────────────────────────────────────────

def test_api_zfs_pools_shows_usable_and_carries_raw(client, monkeypatch):
    monkeypatch.setattr(app, 'run', _runner(NODE3_ZPOOL_P, NODE3_ZFS_P, NODE3_ZPOOL_H))
    p = client.get('/api/zfs/pools').get_json()[0]
    assert p['size'] == '72.5T'
    assert p['alloc'] == '728.5K'
    assert p['cap'] == '0%'
    assert p['size_bytes'] == NODE3_USABLE
    assert (p['raw_size'], p['raw_alloc'], p['raw_free'], p['raw_cap']) == ('90.9T', '912K', '90.9T', '0%')
    assert (p['frag'], p['dedup'], p['health'], p['altroot']) == ('0%', '1.00x', 'ONLINE', '-')


def test_api_zfs_pools_keeps_zpool_strings_without_zfs_list(client, monkeypatch):
    monkeypatch.setattr(app, 'run', _runner(NODE3_ZPOOL_P, '', NODE3_ZPOOL_H, zfs_rc=1))
    p = client.get('/api/zfs/pools').get_json()[0]
    assert p['size'] == '90.9T' and 'size_bytes' in p     # raw bytes still surfaced


# ─── summary card + alert ─────────────────────────────────────────────

def test_summary_zfs_block_is_usable(client, monkeypatch):
    monkeypatch.setattr(app, 'run', _runner(NODE2_ZPOOL_P, NODE2_ZFS_P))
    monkeypatch.setattr(app, '_system_resources', lambda: {})
    z = client.get('/api/summary').get_json()['zfs']
    assert z['pools'] == 2 and z['online'] is True
    assert z['size'] == '84.7T'          # 11.3T + 73.4T usable, not 101.8T raw
    assert z['used'] == '26.5T'
    assert z['pct'] == 31


def test_zfs_full_alert_uses_usable_percent(monkeypatch):
    # 92% committed by the dataset, 70% by zpool's raw column: must alert.
    zp = 'tank\t1000\t700\t300\tONLINE\n'
    zl = 'tank\t920\t80\n'
    monkeypatch.setattr(app, 'run', _runner(zp, zl))
    for name in ('_smart_health_ok', '_fs_alerts', '_md_alerts'):
        monkeypatch.setattr(app, name, lambda *a, **k: None if name == '_smart_health_ok' else [])
    monkeypatch.setattr(app, '_task_alerts', lambda *a, **k: [])
    keys = {a['key']: a['message'] for a in summ._compute_alerts()}
    assert keys.get('zfs_full:tank') == 'ZFS pool tank is 92% full'


def test_zfs_health_alert_still_fires(monkeypatch):
    monkeypatch.setattr(app, 'run', _runner(NODE3_ZPOOL_P.replace('ONLINE', 'DEGRADED'), NODE3_ZFS_P))
    for name in ('_fs_alerts', '_md_alerts'):
        monkeypatch.setattr(app, name, lambda *a, **k: [])
    monkeypatch.setattr(app, '_smart_health_ok', lambda *a, **k: None)
    monkeypatch.setattr(app, '_task_alerts', lambda *a, **k: [])
    keys = [a['key'] for a in summ._compute_alerts()]
    assert 'zfs_health:tank' in keys and 'zfs_full:tank' not in keys


# ─── /metrics + history ───────────────────────────────────────────────

def test_metrics_gauges_are_usable_with_a_raw_family(monkeypatch):
    monkeypatch.setattr(app, 'run', _runner(NODE3_ZPOOL_P, NODE3_ZFS_P))
    monkeypatch.setattr(app, '_system_resources', lambda: dict(_RESOURCES))
    monkeypatch.setattr(app, '_smart_health_ok', lambda *a, **k: None)
    fams = {f[0]: dict(f[3]) for f in met._metrics_families()}
    assert fams['storagedash_zfs_pool_size_bytes']['{pool="tank"}'] == NODE3_USABLE
    assert fams['storagedash_zfs_pool_alloc_bytes']['{pool="tank"}'] == 746016
    assert fams['storagedash_zfs_pool_raw_size_bytes']['{pool="tank"}'] == 99986838650880


def test_history_sample_records_usable(monkeypatch):
    monkeypatch.setattr(app, 'run', _runner(NODE3_ZPOOL_P, NODE3_ZFS_P))
    monkeypatch.setattr(app, '_system_resources', lambda: {})
    rows = {(m, l): v for m, l, v in hist._history_sample()}
    assert rows[('pool_size', 'tank')] == NODE3_USABLE
    assert rows[('pool_alloc', 'tank')] == 746016


def test_forecast_measures_headroom_against_usable(client, monkeypatch):
    monkeypatch.setattr(app, 'run', _runner('tank\t2000\t1000\t1000\tONLINE\n', 'tank\t800\t200\n'))
    monkeypatch.setattr(app, '_history_query_daily', lambda *a, **k: [])
    # 100 bytes/day of growth over the raw window.
    now = 1_700_000_000
    pts = [[now - 3 * 86400, 500], [now - 2 * 86400, 600], [now - 86400, 700], [now, 800]]
    monkeypatch.setattr(app, '_history_query', lambda *a, **k: pts)
    r = client.get('/api/history/forecast?label=tank').get_json()
    assert r['fill_rate_bytes_per_day'] == 100
    assert r['days_to_full'] == 2.0       # 200 usable bytes left, not 1000 raw


# ─── The frontend half: UPS charge is a LEVEL, not consumption ────────

def test_battery_bars_use_level_polarity():
    core = open('static/js/core.js').read()
    nut = open('static/js/nut.js').read()
    assert 'function levelBar(' in core
    m = re.search(r"function levelBar\(pct\) \{.*?\n\}", core, re.S).group(0)
    assert "pct < 20 ? 'red'" in m and "pct < 50 ? 'yellow'" in m and "'green'" in m
    assert 'usageBar(live.charge)' not in nut and 'usageBar(u.charge)' not in nut
    assert nut.count('levelBar(') == 2                    # server-page row + dashboard card
