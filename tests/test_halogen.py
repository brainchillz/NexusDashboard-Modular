"""Halogen module (3.5.0): the halogen-flash-server compose stack — discovery
by compose label, /health + /metrics parsing, compose actions, the boot
(restart-policy) toggle, logs, and the summary/alerts/history/metrics hooks.

Fixtures are the REAL 0.13.0 stack on node4 (2026-09-25): `docker inspect`
of both containers, /health, /metrics, /cache and the engine's startup log.
Docker and HTTP are faked at the module boundary; nothing here needs a
socket, a GPU or the server.
"""
import os
import json
import pytest

import app
from nexusdash.core import registry
from nexusdash.modules import halogen as hg
from nexusdash.modules.docker import DockerError

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'halogen')


def _read(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


INSPECT = json.loads(_read('inspect-0.13.0.json'))          # [engine, api]
HEALTH = _read('health-0.13.0.json')
METRICS = _read('metrics-0.13.0.txt')
CACHE = _read('cache-0.13.0.json')
ENGINE_LOG = _read('engine-startup-0.13.0.txt')
BY_ID = {c['Id']: c for c in INSPECT}


def _fake_docker(docs=INSPECT, updates=None):
    """docker_request keyed on path: the label-filtered list, per-id inspect,
    and /update (recorded)."""
    def fake(method, path, body=None, timeout=60):
        if method == 'GET' and path.startswith('/containers/json?all=1&filters='):
            assert 'com.docker.compose.project%3Dhalogen' in path or 'com.docker.compose.project=halogen' in path
            return [{'Id': c['Id']} for c in docs]
        if method == 'GET' and path.endswith('/json'):
            cid = path.split('/')[2]
            for c in docs:
                if c['Id'].startswith(cid):
                    return c
            raise DockerError(404, 'No such container')
        if method == 'POST' and path.endswith('/update'):
            (updates if updates is not None else []).append((path.split('/')[2], body))
            return {}
        raise AssertionError('unexpected docker call %s %s' % (method, path))
    return fake


def _fake_http(health=HEALTH, metrics=METRICS, cache=CACHE, health_err=None):
    def fake(base, path, timeout):
        assert base == 'http://127.0.0.1:8731'
        if path == '/metrics':
            return (metrics, None) if metrics else (None, 'connection refused')
        if path == '/health':
            return (None, health_err) if health_err else (health, None)
        if path == '/cache':
            return (cache, None)
        raise AssertionError(path)
    return fake


def _stopped(doc, state='exited'):
    d = json.loads(json.dumps(doc))
    d['State'].update({'Status': state, 'Running': False, 'Health': {'Status': ''}})
    return d


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(hg, 'HALOGEN_STATE_FILE', str(tmp_path / 'halogen.json'))
    monkeypatch.setattr(hg, 'HALOGEN_DIR', '')
    monkeypatch.setattr(hg, 'HALOGEN_URL', '')
    monkeypatch.setattr(hg, 'docker_request', _fake_docker())
    monkeypatch.setattr(hg, '_http_get', _fake_http())
    monkeypatch.setattr(hg, 'docker_raw', lambda *a, **k: (200, ENGINE_LOG.encode()))
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── Pure parsers against the real captures ───────────────────────────

def test_metrics_capture_parses():
    m = hg._metrics_summary(hg._parse_metrics(METRICS))
    assert m['tps'] == pytest.approx(40.2949)
    assert m['requests_total'] == 9
    assert m['requests_processing'] == 0 and m['requests_deferred'] == 0
    assert 0 < m['draft_accept_ratio'] < 1
    assert m['tokens_predicted_total'] > 0
    assert hg._metrics_summary({}) is None


def test_health_capture_parses():
    h = hg._health_summary(json.loads(HEALTH))
    assert (h['api_version'], h['engine_version'], h['version_match']) == ('0.13.0', '0.13.0', True)
    assert h['model'] == 'halogen-qwen3.8-flash-next'
    assert (h['context'], h['slots'], h['kv_pool_positions']) == (262144, 4, 524288)
    assert h['reasoning_effort_default'] == 'xhigh'
    assert set(h['reasoning_effort_values']) == {'high', 'low', 'medium', 'minimal', 'none', 'xhigh'}
    assert h['vision'] is True and h['engine_responds'] is True
    assert hg._health_summary('AMDSMI_STATUS_NOT_SUPPORTED') is None       # gpu.py precedent


def test_budget_lines_are_the_engines_own_arithmetic():
    lines = hg._budget_lines(ENGINE_LOG)
    assert lines[0].startswith('halogen: halogen-flash-server 0.13.0, mode engine')
    assert any(l.startswith('halogen: KV budget') for l in lines)
    assert any(l.startswith('kv pool: host RAM') for l in lines)
    assert any(l.startswith('flash_serve: listening') for l in lines)
    assert len(lines) <= 15
    assert not any(l[:4].isdigit() and 'T' in l[:20] for l in lines)     # timestamps stripped
    assert hg._budget_lines('') == []


def test_budget_lines_come_from_the_last_start_only():
    two = ENGINE_LOG + '\n2026-09-25T01:02:03.000000000Z halogen: halogen-flash-server 0.13.0, mode engine\n' \
          '2026-09-25T01:02:04.000000000Z halogen: KV budget SECOND RUN\n'
    lines = hg._budget_lines(two)
    assert lines == ['halogen: halogen-flash-server 0.13.0, mode engine', 'halogen: KV budget SECOND RUN']
    # An image without the marker line still yields the budget lines.
    old = '\n'.join(l for l in ENGINE_LOG.splitlines() if 'mode engine' not in l)
    assert any(l.startswith('halogen: KV budget') for l in hg._budget_lines(old))


def test_container_summary_from_inspect():
    eng, api = (hg._container_summary(c) for c in INSPECT)
    assert (eng['name'], eng['service'], eng['tag']) == ('halogen-engine', 'engine', '0.13.0')
    assert eng['health'] == 'healthy' and eng['running'] and eng['restart_policy'] == 'unless-stopped'
    assert eng['working_dir'] == '/srv/halogen'
    assert eng['config_files'] == ['/srv/halogen/docker-compose.yml']
    assert api['published'] == {'8731/tcp': 8731}
    assert eng['published'] == {}                                           # engine port is NOT published


# ─── GET /api/halogen ─────────────────────────────────────────────────

def test_status_full(client, tmp_path):
    st = client.get('/api/halogen').get_json()
    assert st['reachable'] and st['deployed'] and st['running'] and st['healthy']
    assert st['api_url'] == 'http://127.0.0.1:8731'
    assert st['working_dir'] == '/srv/halogen' and st['image_tag'] == '0.13.0'
    assert st['boot_enabled'] is True and st['tags_match'] is True
    assert st['health']['version_match'] is True
    assert st['metrics']['tps'] == pytest.approx(40.2949)
    assert st['cache']['hit_rate'] is not None
    assert any('KV budget' in l for l in st['budget'])
    assert set(st['containers']) == {'engine', 'api'}
    # The stack dir is remembered so `up` works after a `down`.
    assert json.load(open(tmp_path / 'halogen.json'))['working_dir'] == '/srv/halogen'


def test_status_degrades_without_docker(client, monkeypatch):
    def boom(*a, **k):
        raise DockerError(502, 'Cannot reach the Docker daemon')
    monkeypatch.setattr(hg, 'docker_request', boom)
    r = client.get('/api/halogen')
    assert r.status_code == 200
    st = r.get_json()
    assert st['reachable'] is False and st['deployed'] is False and 'Docker daemon' in st['error']


def test_status_not_deployed(client, monkeypatch):
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[]))
    st = client.get('/api/halogen').get_json()
    assert st['reachable'] is True and st['deployed'] is False and st['containers'] == {}


def test_status_health_failure_is_reported_not_raised(client, monkeypatch):
    monkeypatch.setattr(hg, '_http_get', _fake_http(health_err='connection refused'))
    st = client.get('/api/halogen').get_json()
    assert st['health'] is None and st['health_error'] == 'connection refused'
    assert st['metrics']['tps'] > 0                                         # metrics still came through


def test_status_stopped_stack_skips_http(client, monkeypatch):
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[_stopped(INSPECT[0]), _stopped(INSPECT[1])]))
    monkeypatch.setattr(hg, '_http_get', lambda *a: pytest.fail('must not probe a stopped stack'))
    st = client.get('/api/halogen').get_json()
    assert st['deployed'] and not st['running'] and 'metrics' not in st


# ─── Actions ──────────────────────────────────────────────────────────

def _stack_dir(tmp_path, hg_mod, monkeypatch):
    d = tmp_path / 'srv-halogen'
    d.mkdir()
    (d / 'docker-compose.yml').write_text('services: {}\n')
    monkeypatch.setattr(hg_mod, 'HALOGEN_DIR', str(d))
    return str(d)


@pytest.mark.parametrize('action,sub,timeout', [
    ('start', ['start'], 300), ('stop', ['stop'], 300), ('restart', ['restart'], 600),
    ('restart-api', ['restart', 'api'], 120), ('up', ['up', '-d'], 900), ('down', ['down'], 300)])
def test_action_argv_is_exact(client, monkeypatch, tmp_path, action, sub, timeout):
    wd = _stack_dir(tmp_path, hg, monkeypatch)
    calls = []
    monkeypatch.setattr(hg, 'run', lambda argv, **kw: calls.append((list(argv), kw)) or ('', '', 0))
    r = client.post('/api/halogen/action', json={'action': action})
    assert r.status_code == 200 and r.get_json()['success']
    argv, kw = calls[0]
    assert argv == ['docker', 'compose', '-p', 'halogen', '--project-directory', wd,
                    '-f', os.path.join(wd, 'docker-compose.yml')] + sub
    assert kw == {'no_sudo': True, 'timeout': timeout}


def test_action_refuses_junk(client, monkeypatch):
    monkeypatch.setattr(hg, 'run', lambda *a, **k: pytest.fail('must not run'))
    assert client.post('/api/halogen/action', json={'action': 'down -v'}).status_code == 400
    assert client.post('/api/halogen/action', json={'action': 'rm'}).status_code == 400
    assert client.post('/api/halogen/action', json=None).status_code == 400


def test_action_needs_a_known_stack_dir(client, monkeypatch, tmp_path):
    monkeypatch.setattr(hg, 'run', lambda *a, **k: pytest.fail('must not run'))
    r = client.post('/api/halogen/action', json={'action': 'up'})       # nothing remembered yet
    assert r.status_code == 400 and 'Stack directory unknown' in r.get_json()['error']
    # Once discovery has seen the stack it is remembered — but the dir must exist.
    client.get('/api/halogen')
    assert hg._load_state()['working_dir'] == '/srv/halogen'
    r = client.post('/api/halogen/action', json={'action': 'up'})
    assert r.status_code == 400 and ('unknown' in r.get_json()['error'].lower() or 'No compose file' in r.get_json()['error'])


def test_action_relays_compose_failure(client, monkeypatch, tmp_path):
    _stack_dir(tmp_path, hg, monkeypatch)
    monkeypatch.setattr(hg, 'run', lambda *a, **k: ('', 'Error response from daemon: OCI runtime create failed', 1))
    r = client.post('/api/halogen/action', json={'action': 'up'})
    assert r.status_code == 400 and 'OCI runtime' in r.get_json()['error']


def test_actions_are_admin_only(client, monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('viewer', 'readonly'))
    monkeypatch.setattr(hg, 'run', lambda *a, **k: pytest.fail('must not run'))
    assert client.post('/api/halogen/action', json={'action': 'stop'}).status_code == 403
    assert client.post('/api/halogen/boot', json={'enabled': False}).status_code == 403
    assert client.get('/api/halogen').status_code == 200                    # reads stay open


# ─── Boot toggle ──────────────────────────────────────────────────────

def test_boot_toggle_updates_both_containers(client, monkeypatch):
    updates = []
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(updates=updates))
    r = client.post('/api/halogen/boot', json={'enabled': False})
    assert r.get_json() == {'success': True, 'boot_enabled': False, 'policy': 'no'}
    assert len(updates) == 2
    assert {u[1]['RestartPolicy']['Name'] for u in updates} == {'no'}
    assert {u[0] for u in updates} == {c['Id'][:12] for c in INSPECT}
    updates.clear()
    client.post('/api/halogen/boot', json={'enabled': True})
    assert {u[1]['RestartPolicy']['Name'] for u in updates} == {'unless-stopped'}


def test_boot_toggle_validates(client, monkeypatch):
    assert client.post('/api/halogen/boot', json={'enabled': 'yes'}).status_code == 400
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[]))
    r = client.post('/api/halogen/boot', json={'enabled': True})
    assert r.status_code == 400 and 'not deployed' in r.get_json()['error']


# ─── Logs ─────────────────────────────────────────────────────────────

def test_logs_demux_and_clamp(client, monkeypatch):
    seen = {}
    frame = b'\x01\x00\x00\x00' + (5).to_bytes(4, 'big') + b'hello'
    monkeypatch.setattr(hg, 'docker_raw', lambda m, p, **k: seen.update(path=p) or (200, frame))
    r = client.get('/api/halogen/logs?service=api&tail=99999').get_json()
    assert r['logs'] == 'hello' and r['container'] == 'halogen-api'
    assert 'tail=5000' in seen['path'] and seen['path'].startswith('/containers/%s/logs' % INSPECT[1]['Id'][:12])
    assert client.get('/api/halogen/logs?service=db').status_code == 400


# ─── Hooks ────────────────────────────────────────────────────────────

def test_summary_block(client):
    s = hg._halogen_summary()
    assert s['deployed'] and s['running'] and s['healthy']
    assert s['image_tag'] == '0.13.0' and s['boot_enabled'] is True
    assert s['engine'] == 'healthy' and s['api'] == 'healthy'
    assert s['tps'] == pytest.approx(40.2949) and s['requests_total'] == 9
    assert '/api/summary' and client.get('/api/summary').get_json()['halogen']['running'] is True


def test_summary_absent_when_not_deployed(client, monkeypatch):
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[]))
    assert hg._halogen_summary() is None
    assert 'halogen' not in client.get('/api/summary').get_json()


def test_alerts_quiet_on_a_healthy_stack(client):
    assert hg._halogen_alerts() == []


def test_alerts_api_down(client, monkeypatch):
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[INSPECT[0], _stopped(INSPECT[1])]))
    keys = {a['key']: a['message'] for a in hg._halogen_alerts()}
    assert keys == {'halogen_down:api': 'Halogen api container is exited'}


def test_alerts_engine_unhealthy_and_missing(client, monkeypatch):
    sick = json.loads(json.dumps(INSPECT[0]))
    sick['State']['Health']['Status'] = 'unhealthy'
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[sick]))
    keys = {a['key'] for a in hg._halogen_alerts()}
    assert keys == {'halogen_unhealthy:engine', 'halogen_missing:api'}


def test_alerts_boot_off_and_queue_and_tag_drift(client, monkeypatch):
    eng = json.loads(json.dumps(INSPECT[0]))
    api = json.loads(json.dumps(INSPECT[1]))
    for d in (eng, api):
        d['HostConfig']['RestartPolicy']['Name'] = 'no'
    api['Config']['Image'] = 'ghcr.io/peonist-ai/halogen-flash-server:0.12.0'
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[eng, api]))
    busy = METRICS.replace('llamacpp:requests_deferred 0', 'llamacpp:requests_deferred 3') \
                  .replace('llamacpp:requests_processing 0', 'llamacpp:requests_processing 4')
    monkeypatch.setattr(hg, '_http_get', _fake_http(metrics=busy))
    keys = {a['key']: a['message'] for a in hg._halogen_alerts()}
    assert set(keys) == {'halogen_boot_off', 'halogen_queue', 'halogen_tag_drift'}
    assert '3 request(s) queued behind 4' in keys['halogen_queue']
    assert 'will not survive a reboot' in keys['halogen_boot_off']


def test_history_rows_are_registered_metrics(client):
    rows = hg._halogen_history()
    assert {r[0] for r in rows} == hg.HALOGEN_HISTORY_METRICS <= app.HISTORY_METRICS
    assert {r[1] for r in rows} == {'halogen'}
    by = {r[0]: r[2] for r in rows}
    assert by['halogen_tps'] == pytest.approx(40.2949) and by['halogen_requests'] == 9
    assert 0 < by['halogen_draft_accept'] < 100


def test_history_empty_when_stopped(client, monkeypatch):
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[_stopped(INSPECT[0]), _stopped(INSPECT[1])]))
    assert hg._halogen_history() == []


def test_metrics_lines(client, monkeypatch):
    lines = hg._halogen_metrics()
    assert 'storagedash_halogen_up 1' in lines
    assert any(l.startswith('storagedash_halogen_predicted_tokens_seconds 40.29') for l in lines)
    assert sum(l.startswith('# HELP') for l in lines) == sum(l.startswith('# TYPE') for l in lines)
    monkeypatch.setattr(hg, 'docker_request', _fake_docker(docs=[]))
    assert hg._halogen_metrics() == []


# ─── Registration ─────────────────────────────────────────────────────

def test_module_is_default_off_and_gated(client, monkeypatch):
    assert 'halogen' in registry.DEFAULT_OFF
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'halogen'})
    r = client.get('/api/halogen')
    assert r.status_code == 403 and "module 'halogen' is disabled" in r.get_json()['error']


def test_frontend_is_wired():
    idx = open('templates/index.html').read()
    js = open('static/js/halogen.js').read()
    assert '/static/js/halogen.js' in idx
    assert 'function page_halogen(' in js and 'function dashcard_halogen(' in js
    assert 'levelBar' not in js or 'usageBar' not in js      # no consumption bar drawn for a level
