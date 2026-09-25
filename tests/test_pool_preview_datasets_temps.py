"""3.6.0 small features: pool-layout preview before create, dataset space
properties (quota/reservation) on the ZFS page, and host temperatures from
hwmon with an alert + history rows. All sysfs/zfs/lsblk output is faked."""
import os
import pytest

import app
from nexusdash.modules import zfs
from nexusdash.core import summary as summ, history as hist

T = 20_000_000_000_000        # a "20T" disk, in bytes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── estimate_pool ────────────────────────────────────────────────────

def test_estimate_raidz1_matches_the_real_pool():
    # node3: 5x20T raidz1 listed 90.9T raw / 72.5T usable.
    est = zfs.estimate_pool('raidz', [19997367730176] * 5)
    assert est['tolerance'] == 1 and est['disks'] == 5 and est['wasted'] == 0
    assert abs(est['usable'] - 79734859890688) / 79734859890688 < 0.002      # within 0.2% of zfs's own figure


@pytest.mark.parametrize('layout,n,usable_disks,tol', [
    ('', 3, 3, 0), ('mirror', 2, 1, 1), ('mirror', 3, 1, 2),
    ('raidz', 3, 2, 1), ('raidz2', 6, 4, 2), ('raidz3', 8, 5, 3)])
def test_estimate_layouts(layout, n, usable_disks, tol):
    est = zfs.estimate_pool(layout, [T] * n)
    assert est['tolerance'] == tol
    expect = usable_disks * T * (1 if layout in ('', 'mirror') else zfs.RAIDZ_OVERHEAD)
    assert abs(est['usable'] - expect) <= 1


def test_estimate_mixed_sizes_counts_the_smallest():
    est = zfs.estimate_pool('raidz', [T, T, T // 2])
    assert est['usable'] == int(2 * (T // 2) * zfs.RAIDZ_OVERHEAD)
    assert est['wasted'] == 2 * (T - T // 2)


def test_estimate_refuses_impossible_layouts():
    assert 'error' in zfs.estimate_pool('raidz2', [T, T, T])
    assert 'error' in zfs.estimate_pool('mirror', [T])
    assert 'error' in zfs.estimate_pool('raidz', [])
    assert 'error' in zfs.estimate_pool('draid', [T, T])


def test_preview_route_runs_lsblk_on_validated_devices(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'run', lambda a, **k: calls.append(list(a)) or ('sda %d\nsdb %d\nsdc %d\n' % (T, T, T), '', 0))
    r = client.get('/api/zfs/pools/preview?type=raidz&disks=/dev/sda,sdb,sdc').get_json()
    assert calls[0] == ['lsblk', '-bdno', 'NAME,SIZE', '/dev/sda', '/dev/sdb', '/dev/sdc']
    assert r['tolerance'] == 1 and r['usable_h'].endswith('T') and r['raw_h'].endswith('T')
    assert client.get('/api/zfs/pools/preview?type=raidz&disks=sda;rm').status_code == 400
    assert 'error' in client.get('/api/zfs/pools/preview?type=raidz').get_json()


# ─── datasets ─────────────────────────────────────────────────────────

ZFS_LIST = ('tank\tfilesystem\t746016\t79734859144672\t157056\t-\t-\t-\t-\t1.00\t/tank\t-\t0\n'
            'tank/vm\tvolume\t1099511627776\t80834370772448\t12288\t-\t-\t-\t1099511627776\t1.85\t-\t1099511627776\t0\n'
            'tank/media\tfilesystem\t5000000\t2000000000000\t4900000\t2000000000000\t-\t-\t-\t1.02\t/tank/media\t-\t100000\n')


def test_dataset_rows_parse_reservations_and_quotas():
    rows = {r['name']: r for r in zfs._parse_dataset_rows(ZFS_LIST)}
    assert rows['tank']['is_pool'] and rows['tank']['quota'] is None
    vm = rows['tank/vm']
    assert vm['type'] == 'volume' and vm['refreservation'] == 1099511627776 == vm['volsize']
    assert vm['compressratio'] == 1.85
    m = rows['tank/media']
    assert m['quota'] == 2000000000000 and m['usedbysnapshots'] == 100000 and not m['is_pool']


def test_datasets_detail_route(client, monkeypatch):
    monkeypatch.setattr(app, 'run', lambda a, **k: (ZFS_LIST, '', 0) if a[:2] == ['zfs', 'list'] else ('', '', 1))
    r = client.get('/api/zfs/datasets/detail').get_json()
    assert [d['name'] for d in r['datasets']] == ['tank', 'tank/vm', 'tank/media']


def test_dataset_property_set_uses_the_existing_put_route(client, monkeypatch):
    # The modal drives the pre-existing PUT /api/zfs/datasets/<name>/properties.
    calls = []
    monkeypatch.setattr(app, 'run_safe', lambda a, **k: calls.append(list(a)) or {'success': True})
    assert client.put('/api/zfs/datasets/tank/media/properties', json={'property': 'quota', 'value': '2T'}).status_code == 200
    assert calls == [['zfs', 'set', 'quota=2T', 'tank/media']]
    client.put('/api/zfs/datasets/tank/media/properties', json={'property': 'refreservation', 'value': 'none'})
    assert calls[-1] == ['zfs', 'set', 'refreservation=none', 'tank/media']
    assert client.put('/api/zfs/datasets/tank/media/properties', json={'property': 'quo ta', 'value': '1G'}).status_code == 400
    assert 'datasets/${name' in open('static/js/storage.js').read()


# ─── temperatures ─────────────────────────────────────────────────────

def _hwmon(tmp_path):
    for i, (name, temps) in enumerate([('k10temp', {'temp1': ('Tctl', 61500), 'temp3': ('Tccd1', 58250)}),
                                       ('nvme', {'temp1': ('Composite', 43000), 'temp2': ('Sensor 1', 47000)}),
                                       ('amdgpu', {'temp1': ('edge', 50000)}),
                                       ('broken', {'temp1': (None, 'garbage')})]):
        d = tmp_path / ('hwmon%d' % i)
        d.mkdir()
        (d / 'name').write_text(name + '\n')
        for k, (label, val) in temps.items():
            (d / (k + '_input')).write_text(str(val) + '\n')
            if label:
                (d / (k + '_label')).write_text(label + '\n')
    return str(tmp_path)


def test_host_temps_reads_hwmon(tmp_path):
    temps = summ._host_temps(_hwmon(tmp_path))
    assert {(t['chip'], t['label'], t['c']) for t in temps} == {
        ('k10temp', 'Tctl', 61.5), ('k10temp', 'Tccd1', 58.2), ('nvme', 'Composite', 43.0),
        ('nvme', 'Sensor 1', 47.0), ('amdgpu', 'edge', 50.0)}          # 'broken' skipped
    s = summ._temps_summary(temps)
    assert s['cpu'] == 61.5 and s['nvme'] == 47.0 and len(s['sensors']) == 5
    assert summ._host_temps(str(tmp_path / 'nope')) == []
    assert summ._temps_summary([]) == {'cpu': None, 'nvme': None, 'sensors': []}


def test_temperature_alert_and_history(monkeypatch):
    monkeypatch.setattr(app, '_host_temps', lambda *a, **k: [{'chip': 'k10temp', 'label': 'Tctl', 'c': 93.0},
                                                             {'chip': 'nvme', 'label': 'Composite', 'c': 55.0}])
    keys = {a['key']: a['message'] for a in summ._temp_alerts()}
    assert keys == {'temp_high:cpu': 'CPU temperature is 93°C (limit 90)'}
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    monkeypatch.setattr(app, '_system_resources', lambda: {})
    rows = {(m, l): v for m, l, v in hist._history_sample()}
    assert rows[('host_temp', 'cpu')] == 93.0 and rows[('host_temp', 'nvme')] == 55.0
    assert 'host_temp' in app.HISTORY_METRICS


def test_resources_route_carries_temps(client, monkeypatch):
    monkeypatch.setattr(app, '_host_temps', lambda *a, **k: [])
    r = client.get('/api/system/resources').get_json()
    assert r['temps'] == {'cpu': None, 'nvme': None, 'sensors': []}
