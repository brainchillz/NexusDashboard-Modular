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
INSPECT = {}                                                         # 12-char container id -> Config.Image


@pytest.fixture
def fake(monkeypatch):
    def docker_request(method, path, body=None, timeout=60):
        if path.startswith('/containers/json'):
            return CTS
        if path.startswith('/containers/'):                           # inspect, asked only for by-id rows
            cid = path.split('/')[2]
            if cid in INSPECT:
                return {'Config': {'Image': INSPECT[cid]}}
            raise DockerError(404, 'No such container')
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


# ── Pull from the update badge (reported on the docker host, 2026-10-01) ──
def test_update_badge_pull_posts_the_key_the_route_reads():
    """dkPullFor sent {ref}; the route reads `reference`, so every badge Pull
    answered 'Invalid image reference' for a perfectly good image."""
    import os
    import re
    js = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'docker.js')).read()
    bodies = re.findall(r"API\.post\('/api/docker/images/pull',\s*\{([^}]*)\}", js)
    assert len(bodies) == 2                                           # Images tab + update badge
    assert all(re.match(r'\s*reference\s*:', b) for b in bodies), bodies


@pytest.mark.parametrize('ref,sent', [
    ('searxng/searxng', 'searxng/searxng:latest'),                    # name-only = EVERY tag at the API
    ('nginx', 'nginx:latest'),
    ('localhost:5000/app', 'localhost:5000/app:latest'),              # the port colon is not a tag
    ('nginx:1.25', 'nginx:1.25'),
    ('ghcr.io/o/app:2.0', 'ghcr.io/o/app:2.0'),
    ('nginx@sha256:' + 'f' * 64, 'nginx@sha256:' + 'f' * 64),
])
def test_pull_never_sends_a_name_only_reference(fake, monkeypatch, ref, sent):
    calls = []
    monkeypatch.setattr(dk, 'docker_raw', lambda m, p, body=None, timeout=60: calls.append(p) or (200, b'{"status":"ok"}'))
    r = fake.post('/api/docker/images/pull', json={'reference': ref})
    assert r.status_code == 200, r.get_json()
    import urllib.parse
    assert urllib.parse.parse_qs(calls[0].split('?', 1)[1]) == {'fromImage': [sent]}


def test_pull_without_a_reference_is_refused(fake, monkeypatch):
    monkeypatch.setattr(dk, 'docker_raw', lambda *a, **k: pytest.fail('must not reach the daemon'))
    assert fake.post('/api/docker/images/pull', json={'ref': 'nginx:1.25'}).status_code == 400


# ── After a pull: the tag moved, the container still runs the old image ──
def _moved(monkeypatch, cid, config_image, tag_local, remote=None):
    """One container listed by image ID (what /containers/json shows once
    its tag points elsewhere) whose config still names `config_image`."""
    import sys
    me = sys.modules[__name__]
    old_id = 'sha256:' + '0' * 64
    monkeypatch.setattr(me, 'CTS', [{'Id': cid * 64, 'Names': ['/searxng'], 'Image': old_id,
                                     'ImageID': old_id, 'State': 'running'}])
    monkeypatch.setattr(me, 'INSPECT', {cid * 12: config_image})
    monkeypatch.setattr(me, 'LOCAL', {config_image: tag_local} if tag_local else {})
    monkeypatch.setattr(me, 'REMOTE', remote or {})
    return old_id


def test_pulled_but_not_recreated_says_so(fake, monkeypatch):
    """Real case, 2026-10-01: after Pull the badge went gray 'unknown —
    image referenced by id' because the running image had lost its tag."""
    _moved(monkeypatch, 'a', 'searxng/searxng:latest',
           {'Id': 'sha256:' + '9' * 64, 'RepoDigests': ['searxng/searxng@sha256:new']},
           {('docker.io', 'searxng/searxng', 'latest'): ('sha256:new', None)})
    r = fake.get('/api/docker/updates').get_json()
    row = r['containers'][0]
    assert row['status'] == 'recreate' and row['origin'] == 'pulled'
    assert row['image'] == 'searxng/searxng:latest'                   # the tag, not the bare id
    assert 'recreate the container' in row['detail'] and 'restart keeps the old' in row['detail']
    assert r['recreate'] == 1 and r['updates'] == 0


def test_rebuilt_local_image_is_recreate_without_asking_a_registry(fake, monkeypatch):
    _moved(monkeypatch, 'b', 'mybuild:dev', {'Id': 'sha256:' + '9' * 64, 'RepoDigests': []})
    row = fake.get('/api/docker/updates').get_json()['containers'][0]  # REMOTE is empty: a lookup would KeyError
    assert row['status'] == 'recreate' and row['origin'] == 'built' and 'rebuilt' in row['detail']


def test_tag_moved_again_at_the_registry_is_still_an_update_and_pulls_the_tag(fake, monkeypatch):
    _moved(monkeypatch, 'c', 'nginx:1.25', {'Id': 'sha256:' + '9' * 64, 'RepoDigests': ['nginx@sha256:mid']},
           {('docker.io', 'library/nginx', '1.25'): ('sha256:newest', None)})
    r = fake.get('/api/docker/updates').get_json()
    assert r['containers'][0]['status'] == 'update' and r['containers'][0]['image'] == 'nginx:1.25'
    assert r['updates'] == 1 and r['recreate'] == 0


def test_registry_down_does_not_hide_a_pending_recreate(fake, monkeypatch):
    _moved(monkeypatch, 'd', 'nginx:1.25', {'Id': 'sha256:' + '9' * 64, 'RepoDigests': ['nginx@sha256:mid']},
           {('docker.io', 'library/nginx', '1.25'): (None, 'HTTP 503 from docker.io')})
    assert fake.get('/api/docker/updates').get_json()['containers'][0]['status'] == 'recreate'


def test_by_id_rows_that_are_not_a_moved_tag_stay_unknown(fake, monkeypatch):
    # the tag is gone from the host
    _moved(monkeypatch, 'e', 'nginx:1.25', None, {('docker.io', 'library/nginx', '1.25'): ('sha256:x', None)})
    row = fake.get('/api/docker/updates').get_json()['containers'][0]
    assert row['status'] == 'unknown' and 'no longer on this host' in row['detail']
    # created from a short id: the config "ref" resolves to the image it runs
    old_id = _moved(monkeypatch, 'f', '0bc29cbce145', None)
    import sys
    monkeypatch.setattr(sys.modules[__name__], 'LOCAL', {'0bc29cbce145': {'Id': old_id, 'RepoDigests': []}})
    row = fake.get('/api/docker/updates').get_json()['containers'][0]
    assert row['status'] == 'unknown' and 'by id' in row['detail'] and row['image'] == old_id


def test_badge_draws_recreate_and_pulls_the_rows_reference():
    import os
    js = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'docker.js')).read()
    assert "r.status === 'recreate'" in js and 'recreate to apply' in js
    assert "dkPullFor('${jsArg(r.image || c.image)}')" in js          # a by-id c.image cannot be pulled
    assert 'restart or recreate' not in js                            # a restart keeps the old image
