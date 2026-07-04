import json
import pytest
import app


def test_host_interfaces_bridgeable(monkeypatch):
    addr = [
        {'ifname': 'lo', 'flags': ['LOOPBACK', 'UP'], 'addr_info': [
            {'family': 'inet', 'local': '127.0.0.1'}]},
        {'ifname': 'eno1', 'operstate': 'UP', 'flags': ['UP', 'LOWER_UP'],
         'addr_info': [{'family': 'inet', 'local': '192.168.10.9'}]},
        {'ifname': 'enp197s0', 'operstate': 'UP', 'flags': ['UP', 'LOWER_UP'],
         'addr_info': [{'family': 'inet6', 'local': 'fe80::1'}]},  # link-local only → no host IPv4
        {'ifname': 'lxdbr0', 'operstate': 'UP', 'flags': ['UP', 'LOWER_UP'],
         'addr_info': [{'family': 'inet', 'local': '10.0.0.1'}]},
    ]
    route = [{'dst': 'default', 'dev': 'eno1', 'gateway': '192.168.10.1'}]

    def fake_run(args, input_data=None):
        if args[:3] == ['ip', '-j', 'addr']:
            return json.dumps(addr), '', 0
        if args[:3] == ['ip', '-j', 'route']:
            return json.dumps(route), '', 0
        return '', '', 1
    monkeypatch.setattr(app, 'run', fake_run)
    monkeypatch.setattr(app, 'lxd_request', lambda *a, **k: [
        {'name': 'eno1', 'type': 'physical'},
        {'name': 'enp197s0', 'type': 'physical'},
        {'name': 'lxdbr0', 'type': 'bridge'},
    ])
    ifs = {i['name']: i for i in app._host_interfaces()}
    assert 'lo' not in ifs
    # eno1 carries the host IP + default route → NOT bridgeable.
    assert ifs['eno1']['has_ip'] and ifs['eno1']['is_default_route']
    assert ifs['eno1']['bridgeable'] is False
    # enp197s0 has no host IPv4, not default route, physical → bridgeable.
    assert ifs['enp197s0']['has_ip'] is False
    assert ifs['enp197s0']['bridgeable'] is True
    # a managed bridge is not a bridgeable uplink.
    assert ifs['lxdbr0']['bridgeable'] is False


def test_nic_device_for_managed(monkeypatch):
    monkeypatch.setattr(app, 'lxd_request', lambda *a, **k: {'managed': True, 'type': 'bridge'})
    dev = app._nic_device_for('lanbr0', 'eth0')
    assert dev == {'type': 'nic', 'name': 'eth0', 'network': 'lanbr0'}


def test_nic_device_for_unmanaged_bridge(monkeypatch):
    monkeypatch.setattr(app, 'lxd_request', lambda *a, **k: {'managed': False, 'type': 'bridge'})
    dev = app._nic_device_for('bridge0', 'eth0')
    assert dev == {'type': 'nic', 'name': 'eth0', 'nictype': 'bridged', 'parent': 'bridge0'}


def test_nic_device_for_bad_type(monkeypatch):
    monkeypatch.setattr(app, 'lxd_request', lambda *a, **k: {'managed': False, 'type': 'physical'})
    with pytest.raises(app.LxdError):
        app._nic_device_for('eno1', 'eth0')


def test_network_kinds_and_cidr():
    assert app.NET_KINDS == {'nat', 'bridge-lan', 'macvlan'}
    assert app.RE_CIDR.match('10.10.0.1/24')
    assert not app.RE_CIDR.match('auto')
    assert not app.RE_CIDR.match('10.10.0.1')       # needs a prefix length
    assert app.RE_CT_DEVNAME.match('eth0')
    assert not app.RE_CT_DEVNAME.match('eth 0')
