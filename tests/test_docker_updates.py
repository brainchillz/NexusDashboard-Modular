"""Image update checker (3.6.0): registry-v2 reference parsing, digest
comparison against RepoDigests, the /api/docker/updates rows, and halogen's
newest-release hint. No network: the registry client is faked at its two
entry points."""
import pytest

import app
from nexusdash.modules import docker as dk, docker_registry as reg, halogen as hg
from nexusdash.modules.docker import DockerError


@pytest.mark.parametrize('ref,expect', [
    ('nginx', ('docker.io', 'library/nginx', 'latest', None)),
    ('nginx:1.25', ('docker.io', 'library/nginx', '1.25', None)),
    ('immich-app/immich-server:v1.99', ('docker.io', 'immich-app/immich-server', 'v1.99', None)),
    ('ghcr.io/peonist-ai/halogen-flash-server:0.13.8', ('ghcr.io', 'peonist-ai/halogen-flash-server', '0.13.8', None)),
    ('localhost:5000/app:dev', ('localhost:5000', 'app', 'dev', None)),
    ('registry.example.com/team/app', ('registry.example.com', 'team/app', 'latest', None)),
    ('nginx@sha256:abc', ('docker.io', 'library/nginx', None, 'sha256:abc')),
])
def test_parse_ref(ref, expect):
    assert reg.parse_ref(ref) == expect


def test_compare_and_semver():
    assert reg.compare(['nginx@sha256:aaa'], 'sha256:aaa') == 'current'
    assert reg.compare(['nginx@sha256:aaa'], 'sha256:bbb') == 'update'
    assert reg.compare([], 'sha256:bbb') == 'unknown'                 # locally built
    assert reg.compare(['nginx@sha256:aaa'], None) == 'unknown'
    assert reg.newest_semver(['0.13.0', '0.13.8', '0.9.1', 'latest', 'v0.2.0', 'sha-abc']) == '0.13.8'
    assert reg.newest_semver(['latest']) is None
    assert reg.semver_newer('0.13.8', '0.13.0') and not reg.semver_newer('0.13.0', '0.13.8')
    assert not reg.semver_newer('latest', '0.13.0')


CTS = [{'Id': 'a' * 64, 'Names': ['/web'], 'Image': 'nginx:1.25', 'State': 'running'},
       {'Id': 'b' * 64, 'Names': ['/api'], 'Image': 'ghcr.io/o/app:2.0', 'State': 'running'},
       {'Id': 'c' * 64, 'Names': ['/pinned'], 'Image': 'nginx@sha256:' + 'f' * 64, 'State': 'exited'},
       {'Id': 'd' * 64, 'Names': ['/local'], 'Image': 'mybuild:dev', 'State': 'running'},
       {'Id': 'e' * 64, 'Names': ['/byid'], 'Image': 'sha256:' + '1' * 64, 'State': 'running'}]
LOCAL = {'nginx:1.25': {'RepoDigests': ['nginx@sha256:old']},
         'ghcr.io/o/app:2.0': {'RepoDigests': ['ghcr.io/o/app@sha256:same']},
         'nginx@sha256:' + 'f' * 64: {'RepoDigests': ['nginx@sha256:' + 'f' * 64]},
         'mybuild:dev': {'RepoDigests': []}}
REMOTE = {('docker.io', 'library/nginx', '1.25'): ('sha256:new', None),
          ('ghcr.io', 'o/app', '2.0'): ('sha256:same', None)}   # mybuild:dev must never be asked for


@pytest.fixture
def fake(monkeypatch):
    def docker_request(method, path, body=None, timeout=60):
        if path.startswith('/containers/json'):
            return CTS
        if path.startswith('/images/'):
            import urllib.parse
            ref = urllib.parse.unquote(path[len('/images/'):-len('/json')])
            if ref in LOCAL:
                return LOCAL[ref]
            raise DockerError(404, 'No such image')
        raise AssertionError(path)
    monkeypatch.setattr(dk, 'docker_request', docker_request)
    monkeypatch.setattr(reg, 'remote_digest', lambda h, r, t: REMOTE[(h, r, t)])
    monkeypatch.setattr(dk, '_UPD_CACHE', {})
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


def test_updates_rows(fake):
    r = fake.get('/api/docker/updates').get_json()
    by = {x['name']: x for x in r['containers']}
    assert by['web']['status'] == 'update' and 'has moved' in by['web']['detail'] and by['web']['tag'] == '1.25'
    assert by['api']['status'] == 'current'
    assert by['pinned']['status'] == 'pinned'
    assert by['local']['status'] == 'local' and 'locally built' in by['local']['detail']
    assert by['byid']['status'] == 'unknown' and 'by id' in by['byid']['detail']
    assert r['updates'] == 1


def test_updates_cache_and_refresh(fake, monkeypatch):
    calls = []
    monkeypatch.setattr(reg, 'remote_digest', lambda h, r, t: calls.append(t) or ('sha256:x', None))
    fake.get('/api/docker/updates')
    n = len(calls)
    fake.get('/api/docker/updates')
    assert len(calls) == n                                            # served from cache
    assert fake.get('/api/docker/updates').get_json()['containers'][0]['cached'] is True
    fake.get('/api/docker/updates?refresh=1')
    assert len(calls) == 2 * n


def test_updates_degrade_without_docker(fake, monkeypatch):
    def boom(*a, **k):
        raise DockerError(502, 'Cannot reach the Docker daemon')
    monkeypatch.setattr(dk, 'docker_request', boom)
    r = fake.get('/api/docker/updates')
    assert r.status_code == 502 and 'Docker daemon' in r.get_json()['error']


def test_halogen_latest_tag_hint(monkeypatch):
    monkeypatch.setattr(reg, 'list_tags', lambda h, r: (['0.13.0', '0.13.8', 'latest'], None))
    monkeypatch.setattr(hg, '_LATEST', {})
    assert hg._latest_tag('ghcr.io/peonist-ai/halogen-flash-server:0.13.0') == ('0.13.8', None)
    monkeypatch.setattr(reg, 'list_tags', lambda h, r: pytest.fail('cached'))
    assert hg._latest_tag('ghcr.io/peonist-ai/halogen-flash-server:0.13.0')[0] == '0.13.8'
    assert hg._latest_tag('') == (None, None)
    assert reg.semver_newer('0.13.8', '0.13.0')
