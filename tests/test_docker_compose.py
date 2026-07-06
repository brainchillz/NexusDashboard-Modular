"""Compose stacks module — label-based discovery, argv building, managed-dir
confinement, and the validate-before-keep write flow. No docker needed."""
import os
import pytest

import app


_LBL = 'com.docker.compose.'


def _ct(project, service, name, state, workdir='/srv/proj', files='/srv/proj/docker-compose.yml'):
    return {'Id': 'a' * 64, 'Names': ['/' + name], 'State': state,
            'Status': 'Up 2 hours' if state == 'running' else 'Exited (0)',
            'Labels': {_LBL + 'project': project,
                       _LBL + 'service': service,
                       _LBL + 'project.working_dir': workdir,
                       _LBL + 'project.config_files': files}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


@pytest.fixture
def compose_root(tmp_path, monkeypatch):
    root = tmp_path / 'compose'
    root.mkdir()
    monkeypatch.setattr(app, 'COMPOSE_ROOT', str(root))
    return root


def test_project_name_regex():
    for ok in ('immich', 'book-db_2', 'a'):
        assert app.RE_COMPOSE_PROJECT.match(ok), ok
    for bad in ('', 'Immich', 'has space', '-lead', 'a/b', '..', 'x' * 80):
        assert not app.RE_COMPOSE_PROJECT.match(bad), bad


def test_managed_dir_confinement(compose_root):
    d = app._managed_dir('mystack')
    assert d == os.path.join(str(compose_root), 'mystack')
    assert app._managed_dir('../etc') is None
    assert app._managed_dir('a/b') is None
    assert app._managed_dir('') is None


def test_discovery_merges_adopted_and_managed(compose_root, monkeypatch):
    (compose_root / 'mystack').mkdir()
    (compose_root / 'mystack' / 'compose.yaml').write_text('services: {}\n')
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [
        _ct('immich', 'server', 'immich_server', 'running'),
        _ct('immich', 'redis', 'immich_redis', 'running'),
        _ct('immich', 'db', 'immich_postgres', 'exited'),
    ])
    stacks = {s['name']: s for s in app._discover_stacks()}
    im = stacks['immich']
    assert im['managed'] is False and im['actions_available']
    assert im['running'] == 2 and im['total'] == 3
    assert [s['service'] for s in im['services']] == ['db', 'redis', 'server']
    assert im['working_dir'] == '/srv/proj'
    assert im['config_files'] == ['/srv/proj/docker-compose.yml']
    my = stacks['mystack']
    assert my['managed'] is True and my['total'] == 0
    assert my['working_dir'] == str(compose_root / 'mystack')


def test_discovery_handles_missing_workdir_label(compose_root, monkeypatch):
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [
        _ct('old', 'web', 'old_web_1', 'running', workdir='', files='')])
    st = app._discover_stacks()[0]
    assert st['actions_available'] is False and st['config_files'] == []


def test_action_builds_compose_argv(client, compose_root, monkeypatch):
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [
        _ct('immich', 'server', 'immich_server', 'running')])
    monkeypatch.setattr(app.os.path, 'isdir', lambda p: True)
    calls = []
    def fake_run(args, input_data=None, no_sudo=False, timeout=120):
        calls.append((list(args), no_sudo, timeout))
        return '', '', 0
    monkeypatch.setattr(app, 'run', fake_run)
    r = client.post('/api/compose/immich/action', json={'action': 'up'})
    assert r.get_json()['success']
    args, no_sudo, timeout = calls[-1]
    assert args == ['docker', 'compose', '-p', 'immich',
                    '--project-directory', '/srv/proj',
                    '-f', '/srv/proj/docker-compose.yml', 'up', '-d']
    assert no_sudo is True and timeout == 600
    # down never carries -v.
    client.post('/api/compose/immich/action', json={'action': 'down'})
    assert calls[-1][0][-1] == 'down' and '-v' not in calls[-1][0]
    # Unknown action and unknown stack are refused.
    assert client.post('/api/compose/immich/action',
                       json={'action': 'nuke'}).status_code == 400
    assert client.post('/api/compose/ghost/action',
                       json={'action': 'up'}).status_code == 404


def test_create_validates_and_keeps_or_cleans(client, compose_root, monkeypatch):
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [])
    results = {'rc': 0}
    def fake_run(args, input_data=None, no_sudo=False, timeout=120):
        if 'config' in args:
            return '', 'yaml: bad' if results['rc'] else '', results['rc']
        return '', '', 0
    monkeypatch.setattr(app, 'run', fake_run)
    r = client.post('/api/compose/create',
                    json={'name': 'web', 'content': 'services: {}\n'})
    assert r.get_json()['success']
    assert (compose_root / 'web' / 'compose.yaml').read_text() == 'services: {}\n'
    # Duplicate name refused.
    assert client.post('/api/compose/create',
                       json={'name': 'web', 'content': 'x'}).status_code == 400
    # Invalid YAML: nothing kept.
    results['rc'] = 1
    r = client.post('/api/compose/create',
                    json={'name': 'bad', 'content': 'nonsense'})
    assert r.status_code == 400 and 'yaml: bad' in r.get_json()['error']
    assert not (compose_root / 'bad').exists()
    # Traversal names never touch the filesystem.
    assert client.post('/api/compose/create',
                       json={'name': '../evil', 'content': 'x'}).status_code == 400
    assert not (compose_root.parent / 'evil').exists()


def test_edit_restores_previous_on_bad_yaml(client, compose_root, monkeypatch):
    (compose_root / 'web').mkdir()
    (compose_root / 'web' / 'compose.yaml').write_text('services: {}\n')
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [])
    rc = {'v': 1}
    monkeypatch.setattr(app, 'run',
                        lambda args, **k: ('', 'boom', rc['v']) if 'config' in args
                        else ('', '', 0))
    r = client.post('/api/compose/web/file', json={'content': 'broken'})
    assert r.status_code == 400
    assert (compose_root / 'web' / 'compose.yaml').read_text() == 'services: {}\n'
    rc['v'] = 0
    r = client.post('/api/compose/web/file', json={'content': 'services: {x: {}}\n'})
    assert r.get_json()['success']
    assert (compose_root / 'web' / 'compose.yaml').read_text() == 'services: {x: {}}\n'


def test_edit_refuses_adopted_stack(client, compose_root, monkeypatch):
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [
        _ct('immich', 'server', 'immich_server', 'running')])
    r = client.post('/api/compose/immich/file', json={'content': 'x'})
    assert r.status_code == 400
    assert 'outside the dashboard' in r.get_json()['error']


def test_delete_only_managed_and_down(client, compose_root, monkeypatch):
    (compose_root / 'web').mkdir()
    (compose_root / 'web' / 'compose.yaml').write_text('services: {}\n')
    # A running container in the stack blocks delete.
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [
        _ct('web', 'app', 'web-app-1', 'running',
            workdir=str(compose_root / 'web'))])
    r = client.post('/api/compose/web/delete', json={})
    assert r.status_code == 400 and 'running' in r.get_json()['error']
    # Adopted stacks cannot be deleted from here.
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [
        _ct('immich', 'server', 'immich_server', 'exited')])
    assert client.post('/api/compose/immich/delete', json={}).status_code == 400
    # Managed and fully down: gone.
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [])
    r = client.post('/api/compose/web/delete', json={})
    assert r.get_json()['success']
    assert not (compose_root / 'web').exists()


def test_file_get_reports_editability(client, compose_root, monkeypatch):
    (compose_root / 'web').mkdir()
    (compose_root / 'web' / 'compose.yaml').write_text('services: {}\n')
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [])
    j = client.get('/api/compose/web/file').get_json()
    assert j['editable'] is True and j['content'] == 'services: {}\n'
    # Unreadable adopted file -> 403, not a crash.
    monkeypatch.setattr(app, 'docker_request', lambda *a, **k: [
        _ct('immich', 'server', 'immich_server', 'running',
            files='/root/immich/docker-compose.yml')])
    assert client.get('/api/compose/immich/file').status_code == 403


def test_compose_module_registered():
    assert 'compose' in app.MODULE_IDS
    assert app._DESCRIPTORS['compose']['category'] == 'Docker'
