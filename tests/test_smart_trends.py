"""SMART trends (3.6.0): the disks module samples each whole disk's SMART
counters into history every tick (labelled by serial), remembers the last
values in smart_state.json, records any growth in a defect counter as an
event, and alerts on it for a week. smartctl/lsblk are faked."""
import json
import time
import pytest

import app
from nexusdash.modules import disks

ATA = json.dumps({
    'model_name': 'ST20000NM007D-3DJ103', 'serial_number': 'ZVT7Q9X1', 'firmware_version': 'SN03',
    'rotation_rate': 7200, 'user_capacity': {'bytes': 20000588955648},
    'smart_status': {'passed': True}, 'temperature': {'current': 38}, 'power_on_time': {'hours': 1200},
    'ata_smart_attributes': {'table': [
        {'name': 'Reallocated_Sector_Ct', 'raw': {'value': 0}},
        {'name': 'Current_Pending_Sector', 'raw': {'value': 0}},
        {'name': 'Offline_Uncorrectable', 'raw': {'value': 0}}]}})
NVME = json.dumps({
    'model_name': 'Samsung SSD 990 PRO 2TB', 'serial_number': 'S6Z2NJ0W123456X',
    'smart_status': {'passed': True},
    'nvme_smart_health_information_log': {'temperature': 44, 'power_on_hours': 300, 'media_errors': 0,
                                          'percentage_used': 3, 'critical_warning': 0}})


def _runner(by_dev, standby_rc=0, calls=None):
    def fake(args, **kw):
        if calls is not None:
            calls.append(list(args))
        if args[0] == 'lsblk':
            return (json.dumps({'blockdevices': [{'name': d, 'type': 'disk'} for d in by_dev] + [{'name': 'sda1', 'type': 'part'}]}), '', 0)
        if args[0] == 'smartctl':
            dev = args[-1].split('/')[-1]
            out = by_dev.get(dev)
            if out == 'STANDBY':
                return ('{"smartctl": {"exit_status": 2}}', '', 2)
            return (out or '', '', 0)
        return ('', '', 1)
    return fake


@pytest.fixture
def state_file(monkeypatch, tmp_path):
    p = tmp_path / 'smart_state.json'
    monkeypatch.setattr(disks, 'SMART_STATE_FILE', str(p))
    return p


def test_smart_info_normalizes_ata_and_nvme(monkeypatch):
    monkeypatch.setattr(app, 'run', _runner({'sda': ATA, 'nvme0n1': NVME}))
    a = disks._smart_info('sda')
    assert (a['serial'], a['health'], a['temperature_c'], a['reallocated']) == ('ZVT7Q9X1', 'PASSED', 38, 0)
    n = disks._smart_info('nvme0n1')
    assert (n['serial'], n['temperature_c'], n['media_errors'], n['percentage_used']) == ('S6Z2NJ0W123456X', 44, 0, 3)


def test_smart_info_standby_does_not_wake_the_disk(monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'run', _runner({'sdb': 'STANDBY'}, calls=calls))
    info = disks._smart_info('sdb', standby=True)
    assert info == {'device': 'sdb', 'available': False, 'standby': True}
    assert calls[0] == ['smartctl', '-H', '-A', '-i', '-j', '-n', 'standby', '/dev/sdb']


def test_apply_sample_rows_and_growth_event():
    state, now = {}, 1_700_000_000
    info = disks._smart_info.__wrapped__ if hasattr(disks._smart_info, '__wrapped__') else None
    reading = {'device': 'sda', 'available': True, 'serial': 'ZVT7Q9X1', 'model': 'ST20000',
               'temperature_c': 38, 'reallocated': 0, 'pending': 0, 'uncorrectable': 0, 'power_on_hours': 1200}
    rows = disks._smart_apply_sample(state, reading, now)
    assert {r[0] for r in rows} == {'smart_temp', 'smart_realloc', 'smart_pending', 'smart_uncorr', 'smart_power_on_hours'}
    assert all(r[1] == 'ZVT7Q9X1' for r in rows)
    assert state['ZVT7Q9X1']['growth'] == {} and state['ZVT7Q9X1']['first_seen'] == now
    # Next tick: reallocated moved 0 → 5. Temperature changes are not "growth".
    reading.update({'reallocated': 5, 'temperature_c': 41})
    disks._smart_apply_sample(state, reading, now + 300)
    assert state['ZVT7Q9X1']['growth'] == {'reallocated': {'from': 0, 'to': 5, 'ts': now + 300}}
    assert state['ZVT7Q9X1']['first_seen'] == now
    # A later tick with the same value keeps the event; a further rise updates it.
    disks._smart_apply_sample(state, reading, now + 600)
    assert state['ZVT7Q9X1']['growth']['reallocated']['to'] == 5
    reading['reallocated'] = 9
    disks._smart_apply_sample(state, reading, now + 900)
    assert state['ZVT7Q9X1']['growth']['reallocated'] == {'from': 5, 'to': 9, 'ts': now + 900}


def test_apply_sample_skips_unavailable_and_junk_serials():
    state = {}
    assert disks._smart_apply_sample(state, {'device': 'sdb', 'available': False, 'standby': True}, 1) == []
    assert disks._smart_apply_sample(state, {'device': 'sdc', 'available': True, 'serial': 'bad\nserial', 'temperature_c': 1}, 1) == []
    assert state == {}


def test_history_hook_samples_every_whole_disk_with_standby(monkeypatch, state_file):
    calls = []
    monkeypatch.setattr(app, 'run', _runner({'sda': ATA, 'nvme0n1': NVME, 'sdb': 'STANDBY'}, calls=calls))
    rows = disks._smart_history_samples()
    assert {r[1] for r in rows} == {'ZVT7Q9X1', 'S6Z2NJ0W123456X'}
    assert all('-n' in c for c in calls if c[0] == 'smartctl')
    assert not any(c[-1] == '/dev/sda1' for c in calls)
    st = json.load(open(state_file))
    assert set(st) == {'ZVT7Q9X1', 'S6Z2NJ0W123456X'} and st['ZVT7Q9X1']['dev'] == 'sda'
    assert disks.SMART_HISTORY_METRICS <= app.HISTORY_METRICS


def test_growth_alert_wording_and_expiry():
    now = 1_700_000_000
    state = {'ZVT7Q9X1': {'dev': 'sda', 'model': 'ST20000', 'growth': {
        'reallocated': {'from': 0, 'to': 5, 'ts': now - 3600},
        'pending': {'from': 0, 'to': 2, 'ts': now - 2 * 86400}}},
        'OLD1': {'dev': 'sdb', 'model': 'X', 'growth': {'reallocated': {'from': 1, 'to': 2, 'ts': now - 30 * 86400}}},
        'FINE': {'dev': 'sdc', 'model': 'Y', 'growth': {}}}
    alerts = disks._smart_growth_alerts(state, now)
    assert [a['key'] for a in alerts] == ['smart_growth:ZVT7Q9X1']
    m = alerts[0]['message']
    assert 'sda' in m and 'pending sectors 0→2' in m and 'reallocated sectors 0→5' in m and 'pre-failure' in m


def test_smart_route_carries_growth(monkeypatch, state_file):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(app, 'run', _runner({'sda': ATA}))
    state_file.write_text(json.dumps({'ZVT7Q9X1': {'dev': 'sda', 'first_seen': 5,
                                                   'growth': {'reallocated': {'from': 0, 'to': 5, 'ts': 6}}}}))
    app.app.config['TESTING'] = True
    r = app.app.test_client().get('/api/disks/sda/smart').get_json()
    assert r['growth'] == {'reallocated': {'from': 0, 'to': 5, 'ts': 6}} and r['first_seen'] == 5
    assert r['serial'] == 'ZVT7Q9X1'
