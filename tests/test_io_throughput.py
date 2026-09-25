"""Disk + network throughput (3.6.0): /proc/diskstats and /proc/net/dev
deltas → bytes/s for the resources poll (in-process) and the 5-minute
history tick (previous counters in io_prev.json, since every tick is its own
process). Whole disks and real interfaces only."""
import json
import pytest

import app
from nexusdash.core import summary as summ, history as hist

DISKSTATS = """   8       0 sda 1000 0 2000 0 500 0 8000 0 0 0 0 0 0 0 0 0 0
   8       1 sda1 900 0 1800 0 400 0 7000 0 0 0 0 0 0 0 0 0 0
 259       0 nvme0n1 10 0 4000 0 20 0 16000 0 0 0 0 0 0 0 0 0 0
   7       0 loop0 1 0 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
 253       0 dm-0 1 0 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
   9     127 md127 1 0 512 0 1 0 1024 0 0 0 0 0 0 0 0 0 0
"""
NETDEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0
 enp5s0: 1000000 100 0 0 0 0 0 0 200000 50 0 0 0 0 0 0
docker0: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0
veth1ab: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0
   br0: 3000 3 0 0 0 0 0 0 4000 4 0 0 0 0 0 0
"""


def test_parse_diskstats_whole_disks_only():
    d = summ._parse_diskstats(DISKSTATS)
    assert set(d) == {'sda', 'nvme0n1', 'md127'}
    assert d['sda'] == (2000 * 512, 8000 * 512)


def test_parse_netdev_skips_plumbing():
    n = summ._parse_netdev(NETDEV)
    assert set(n) == {'enp5s0', 'br0'}
    assert n['enp5s0'] == (1000000, 200000)


def test_rates_are_per_second_deltas_with_totals_and_wrap_guard():
    prev = {'ts': 100.0, 'disks': {'sda': (0, 0), 'sdb': (5000, 5000)}, 'net': {'eth0': (0, 0)}}
    cur = {'ts': 110.0, 'disks': {'sda': (10240, 20480), 'sdb': (100, 100), 'sdc': (1, 1)}, 'net': {'eth0': (1000, 2000)}}
    r = summ._io_rates(prev, cur)
    assert r['interval_s'] == 10.0
    assert r['disks'] == {'sda': {'read_bps': 1024, 'write_bps': 2048}}      # sdb wrapped, sdc is new → skipped
    assert r['disk_total'] == {'read_bps': 1024, 'write_bps': 2048}
    assert r['net'] == {'eth0': {'rx_bps': 100, 'tx_bps': 200}} and r['net_total'] == {'rx_bps': 100, 'tx_bps': 200}
    assert summ._io_rates(None, cur) is None
    assert summ._io_rates(cur, cur) is None                                   # dt == 0


def test_live_rates_need_two_polls_and_ignore_bursts(monkeypatch):
    snaps = iter([{'ts': 1.0, 'disks': {'sda': (0, 0)}, 'net': {}},
                  {'ts': 1.2, 'disks': {'sda': (100, 0)}, 'net': {}},       # burst: same second
                  {'ts': 3.0, 'disks': {'sda': (2048, 0)}, 'net': {}},      # >1 s but < min interval: rate, no re-anchor
                  {'ts': 31.0, 'disks': {'sda': (3072, 0)}, 'net': {}}])
    monkeypatch.setattr(summ, '_io_counters', lambda: next(snaps))
    monkeypatch.setattr(summ, '_IO_PREV', {})
    assert summ._io_rates_live() is None
    assert summ._io_rates_live() is None
    assert summ._io_rates_live()['disks']['sda']['read_bps'] == 1024        # over 2 s since ts=1
    assert summ._io_rates_live()['disks']['sda']['read_bps'] == 102         # over 30 s since ts=1 (anchor kept)
    assert summ._IO_PREV['snap']['ts'] == 31.0


def test_history_tick_uses_the_state_file(monkeypatch, tmp_path):
    state = tmp_path / 'io_prev.json'
    monkeypatch.setattr(hist, 'IO_STATE_FILE', str(state))
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    monkeypatch.setattr(app, '_system_resources', lambda: {})
    monkeypatch.setattr(app, '_host_temps', lambda *a, **k: [])
    snaps = iter([{'ts': 1000.0, 'disks': {'sda': (0, 0)}, 'net': {'eth0': (0, 0)}},
                  {'ts': 1300.0, 'disks': {'sda': (300 * 1024, 300 * 2048)}, 'net': {'eth0': (300 * 10, 300 * 20)}}])
    monkeypatch.setattr(hist, '_io_counters', lambda: next(snaps))
    rows = hist._history_sample()
    assert not [r for r in rows if r[0].endswith('_bps')]                     # first tick: nothing to diff against
    assert json.load(open(state))['disks']['sda'] == [0, 0]
    rows = {(m, l): v for m, l, v in hist._history_sample()}
    assert rows[('disk_read_bps', 'sda')] == 1024 and rows[('disk_write_bps', 'total')] == 2048
    assert rows[('net_rx_bps', 'eth0')] == 10 and rows[('net_tx_bps', 'total')] == 20
    assert {'disk_read_bps', 'disk_write_bps', 'net_rx_bps', 'net_tx_bps'} <= app.HISTORY_METRICS


def test_resources_route_carries_io(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(app, '_host_temps', lambda *a, **k: [])
    monkeypatch.setattr(summ, '_io_rates_live', lambda: {'interval_s': 30.0, 'disks': {}, 'net': {},
                                                          'disk_total': {'read_bps': 1, 'write_bps': 2},
                                                          'net_total': {'rx_bps': 3, 'tx_bps': 4}})
    app.app.config['TESTING'] = True
    r = app.app.test_client().get('/api/system/resources').get_json()
    assert r['io']['disk_total'] == {'read_bps': 1, 'write_bps': 2}
