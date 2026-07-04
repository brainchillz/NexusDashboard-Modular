import json
import pytest
import app


def _raw(doc):
    return 200, json.dumps(doc).encode()


def test_sync_returns_metadata(monkeypatch):
    monkeypatch.setattr(app, 'lxd_raw', lambda m, p, b=None, timeout=60:
                        _raw({'type': 'sync', 'metadata': {'hello': 'world'}}))
    assert app.lxd_request('GET', '/1.0') == {'hello': 'world'}


def test_error_type_raises(monkeypatch):
    monkeypatch.setattr(app, 'lxd_raw', lambda m, p, b=None, timeout=60:
                        (400, json.dumps({'type': 'error', 'error_code': 400,
                                          'error': 'nope'}).encode()))
    with pytest.raises(app.LxdError) as ei:
        app.lxd_request('GET', '/1.0/instances/x')
    assert ei.value.status == 400
    assert 'nope' in ei.value.message


def test_async_waits_on_operation(monkeypatch):
    seen = {}
    def fake_raw(method, path, body=None, timeout=60):
        if path == '/1.0/instances':
            return _raw({'type': 'async', 'operation': '/1.0/operations/abc'})
        if path.startswith('/1.0/operations/abc/wait'):
            seen['waited'] = True
            return _raw({'type': 'sync',
                         'metadata': {'status_code': 200, 'err': '', 'status': 'Success'}})
        raise AssertionError('unexpected path ' + path)
    monkeypatch.setattr(app, 'lxd_raw', fake_raw)
    meta = app.lxd_request('POST', '/1.0/instances', {'name': 'x'})
    assert seen.get('waited')
    assert meta['status'] == 'Success'


def test_async_failed_operation_raises(monkeypatch):
    def fake_raw(method, path, body=None, timeout=60):
        if path.startswith('/1.0/operations/'):
            return _raw({'type': 'sync',
                         'metadata': {'status_code': 400, 'err': 'boom'}})
        return _raw({'type': 'async', 'operation': '/1.0/operations/xyz'})
    monkeypatch.setattr(app, 'lxd_raw', fake_raw)
    with pytest.raises(app.LxdError) as ei:
        app.lxd_request('PUT', '/1.0/instances/x/state', {'action': 'start'})
    assert 'boom' in ei.value.message


def test_instance_addresses_extracts_globals():
    state = {'network': {
        'lo': {'addresses': [{'family': 'inet', 'scope': 'local', 'address': '127.0.0.1'}]},
        'eth0': {'addresses': [
            {'family': 'inet', 'scope': 'global', 'address': '10.0.0.5'},
            {'family': 'inet6', 'scope': 'link', 'address': 'fe80::1'},
            {'family': 'inet6', 'scope': 'global', 'address': '2001:db8::1'},
        ]},
    }}
    v4, v6 = app._instance_addresses(state)
    assert v4 == ['10.0.0.5']
    assert v6 == ['2001:db8::1']  # link-local and loopback excluded


def test_instance_summary_shape():
    inst = {'name': 'c1', 'type': 'container', 'status': 'Running',
            'config': {'image.os': 'Alpine', 'image.release': '3.24'},
            'state': {'network': {}, 'memory': {'usage': 1024}}}
    s = app._instance_summary(inst)
    assert s['name'] == 'c1' and s['os'] == 'Alpine' and s['memory'] == 1024
