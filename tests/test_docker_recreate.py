"""Recreate (3.8.0): replace a container with one from the image its tag now
points at — `docker compose up -d --no-deps <service>` when the service user
can read the stack's files, otherwise a clone of the container's own settings
with the old container parked until the new one has stayed up.

No daemon: a small fake engine records every call and keeps just enough state
(names, running flags) for the swap and the put-back to be checked in order."""
import copy
import urllib.parse

import pytest

import app
from nexusdash.modules import docker as dk
from nexusdash.modules.docker import DockerError

OLD_IMG = 'sha256:' + '0' * 64
NEW_IMG = 'sha256:' + '9' * 64
OLD_ID = 'a' * 64
NEW_ID = 'b' * 64

IMAGE_CFG = {'Env': ['PATH=/usr/bin', 'APP_VERSION=1.0'], 'Cmd': ['serve'], 'Entrypoint': ['/init'],
             'WorkingDir': '/app', 'User': '', 'Labels': {'org.opencontainers.image.version': '1.0'},
             'Healthcheck': {'Test': ['CMD', 'true']}}


def container(**over):
    c = {
        'Id': OLD_ID, 'Name': '/web', 'Image': OLD_IMG,
        'State': {'Running': True},
        'Config': {'Image': 'example/web:latest', 'Hostname': OLD_ID[:12],
                   'Env': ['PATH=/usr/bin', 'APP_VERSION=1.0', 'TZ=UTC'],
                   'Cmd': ['serve'], 'Entrypoint': ['/init'], 'WorkingDir': '/app', 'User': '',
                   'Healthcheck': {'Test': ['CMD', 'true']}, 'MacAddress': '02:42:ac:11:00:02',
                   'Labels': {'org.opencontainers.image.version': '1.0', 'mine': 'yes'},
                   'ExposedPorts': {'80/tcp': {}}},
        'HostConfig': {'NetworkMode': 'front', 'Binds': ['/srv/web:/data', 'webcfg:/etc/web:ro'],
                       'PortBindings': {'80/tcp': [{'HostIp': '', 'HostPort': '8080'}]},
                       'RestartPolicy': {'Name': 'unless-stopped'}, 'CapAdd': ['NET_ADMIN']},
        'Mounts': [{'Type': 'bind', 'Source': '/srv/web', 'Destination': '/data', 'RW': True},
                   {'Type': 'volume', 'Name': 'webcfg', 'Destination': '/etc/web', 'RW': False},
                   {'Type': 'volume', 'Name': 'f' * 64, 'Destination': '/var/cache', 'RW': True}],
        'NetworkSettings': {'Networks': {
            'front': {'IPAMConfig': {'IPv4Address': '172.30.0.10'}, 'Aliases': ['web', OLD_ID[:12]],
                      'IPAddress': '172.30.0.10', 'EndpointID': 'x', 'MacAddress': '02:42:ac:1e:00:0a'},
            'back': {'IPAMConfig': None, 'Aliases': None, 'IPAddress': '172.31.0.4'}}},
    }
    for k, v in over.items():
        c[k] = v
    return c


class Engine:
    """Just enough Docker for the swap."""

    def __init__(self, info, fail=None, new_state=None, tag_image=NEW_IMG):
        self.info, self.fail, self.calls = info, fail or {}, []
        self.new_state = new_state or {'Running': True, 'Restarting': False}
        self.tag_image = tag_image
        self.names = {OLD_ID: info['Name'].lstrip('/')}
        self.running = {OLD_ID: info['State']['Running']}
        self.created = None

    def request(self, method, path, body=None, timeout=60):
        self.calls.append((method, path))
        for needle, exc in self.fail.items():
            if needle in '%s %s' % (method, path):
                raise exc
        p = urllib.parse.urlsplit(path)
        seg = p.path.strip('/').split('/')
        if seg[0] == 'images':
            ref = urllib.parse.unquote(seg[1])
            if ref == OLD_IMG:
                return {'Id': OLD_IMG, 'Config': IMAGE_CFG}
            if self.tag_image is None:
                raise DockerError(404, 'No such image: %s' % ref)
            return {'Id': self.tag_image}
        if seg[0] == 'networks':
            self.connected = (urllib.parse.unquote(seg[1]), body)
            return {}
        cid = seg[1]
        if cid == 'create':
            name = urllib.parse.parse_qs(p.query)['name'][0]
            assert name not in self.names.values(), 'name still taken by the old container'
            self.created, self.names[NEW_ID], self.running[NEW_ID] = copy.deepcopy(body), name, False
            return {'Id': NEW_ID}
        full = NEW_ID if NEW_ID.startswith(cid) else OLD_ID
        if method == 'GET':
            return {'State': self.new_state} if full == NEW_ID else self.info
        if method == 'DELETE':
            del self.names[full], self.running[full]
            return {}
        verb = seg[2]
        if verb == 'stop':
            self.running[full] = False
        elif verb == 'start':
            self.running[full] = True
        elif verb == 'rename':
            self.names[full] = urllib.parse.parse_qs(p.query)['name'][0]
        return {}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(dk, 'DK_RECREATE_SETTLE_S', 0)
    monkeypatch.setattr(dk, 'docker_raw', lambda *a, **k: (200, b'boom: bad config\n'))
    monkeypatch.setattr(dk, 'run', lambda *a, **k: pytest.fail('compose must not run here'))
    app.app.config['TESTING'] = True
    return app.app.test_client()


def engine(monkeypatch, info=None, **kw):
    e = Engine(info or container(), **kw)
    monkeypatch.setattr(dk, 'docker_request', e.request)
    return e


def verbs(e):
    """The mutating calls, in order, as 'verb target' with ids shortened."""
    out = []
    for m, p in e.calls:
        if m == 'GET':
            continue
        p = p.split('?')[0].replace(OLD_ID, 'OLD').replace(NEW_ID, 'NEW')
        out.append('%s %s' % (m, p))
    return out


# ── The clone path ─────────────────────────────────────────────────────
def test_clone_swaps_in_order_and_removes_the_old_one_last(client, monkeypatch):
    e = engine(monkeypatch)
    r = client.post('/api/docker/containers/web/recreate', json={})
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {'success': True, 'method': 'clone', 'id': NEW_ID[:12], 'warning': None}
    assert verbs(e) == ['POST /containers/OLD/stop', 'POST /containers/OLD/rename',
                        'POST /containers/create', 'POST /networks/back/connect',
                        'POST /containers/NEW/start', 'DELETE /containers/OLD']
    assert e.names == {NEW_ID: 'web'} and e.running == {NEW_ID: True}
    assert not any('v=1' in p for _, p in e.calls)                     # volumes are never removed


def test_clone_body_keeps_configuration_and_drops_what_the_old_image_supplied(client, monkeypatch):
    e = engine(monkeypatch)
    client.post('/api/docker/containers/web/recreate', json={})
    b = e.created
    assert b['Image'] == 'example/web:latest'
    assert b['Env'] == ['TZ=UTC']                                      # PATH / APP_VERSION come from the NEW image
    assert b['Labels'] == {'mine': 'yes'}
    for inherited in ('Cmd', 'Entrypoint', 'WorkingDir', 'User', 'Healthcheck', 'MacAddress', 'Hostname'):
        assert inherited not in b, inherited
    h = b['HostConfig']
    assert h['PortBindings'] == {'80/tcp': [{'HostIp': '', 'HostPort': '8080'}]} and b['ExposedPorts'] == {'80/tcp': {}}
    assert h['RestartPolicy'] == {'Name': 'unless-stopped'} and h['CapAdd'] == ['NET_ADMIN']
    # the anonymous volume is carried over by name; the two declared binds are not duplicated
    assert h['Binds'] == ['/srv/web:/data', 'webcfg:/etc/web:ro', 'f' * 64 + ':/var/cache']
    # primary network (static address + alias, minus the old id) at create, the other connected after
    assert b['NetworkingConfig'] == {'EndpointsConfig': {'front': {
        'IPAMConfig': {'IPv4Address': '172.30.0.10'}, 'Aliases': ['web']}}}
    assert e.connected == ('back', {'Container': NEW_ID, 'EndpointConfig': {}})


def test_clone_keeps_what_the_user_overrode(client, monkeypatch):
    info = container()
    info['Config'].update({'Cmd': ['serve', '--debug'], 'Hostname': 'web01', 'User': '1000'})
    e = engine(monkeypatch, info)
    client.post('/api/docker/containers/web/recreate', json={})
    assert e.created['Cmd'] == ['serve', '--debug'] and e.created['Hostname'] == 'web01' and e.created['User'] == '1000'


@pytest.mark.parametrize('mode', ['host', 'none', 'container:' + 'c' * 64])
def test_clone_special_network_modes_attach_no_endpoints(client, monkeypatch, mode):
    info = container()
    info['HostConfig']['NetworkMode'] = mode
    info['NetworkSettings'] = {'Networks': {'host': {'IPAddress': ''}}}
    e = engine(monkeypatch, info)
    assert client.post('/api/docker/containers/web/recreate', json={}).status_code == 200
    assert 'NetworkingConfig' not in e.created and not any('/networks/' in p for _, p in e.calls)
    if mode.startswith('container:'):                                  # these conflict with a shared stack
        assert 'ExposedPorts' not in e.created and e.created['HostConfig']['PortBindings'] == {}


def test_a_stopped_container_is_replaced_stopped(client, monkeypatch):
    e = engine(monkeypatch, container(State={'Running': False}))
    assert client.post('/api/docker/containers/web/recreate', json={}).status_code == 200
    assert verbs(e) == ['POST /containers/OLD/rename', 'POST /containers/create',
                        'POST /networks/back/connect', 'DELETE /containers/OLD']
    assert e.running == {NEW_ID: False}


# ── Put-back ───────────────────────────────────────────────────────────
@pytest.mark.parametrize('needle', ['POST /containers/create', 'POST /networks/back/connect',
                                    'POST /containers/' + NEW_ID + '/start'])
def test_any_failure_puts_the_old_container_back_running(client, monkeypatch, needle):
    e = engine(monkeypatch, fail={needle: DockerError(500, 'port is already allocated')})
    r = client.post('/api/docker/containers/web/recreate', json={})
    assert r.status_code == 500
    msg = r.get_json()['error']
    assert 'port is already allocated' in msg and 'put back and is running again' in msg
    assert e.names == {OLD_ID: 'web'} and e.running == {OLD_ID: True}  # exactly as before


def test_a_replacement_that_does_not_stay_up_is_rolled_back_with_its_log(client, monkeypatch):
    e = engine(monkeypatch, new_state={'Running': False, 'ExitCode': 1})
    r = client.post('/api/docker/containers/web/recreate', json={})
    msg = r.get_json()['error']
    assert r.status_code == 500 and 'did not stay up (exit code 1)' in msg and 'bad config' in msg
    assert e.names == {OLD_ID: 'web'} and e.running == {OLD_ID: True}


def test_a_crash_looping_replacement_counts_as_not_up(client, monkeypatch):
    e = engine(monkeypatch, new_state={'Running': True, 'Restarting': True, 'ExitCode': 137})
    assert client.post('/api/docker/containers/web/recreate', json={}).status_code == 500
    assert e.names == {OLD_ID: 'web'} and e.running == {OLD_ID: True}


def test_a_failed_stop_changes_nothing(client, monkeypatch):
    e = engine(monkeypatch, fail={'/stop': DockerError(500, 'cannot stop')})
    assert client.post('/api/docker/containers/web/recreate', json={}).status_code == 500
    assert verbs(e) == ['POST /containers/OLD/stop'] and e.names == {OLD_ID: 'web'}


def test_old_container_that_cannot_be_removed_is_a_warning_not_a_failure(client, monkeypatch):
    e = engine(monkeypatch, fail={'DELETE /containers/' + OLD_ID: DockerError(409, 'removal in progress')})
    j = client.post('/api/docker/containers/web/recreate', json={}).get_json()
    assert j['success'] and 'kept as web-replaced-' in j['warning']
    assert e.names[NEW_ID] == 'web' and e.running[NEW_ID]


# ── Refusals ───────────────────────────────────────────────────────────
def test_refusals_touch_nothing(client, monkeypatch):
    e = engine(monkeypatch, tag_image=OLD_IMG)                         # tag still points at the running image
    r = client.post('/api/docker/containers/web/recreate', json={})
    assert r.status_code == 400 and 'nothing to apply' in r.get_json()['error'] and verbs(e) == []
    e = engine(monkeypatch, tag_image=None)                            # tag not on this host
    r = client.post('/api/docker/containers/web/recreate', json={})
    assert r.status_code == 400 and 'pull it first' in r.get_json()['error'] and verbs(e) == []
    info = container()
    info['Config']['Image'] = OLD_IMG                                  # created from an id
    e = engine(monkeypatch, info)
    r = client.post('/api/docker/containers/web/recreate', json={})
    assert r.status_code == 400 and 'image id, not a tag' in r.get_json()['error'] and verbs(e) == []
    assert client.post('/api/docker/containers/bad;id/recreate', json={}).status_code == 400
    assert client.post('/api/docker/containers/web/recreate', json={'method': 'rm -rf'}).status_code == 400
    e = engine(monkeypatch)                                            # not a compose container
    r = client.post('/api/docker/containers/web/recreate', json={'method': 'compose'})
    assert r.status_code == 400 and 'Not a compose service' in r.get_json()['error'] and verbs(e) == []


def test_readonly_role_cannot_recreate(client, monkeypatch):
    e = engine(monkeypatch)
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('viewer', 'readonly'))
    assert client.post('/api/docker/containers/web/recreate', json={}).status_code == 403
    assert e.calls == []


# ── The compose path ───────────────────────────────────────────────────
def compose_container(tmp_path, readable=True):
    f = tmp_path / 'compose.yml'
    f.write_text('services: {}\n')
    info = container()
    info['Config']['Labels'].update({
        'com.docker.compose.project': 'web', 'com.docker.compose.service': 'app',
        'com.docker.compose.project.working_dir': str(tmp_path),
        'com.docker.compose.project.config_files': str(f) if readable else str(tmp_path / 'missing.yml')})
    return info, str(f)


def test_compose_service_is_recreated_by_compose_with_exact_argv(client, monkeypatch, tmp_path):
    info, f = compose_container(tmp_path)
    e = engine(monkeypatch, info)
    ran = []
    monkeypatch.setattr(dk, 'run', lambda args, **kw: ran.append((args, kw)) or ('', 'Container web  Started', 0))
    j = client.post('/api/docker/containers/web/recreate', json={}).get_json()
    assert j['success'] and j['method'] == 'compose'
    assert ran == [(['docker', 'compose', '-p', 'web', '--project-directory', str(tmp_path), '-f', f,
                     'up', '-d', '--no-deps', 'app'], {'no_sudo': True, 'timeout': 600})]
    assert verbs(e) == []                                              # compose did the work, not the clone path


def test_compose_failure_is_reported_and_clone_can_be_forced(client, monkeypatch, tmp_path):
    info, _ = compose_container(tmp_path)
    e = engine(monkeypatch, info)
    monkeypatch.setattr(dk, 'run', lambda args, **kw: ('', 'env file /x/.env not found', 1))
    r = client.post('/api/docker/containers/web/recreate', json={})
    assert r.status_code == 500 and '.env not found' in r.get_json()['error'] and verbs(e) == []
    monkeypatch.setattr(dk, 'run', lambda *a, **k: pytest.fail('clone was asked for'))
    j = client.post('/api/docker/containers/web/recreate', json={'method': 'clone'}).get_json()
    assert j['success'] and j['method'] == 'clone'
    assert e.created['Labels']['com.docker.compose.service'] == 'app'   # still the stack's container afterwards


def test_compose_stack_with_unreadable_files_falls_to_clone(client, monkeypatch, tmp_path):
    info, _ = compose_container(tmp_path, readable=False)
    engine(monkeypatch, info)
    assert dk._dk_compose_target(info['Config']['Labels']) is None
    assert client.post('/api/docker/containers/web/recreate', json={}).get_json()['method'] == 'clone'


def test_compose_target_refuses_junk_labels(tmp_path):
    info, _ = compose_container(tmp_path)
    lab = dict(info['Config']['Labels'])
    assert dk._dk_compose_target(lab)[1] == 'app'
    assert dk._dk_compose_target(dict(lab, **{'com.docker.compose.service': 'app; rm'})) is None
    assert dk._dk_compose_target(dict(lab, **{'com.docker.compose.project': '-p'})) is None
    assert dk._dk_compose_target({}) is None and dk._dk_compose_target(None) is None


# ── The UI ─────────────────────────────────────────────────────────────
def test_ui_offers_recreate_on_the_badge_and_says_how():
    import os
    js = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'docker.js')).read()
    assert 'onclick="dkRecreate(' in js and 'async function dkRecreate(id, name, via, method)' in js
    assert '/recreate`, method ? { method } : {}' in js
    assert "return dkRecreate(id, name, via, 'clone')" in js           # compose refused → offer the direct way
