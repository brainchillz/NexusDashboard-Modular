"""Docker module — Engine-API path building, log demux, stats math, and the
degraded-without-daemon contract. All socket I/O is faked; no Docker needed."""
import json
import pytest

import app


# ─── Validators ──────────────────────────────────────────────────────────

def test_dk_name_regex():
    ok = ['immich', 'immich_server', 'a1b2c3d4e5f6', 'my-app.web', 'A' * 64]
    for n in ok:
        assert app.RE_DK_NAME.match(n), n
    bad = ['', '-lead', '.lead', 'has space', 'a/b', 'x' * 200, 'nl\n']
    for n in bad:
        assert not app.RE_DK_NAME.match(n), n


def test_dk_image_regex():
    ok = ['nginx', 'nginx:latest', 'library/nginx:1.25',
          'ghcr.io/owner/image:tag', 'registry.example.com:5000/a/b:v1',
          'redis@sha256:' + 'a' * 64]
    for r in ok:
        assert app.RE_DK_IMAGE.match(r), r
    bad = ['', '-x', 'has space', 'a\nb', 'a;b', 'a|b']
    for r in bad:
        assert not app.RE_DK_IMAGE.match(r), r


def test_dk_subnet_regex():
    assert app.RE_DK_SUBNET.match('172.30.0.0/16')
    assert app.RE_DK_SUBNET.match('10.0.0.0/8')
    for s in ('not-a-subnet', '172.30.0.0', '172.30.0.0/16/24', 'fe80::/64'):
        assert not app.RE_DK_SUBNET.match(s), s


# ─── Log demux ───────────────────────────────────────────────────────────

def _frame(stream, payload):
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, 'big') + payload


def test_demux_multiplexed_stream():
    raw = _frame(1, b'stdout line\n') + _frame(2, b'stderr line\n')
    assert app._dk_demux_logs(raw) == b'stdout line\nstderr line\n'


def test_demux_passes_tty_output_through():
    raw = b'plain terminal output, no framing\n'
    assert app._dk_demux_logs(raw) == raw


def test_demux_empty_and_truncated():
    assert app._dk_demux_logs(b'') == b''
    # Truncated final frame: keep what decodes, never crash.
    raw = _frame(1, b'ok\n') + b'\x01\x00\x00'
    assert app._dk_demux_logs(raw).startswith(b'ok\n')


# ─── Stats math ──────────────────────────────────────────────────────────

def test_stats_summary_math():
    s = {
        'cpu_stats': {'cpu_usage': {'total_usage': 400}, 'system_cpu_usage': 2000,
                      'online_cpus': 4},
        'precpu_stats': {'cpu_usage': {'total_usage': 200}, 'system_cpu_usage': 1000},
        'memory_stats': {'usage': 1000000, 'limit': 4000000,
                         'stats': {'inactive_file': 200000}},
        'networks': {'eth0': {'rx_bytes': 10, 'tx_bytes': 20},
                     'eth1': {'rx_bytes': 1, 'tx_bytes': 2}},
        'blkio_stats': {'io_service_bytes_recursive': [
            {'op': 'read', 'value': 100}, {'op': 'Write', 'value': 50},
            {'op': 'Read', 'value': 10}]},
        'pids_stats': {'current': 12},
    }
    out = app._dk_stats_summary(s)
    # (200 / 1000) * 4 cpus * 100 = 80%
    assert out['cpu_pct'] == 80.0
    assert out['mem_usage'] == 800000 and out['mem_limit'] == 4000000
    assert out['net_rx'] == 11 and out['net_tx'] == 22
    assert out['blk_read'] == 110 and out['blk_write'] == 50
    assert out['pids'] == 12


def test_stats_summary_tolerates_missing_fields():
    out = app._dk_stats_summary({})
    assert out['cpu_pct'] is None and out['mem_usage'] is None
    assert out['net_rx'] == 0 and out['pids'] is None


# ─── Summaries / ports ───────────────────────────────────────────────────

def test_ports_summary():
    ports = [
        {'IP': '0.0.0.0', 'PublicPort': 8080, 'PrivatePort': 80, 'Type': 'tcp'},
        {'IP': '::', 'PublicPort': 8080, 'PrivatePort': 80, 'Type': 'tcp'},
        {'IP': '127.0.0.1', 'PublicPort': 5000, 'PrivatePort': 5000, 'Type': 'tcp'},
        {'PrivatePort': 53, 'Type': 'udp'},
    ]
    assert app._dk_ports(ports) == [
        '127.0.0.1:5000->5000/tcp', '53/udp', '8080->80/tcp']


def test_container_summary_mapping():
    c = {'Id': 'a' * 64, 'Names': ['/immich_server'], 'Image': 'ghcr.io/immich-app/server',
         'State': 'running', 'Status': 'Up 3 days (healthy)', 'Created': 1751500000,
         'Ports': [], 'Labels': {'com.docker.compose.project': 'immich'}}
    out = app._dk_container_summary(c)
    assert out['id'] == 'a' * 12
    assert out['name'] == 'immich_server'
    assert out['compose_project'] == 'immich'
    assert out['state'] == 'running'


# ─── Routes (through the facade test client) ─────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


def _fake_request(responses, calls=None):
    """docker_request stub keyed on (method, path-prefix); records calls."""
    calls = calls if calls is not None else []
    def fake(method, path, body=None, timeout=60):
        calls.append((method, path, body))
        for (m, prefix), resp in responses.items():
            if method == m and path.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return {}
    return fake, calls


def test_overview_degrades_without_daemon(client, monkeypatch):
    monkeypatch.setattr(app, 'docker_request',
                        _fake_request({('GET', '/info'):
                                       app.DockerError(502, 'no socket')})[0])
    r = client.get('/api/docker')
    j = r.get_json()
    assert r.status_code == 200
    assert j['reachable'] is False and 'no socket' in j['error']


def test_overview_reports_engine(client, monkeypatch):
    fake, _ = _fake_request({
        ('GET', '/info'): {'Containers': 5, 'ContainersRunning': 3, 'Images': 7,
                           'Driver': 'overlay2', 'OperatingSystem': 'Ubuntu'},
        ('GET', '/version'): {'Version': '27.1.1', 'ApiVersion': '1.46'},
    })
    monkeypatch.setattr(app, 'docker_request', fake)
    j = client.get('/api/docker').get_json()
    assert j['reachable'] and j['version'] == '27.1.1'
    assert j['running'] == 3 and j['storage_driver'] == 'overlay2'


def test_container_action_paths(client, monkeypatch):
    fake, calls = _fake_request({})
    monkeypatch.setattr(app, 'docker_request', fake)
    r = client.post('/api/docker/containers/abc123/action', json={'action': 'stop'})
    assert r.get_json()['success']
    assert calls == [('POST', '/containers/abc123/stop?t=10', None)]
    calls.clear()
    client.post('/api/docker/containers/abc123/action', json={'action': 'start'})
    assert calls == [('POST', '/containers/abc123/start', None)]
    # Unknown action / bad id never reach the daemon.
    r = client.post('/api/docker/containers/abc123/action', json={'action': 'nuke'})
    assert r.status_code == 400
    r = client.post('/api/docker/containers/-badlead/action', json={'action': 'start'})
    assert r.status_code == 400
    assert len(calls) == 1


def test_container_delete_flags(client, monkeypatch):
    fake, calls = _fake_request({})
    monkeypatch.setattr(app, 'docker_request', fake)
    client.post('/api/docker/containers/abc/delete', json={'force': True, 'volumes': True})
    m, path, _ = calls[0]
    assert m == 'DELETE' and path.startswith('/containers/abc?')
    assert 'force=1' in path and 'v=1' in path
    calls.clear()
    client.post('/api/docker/containers/abc/delete', json={})
    assert 'force=0' in calls[0][1] and 'v=0' in calls[0][1]


def test_images_list_marks_dangling_and_in_use(client, monkeypatch):
    fake, _ = _fake_request({
        ('GET', '/images/json'): [
            {'Id': 'sha256:' + 'a' * 64, 'RepoTags': ['nginx:latest'],
             'Size': 100, 'Created': 2},
            {'Id': 'sha256:' + 'b' * 64, 'RepoTags': [], 'Size': 50, 'Created': 1},
        ],
        ('GET', '/containers/json'): [{'ImageID': 'sha256:' + 'a' * 64}],
    })
    monkeypatch.setattr(app, 'docker_request', fake)
    imgs = client.get('/api/docker/images').get_json()
    assert imgs[0]['id'] == 'a' * 12 and imgs[0]['in_use'] is True
    assert imgs[1]['dangling'] is True and imgs[1]['in_use'] is False


def test_image_pull_validates_and_surfaces_stream_error(client, monkeypatch):
    raw_calls = []
    def fake_raw(method, path, body=None, timeout=60):
        raw_calls.append((method, path))
        lines = [json.dumps({'status': 'Pulling'}),
                 json.dumps({'error': 'manifest unknown'})]
        return 200, '\n'.join(lines).encode()
    monkeypatch.setattr(app, 'docker_raw', fake_raw)
    r = client.post('/api/docker/images/pull', json={'reference': 'bad;ref'})
    assert r.status_code == 400 and raw_calls == []
    r = client.post('/api/docker/images/pull',
                    json={'reference': 'ghcr.io/owner/missing:v1'})
    assert r.status_code == 500
    assert 'manifest unknown' in r.get_json()['error']
    assert raw_calls[0][0] == 'POST'
    assert 'fromImage=ghcr.io%2Fowner%2Fmissing%3Av1' in raw_calls[0][1]


def test_image_delete_quotes_reference(client, monkeypatch):
    fake, calls = _fake_request({})
    monkeypatch.setattr(app, 'docker_request', fake)
    client.post('/api/docker/images/delete', json={'id': 'ghcr.io/owner/img:v1'})
    assert calls[0][1] == '/images/ghcr.io%2Fowner%2Fimg%3Av1?force=0'


def test_volumes_list_reports_users(client, monkeypatch):
    fake, _ = _fake_request({
        ('GET', '/volumes'): {'Volumes': [
            {'Name': 'appdata', 'Driver': 'local', 'Mountpoint': '/var/lib/docker/volumes/appdata/_data'},
            {'Name': 'scratch', 'Driver': 'local', 'Mountpoint': '/x'}]},
        ('GET', '/containers/json'): [
            {'Names': ['/web'], 'Mounts': [{'Type': 'volume', 'Name': 'appdata'}]}],
    })
    monkeypatch.setattr(app, 'docker_request', fake)
    vols = client.get('/api/docker/volumes').get_json()
    assert vols[0]['used_by'] == ['web'] and vols[1]['used_by'] == []


def test_volume_and_network_name_validation(client, monkeypatch):
    fake, calls = _fake_request({})
    monkeypatch.setattr(app, 'docker_request', fake)
    for path in ('/api/docker/volumes/create', '/api/docker/volumes/delete',
                 '/api/docker/networks/create', '/api/docker/networks/delete'):
        r = client.post(path, json={'name': 'bad name'})
        assert r.status_code == 400, path
    assert calls == []


def test_network_create_builds_body_and_guards_builtin(client, monkeypatch):
    fake, calls = _fake_request({})
    monkeypatch.setattr(app, 'docker_request', fake)
    r = client.post('/api/docker/networks/create',
                    json={'name': 'lab', 'subnet': '172.30.0.0/16'})
    assert r.get_json()['success']
    m, path, body = calls[0]
    assert (m, path) == ('POST', '/networks/create')
    assert body['Name'] == 'lab' and body['Driver'] == 'bridge'
    assert body['IPAM']['Config'] == [{'Subnet': '172.30.0.0/16'}]
    # Reserved names and bad subnets are refused before the daemon.
    calls.clear()
    assert client.post('/api/docker/networks/create',
                       json={'name': 'bridge'}).status_code == 400
    assert client.post('/api/docker/networks/create',
                       json={'name': 'lab2', 'subnet': 'junk'}).status_code == 400
    assert client.post('/api/docker/networks/delete',
                       json={'name': 'host'}).status_code == 400
    assert calls == []


def test_networks_list_counts_and_builtin_flag(client, monkeypatch):
    fake, _ = _fake_request({
        ('GET', '/networks'): [
            {'Id': 'n1' * 32, 'Name': 'bridge', 'Driver': 'bridge', 'Scope': 'local',
             'IPAM': {'Config': [{'Subnet': '172.17.0.0/16'}]}},
            {'Id': 'n2' * 32, 'Name': 'immich_default', 'Driver': 'bridge',
             'Scope': 'local', 'IPAM': {'Config': []}}],
        ('GET', '/containers/json'): [
            {'NetworkSettings': {'Networks': {'immich_default': {}}}},
            {'NetworkSettings': {'Networks': {'immich_default': {}, 'bridge': {}}}}],
    })
    monkeypatch.setattr(app, 'docker_request', fake)
    nets = client.get('/api/docker/networks').get_json()
    by_name = {n['name']: n for n in nets}
    assert by_name['bridge']['builtin'] is True
    assert by_name['bridge']['containers'] == 1
    assert by_name['immich_default']['containers'] == 2
    assert by_name['immich_default']['subnets'] == []


def test_logs_demux_by_tty_flag(client, monkeypatch):
    insp = {'Config': {'Tty': False}}
    framed = (b'\x01\x00\x00\x00\x00\x00\x00\x05hello')
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: insp)
    monkeypatch.setattr(app, 'docker_raw', lambda *a, **k: (200, framed))
    j = client.get('/api/docker/containers/abc/logs?tail=50').get_json()
    assert j['logs'] == 'hello' and j['tail'] == 50
    # TTY container: bytes pass through untouched.
    insp['Config']['Tty'] = True
    monkeypatch.setattr(app, 'docker_raw', lambda *a, **k: (200, b'raw text'))
    j = client.get('/api/docker/containers/abc/logs').get_json()
    assert j['logs'] == 'raw text' and j['tail'] == 200


def test_docker_request_error_shapes(monkeypatch):
    monkeypatch.setattr(app, 'docker_raw',
                        lambda *a, **k: (404, b'{"message": "no such container"}'))
    with pytest.raises(app.DockerError) as e:
        app.docker_request('GET', '/containers/x/json')
    assert e.value.status == 404 and 'no such container' in e.value.message
    # 304 (already stopped/started) is success, and empty bodies decode to {}.
    monkeypatch.setattr(app, 'docker_raw', lambda *a, **k: (304, b''))
    assert app.docker_request('POST', '/containers/x/start') == {}


# ─── Container create (tier 2a) ──────────────────────────────────────────

def test_create_body_translation():
    body, bad = app._dk_create_body({
        'image': 'nginx:latest',
        'ports': [{'host': 8080, 'container': 80, 'proto': 'tcp'},
                  {'host_ip': '127.0.0.1', 'host': 5432, 'container': 5432, 'proto': 'tcp'}],
        'volumes': [{'source': 'appdata', 'destination': '/data'},
                    {'source': '/srv/media', 'destination': '/media', 'ro': True}],
        'env': ['TZ=America/New_York', ''],
        'restart': 'unless-stopped',
        'network': 'lab',
        'command': 'nginx -g "daemon off;"',
    })
    assert bad is None
    assert body['Image'] == 'nginx:latest'
    assert body['Env'] == ['TZ=America/New_York']
    assert body['ExposedPorts'] == {'80/tcp': {}, '5432/tcp': {}}
    hc = body['HostConfig']
    assert hc['PortBindings']['80/tcp'] == [{'HostIp': '', 'HostPort': '8080'}]
    assert hc['PortBindings']['5432/tcp'] == [{'HostIp': '127.0.0.1', 'HostPort': '5432'}]
    assert hc['Binds'] == ['appdata:/data', '/srv/media:/media:ro']
    assert hc['RestartPolicy'] == {'Name': 'unless-stopped'}
    assert hc['NetworkMode'] == 'lab'
    assert body['Cmd'] == ['nginx', '-g', 'daemon off;']


@pytest.mark.parametrize('overrides,msg', [
    ({'image': 'bad image'}, 'image'),
    ({'restart': 'sometimes'}, 'Restart'),
    ({'env': ['1BAD=x']}, 'Environment'),
    ({'env': ['NOEQUALS']}, 'Environment'),
    ({'ports': [{'host': 'x', 'container': 80}]}, 'Ports'),
    ({'ports': [{'host': 0, 'container': 80}]}, 'Ports'),
    ({'ports': [{'host': 80, 'container': 80, 'proto': 'icmp'}]}, 'protocol'),
    ({'volumes': [{'source': 'v', 'destination': 'relative'}]}, 'destination'),
    ({'volumes': [{'source': 'v', 'destination': '/a/../etc'}]}, 'destination'),
    ({'volumes': [{'source': '/host/../etc', 'destination': '/d'}]}, 'source'),
    ({'volumes': [{'source': 'has space', 'destination': '/d'}]}, 'source'),
    ({'network': 'bad name'}, 'network'),
    ({'command': 'unclosed "quote'}, 'Command'),
])
def test_create_body_rejects_bad_input(overrides, msg):
    data = dict({'image': 'nginx'}, **overrides)
    body, bad = app._dk_create_body(data)
    assert body is None and msg.lower() in bad.lower()


def test_create_route_creates_and_starts(client, monkeypatch):
    fake, calls = _fake_request({
        ('POST', '/containers/create'): {'Id': 'c' * 64, 'Warnings': []},
    })
    monkeypatch.setattr(app, 'docker_request', fake)
    r = client.post('/api/docker/containers',
                    json={'name': 'web', 'image': 'nginx', 'start': True})
    j = r.get_json()
    assert j['success'] and j['id'] == 'c' * 12
    assert calls[0][0] == 'POST' and calls[0][1] == '/containers/create?name=web'
    assert calls[1] == ('POST', '/containers/%s/start' % ('c' * 12), None)


def test_create_route_auto_pulls_missing_image(client, monkeypatch):
    attempts = []
    def fake_req(method, path, body=None, timeout=60):
        attempts.append(path)
        if path.startswith('/containers/create') and len(attempts) == 1:
            raise app.DockerError(404, 'No such image: ghcr.io/x/y:v1')
        return {'Id': 'd' * 64, 'Warnings': []}
    pulled = []
    monkeypatch.setattr(app, 'docker_request', fake_req)
    monkeypatch.setattr(app, '_dk_pull', lambda ref, timeout=600: pulled.append(ref))
    r = client.post('/api/docker/containers',
                    json={'image': 'ghcr.io/x/y:v1', 'start': False})
    assert r.get_json()['success']
    assert pulled == ['ghcr.io/x/y:v1']
    assert [p for p in attempts if p.startswith('/containers/create')] == \
        ['/containers/create', '/containers/create']


# ─── Exec shell (tier 2a) ────────────────────────────────────────────────

def test_parse_hijack_head():
    code, left = app._parse_hijack_head(
        b'HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\n\r\n$ ')
    assert code == 101 and left == b'$ '
    code, left = app._parse_hijack_head(b'HTTP/1.1 200 OK\r\n\r\n')
    assert code == 200 and left == b''
    with pytest.raises(app.DockerError):
        app._parse_hijack_head(b'not http at all\r\n\r\n')
    with pytest.raises(app.DockerError):
        app._parse_hijack_head(b'HTTP/1.1 101 no terminator')


def test_exec_create_requests_tty_shell(monkeypatch):
    calls = []
    def fake_req(method, path, body=None, timeout=60):
        calls.append((method, path, body))
        return {'Id': 'e' * 64}
    monkeypatch.setattr(app, 'docker_request', fake_req)
    eid = app._exec_create('abc123')
    assert eid == 'e' * 64
    m, path, body = calls[0]
    assert (m, path) == ('POST', '/containers/abc123/exec')
    assert body['Tty'] and body['AttachStdin']
    assert body['Cmd'] == app.DK_SHELL_CMD


# ─── Module registration & summary hook ──────────────────────────────────

def test_docker_module_registered():
    assert 'docker' in app.MODULE_IDS
    desc = app._DESCRIPTORS['docker']
    assert desc['category'] == 'Docker'
    assert desc['summary'] is app._docker_summary


def test_docker_summary_hook(monkeypatch):
    monkeypatch.setattr(app, 'docker_request',
                        lambda *a, **k: {'Containers': 4, 'ContainersRunning': 2,
                                         'Images': 9})
    assert app._docker_summary() == {'docker': {'containers': 4, 'running': 2,
                                                'images': 9}}
    monkeypatch.setattr(app, 'docker_request',
                        _fake_request({('GET', '/info'):
                                       app.DockerError(502, 'down')})[0])
    assert app._docker_summary() == {}


def test_docker_gate_disabled_module(client, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'docker'})
    r = client.get('/api/docker/containers')
    assert r.status_code == 403
    assert 'disabled' in r.get_json()['error']
