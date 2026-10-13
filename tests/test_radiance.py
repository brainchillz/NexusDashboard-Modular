"""Radiance module (3.9.0): the vLLM Radiance systemd unit — `systemctl show`
/ unit-file / drop-in parsing, the vllm:* metrics scrape, the engine's own
startup lines, the typed knob allowlist, the drop-in writer (root path and
helper path), unit actions, logs, and the summary/alerts/history/metrics
hooks.

Fixtures are the REAL unit on node7 (2026-10-13, 1x R9700, TP=1): `systemctl
show`, the unit file, /metrics, /v1/models, /version and the container's
startup log. systemctl, HTTP and the docker socket are faked at the module
boundary; the drop-in is written to a temp systemd dir.
"""
import os
import json
import stat
import pytest

import app
from nexusdash.modules import radiance as rd

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'radiance')


def _read(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


SHOW = _read('systemctl-show.txt')
UNIT = _read('vllm-qwen.service')
METRICS = _read('metrics.txt')
MODELS = _read('models.json')
VERSION = _read('version.json')
STARTUP = _read('startup-log.txt')
UNIT_PATH = '/etc/systemd/system/vllm-qwen.service'
CONFLICT_SHOW = 'Id=llama-server.service\nLoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n'


def _show_text(**over):
    """The capture with some properties replaced."""
    lines = []
    for line in SHOW.splitlines():
        k = line.split('=', 1)[0]
        if k in over:
            lines.append('%s=%s' % (k, over.pop(k)))
        else:
            lines.append(line)
    for k, v in over.items():
        lines.append('%s=%s' % (k, v))
    return '\n'.join(lines) + '\n'


class Fake:
    """Records every `run` / `run_safe` argv; answers systemctl show, HTTP
    and the docker log from the captures."""
    def __init__(self, show=SHOW, conflict=CONFLICT_SHOW, metrics=METRICS, log=STARTUP):
        self.show, self.conflict, self.metrics, self.log = show, conflict, metrics, log
        self.calls = []
        self.inputs = []

    def run(self, args, input_data=None, no_sudo=False, timeout=120):
        self.calls.append(list(args))
        self.inputs.append(input_data)
        if args[:2] == ['systemctl', 'show']:
            if args[2] == 'vllm-qwen.service':
                return self.show, '', 0
            if args[2] == 'llama-server.service':
                return (self.conflict, '', 0) if self.conflict else ('', 'not found', 1)
        if args == ['systemctl', 'daemon-reload']:
            return '', '', 0
        if args[0] == 'journalctl':
            return 'Oct 13 01:00:00 node7 serve-tp1.sh[1]: [run] docker …\n', '', 0
        if args[0].endswith('-radiance'):
            return '', '', 0
        raise AssertionError('unexpected run %r' % (args,))

    def run_safe(self, args, input_data=None):
        self.calls.append(list(args))
        return {'success': True, 'stdout': '', 'stderr': '', 'returncode': 0}

    def http(self, base, path, timeout):
        assert base == 'http://127.0.0.1:8080', base
        if path == '/metrics':
            return (self.metrics, None) if self.metrics else (None, 'connection refused')
        if path == '/version':
            return VERSION, None
        if path == '/v1/models':
            return MODELS, None
        raise AssertionError(path)

    def docker_raw(self, method, path, body=None, timeout=60):
        assert path.startswith('/containers/vllmnvfp4/logs?'), path
        # docker's log stream is 8-byte-framed; _dk_demux_logs undoes it.
        payload = self.log.encode()
        frame = b'\x01\x00\x00\x00' + len(payload).to_bytes(4, 'big') + payload
        return 200, frame


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = Fake()
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(rd, 'run', f.run)
    monkeypatch.setattr(rd, 'run_safe', f.run_safe)
    monkeypatch.setattr(rd, '_http_get', f.http)
    monkeypatch.setattr(rd, 'docker_raw', f.docker_raw)
    monkeypatch.setattr(rd, 'SYSTEMD_DIR', str(tmp_path / 'systemd'))
    monkeypatch.setattr(rd, 'RADIANCE_URL', '')
    monkeypatch.setattr(rd, 'RADIANCE_PREV_FILE', str(tmp_path / 'radiance_prev.json'))
    monkeypatch.setattr(rd.os, 'geteuid', lambda: 0)
    real_read = rd._read_text
    monkeypatch.setattr(rd, '_read_text', lambda p: UNIT if p == UNIT_PATH else real_read(p))
    monkeypatch.setattr(rd, '_models_available', lambda env: ['/home/backupuser/models/Qwen3.8-27B-Uncensored-NVFP4'])
    f.tmp = tmp_path
    f.dropin = tmp_path / 'systemd' / 'vllm-qwen.service.d' / '50-nexus-dashboard.conf'
    app.app.config['TESTING'] = True
    f.client = app.app.test_client()
    return f


# ─── Pure parsers against the real captures ───────────────────────────

def test_show_capture_parses():
    s = rd.parse_show(SHOW)
    assert s['ActiveState'] == 'active' and s['UnitFileState'] == 'enabled'
    assert s['FragmentPath'] == UNIT_PATH and s['DropInPaths'] == '' and s['NRestarts'] == '0'
    env = rd.parse_env_string(s['Environment'])
    assert env['PORT'] == '8080' and env['NAME'] == 'vllmnvfp4' and env['KV_MEM'] == '6539000000'
    # the quoted multi-word value survives shlex intact
    assert env['SERVED_NAMES'] == 'Qwen3.8 Qwen3.6 Qwen3.8-Uncensored Qwen3.8-27B'


def test_unit_file_env_parses():
    env = rd.parse_unit_env(UNIT)
    assert env['SNAP'].endswith('/models/Qwen3.8-27B-Uncensored-NVFP4')
    assert env['MAXSEQS'] == '3' and env['MAXLEN'] == '65536' and env['SPEC_METHOD'] == 'dflash'
    assert env['SERVED_NAMES'].split() == ['Qwen3.8', 'Qwen3.6', 'Qwen3.8-Uncensored', 'Qwen3.8-27B']
    assert 'TP' not in env            # serve-tp1.sh pins it, the unit does not
    # an empty Environment= clears what came before, as systemd does
    assert rd.parse_unit_env('[Service]\nEnvironment=A=1\nEnvironment=\nEnvironment=B=2\n') == {'B': '2'}


def test_metrics_capture_parses():
    sums, model = rd.parse_metrics(METRICS)
    assert model == 'Qwen3.8'
    assert sums['vllm:num_requests_running'] == 0 and sums['vllm:num_requests_waiting'] == 0
    assert sums['vllm:generation_tokens_total'] == 6677 and sums['vllm:prompt_tokens_total'] == 51800
    assert sums['vllm:request_success_total'] == 24          # summed over finished_reason
    assert 'vllm:time_to_first_token_seconds_bucket' not in sums
    m = rd.metrics_summary(sums, model)
    assert m['requests_total'] == 24 and m['kv_cache_pct'] == 0.0
    assert m['draft_accept_ratio'] == round(4786 / 13244, 3)
    assert m['model'] == 'Qwen3.8'
    assert rd.metrics_summary({}) is None


def test_models_and_startup_capture_parse():
    models = rd.parse_models(MODELS)
    assert [m['id'] for m in models] == ['Qwen3.8', 'Qwen3.6', 'Qwen3.8-Uncensored', 'Qwen3.8-27B']
    assert models[0]['max_model_len'] == 65536
    su = rd.startup_summary(STARTUP)
    assert su['ready'] and su['errors'] == 0
    assert su['kv_tokens'] == 140700 and su['max_concurrency'] == 2.15 and su['concurrency_at'] == 65536
    assert su['weights_gib'] == 19.95 and su['init_s'] == 25.32 and su['load_s'] == 32.431876
    assert any(l.startswith('GPU KV cache size') for l in su['lines'])
    assert not any('EngineCore pid' in l for l in su['lines'])      # prefix stripped
    # anchored on the LAST start: an older run's figures must not leak in
    older = STARTUP.replace('140,700', '999,999') + STARTUP
    assert rd.startup_summary(older)['kv_tokens'] == 140700
    assert rd.startup_summary('')['ready'] is False


def test_render_dropin_is_deterministic_and_quotes_spaces():
    text = rd.render_dropin({'TP': '2', 'SERVED_NAMES': 'A B', 'KV_MEM': 'auto'})
    assert text.endswith('[Service]\nEnvironment=KV_MEM=auto\nEnvironment="SERVED_NAMES=A B"\nEnvironment=TP=2\n')
    assert rd.parse_unit_env(text) == {'TP': '2', 'SERVED_NAMES': 'A B', 'KV_MEM': 'auto'}


# ─── The allowlist ────────────────────────────────────────────────────

@pytest.mark.parametrize('key,value,ok', [
    ('TP', '2', True), ('TP', '4', True), ('TP', '0', False), ('TP', '9', False), ('TP', 'x', False),
    ('GPUS', '0,1', True), ('GPUS', '0', True), ('GPUS', '0;1', False), ('GPUS', '0, 1', False),
    ('MAXLEN', '65536', True), ('MAXLEN', '512', False), ('MAXSEQS', '8', True), ('MAXSEQS', '0', False),
    ('SPEC_METHOD', 'mtp', True), ('SPEC_METHOD', 'eagle', False),
    ('GPU_UTIL', '0.97', True), ('GPU_UTIL', '1.5', False), ('GPU_UTIL', '0.97;x', False),
    ('KV_MEM', 'auto', True), ('KV_MEM', '0', True), ('KV_MEM', '6539000000', True), ('KV_MEM', '6.5e9', False),
    ('SNAP', '/home/backupuser/models/Qwen3.8-27B-PARO-int5', True), ('SNAP', 'models/x', False),
    ('SNAP', '/home/x/../etc', False), ('SNAP', '/home/$HOME', False), ('SNAP', '/a b', False),
    ('SERVED_NAMES', 'Qwen3.8 Qwen3.6', True), ('SERVED_NAMES', 'A;B', False), ('SERVED_NAMES', 'A  B', False),
    ('IMAGE', 'stilldeadcode/vllm-radiance:0.9.4', True), ('IMAGE', 'x y', False),
    ('NAME', 'vllmtp2', True), ('NAME', '-bad', False), ('PORT', '8081', True), ('PORT', '80', False),
    ('RUNTIME', 'podman', True), ('RUNTIME', 'nerdctl', False),
    ('RADIANCE_DYNAMIC_WIDTH', '0', True), ('RADIANCE_AR_QBITS', '5', True), ('RADIANCE_X', 'a b', False),
    ('RADIANCE_X', '$(id)', False), ('radiance_x', '1', False),
    ('EXTRA', '--foo', False), ('ExecStart', '/bin/sh', False), ('PATH', '/x', False),
])
def test_validate_knob(key, value, ok):
    nv, e = rd.validate_knob(key, value)
    assert (e is None) == ok, (key, value, e)
    if ok:
        assert nv == value


def test_validate_knob_rejects_non_strings_and_empty():
    assert rd.validate_knob('TP', 2)[1]
    assert rd.validate_knob('TP', '')[1]
    assert rd.validate_knob(None, 'x')[1]


# ─── Status ───────────────────────────────────────────────────────────

def test_status_page_document(fake):
    r = fake.client.get('/api/radiance')
    assert r.status_code == 200
    st = r.get_json()
    assert st['installed'] and st['running'] and st['enabled'] and st['active'] == 'active'
    assert st['unit'] == 'vllm-qwen' and st['container'] == 'vllmnvfp4' and st['port'] == 8080
    assert st['tp'] == '1'                       # GPUS=0, no TP → one card
    assert st['checkpoint'] == 'Qwen3.8-27B-Uncensored-NVFP4'
    assert st['version'] == '0.27.1' and [m['id'] for m in st['models']][0] == 'Qwen3.8'
    assert st['startup']['kv_tokens'] == 140700 and st['startup']['ready']
    assert st['metrics']['requests_total'] == 24
    assert st['editable'] and st['helper'] is None and not st['dropin_present'] and st['overrides'] == {}
    assert st['conflict'] == {'unit': 'llama-server', 'present': True, 'active': False, 'enabled': False}
    assert st['models_available'] == ['/home/backupuser/models/Qwen3.8-27B-Uncensored-NVFP4']
    rows = {k['key']: k for k in st['knobs']}
    assert rows['SNAP']['source'] == 'unit' and rows['SNAP']['unit_value'] == rows['SNAP']['value']
    assert rows['TP']['source'] == 'launcher' and rows['TP']['value'] == '1' and rows['TP']['range'] == [1, 8]
    assert rows['SPEC_METHOD']['choices'] == ['dflash', 'mtp']
    assert rows['RADIANCE_NVFP4_MXFP4']['group'] == 'advanced' and rows['RADIANCE_NVFP4_MXFP4']['source'] == 'unit'
    assert 'EXTRA' not in rows
    assert st['warm_start_s'] == 180
    # exactly: one show of each unit, HTTP + docker, no systemctl action
    assert [c for c in fake.calls if c[0] == 'systemctl' and c[1] != 'show'] == []


def test_status_unit_missing(fake):
    fake.show = 'Id=vllm-qwen.service\nLoadState=not-found\nActiveState=inactive\n'
    st = fake.client.get('/api/radiance').get_json()
    assert st['installed'] is False and 'not found' in st['error'] and st['metrics'] is None
    assert fake.client.post('/api/radiance/params', json={'overrides': {'TP': '2'}}).status_code == 404
    assert fake.client.post('/api/radiance/action', json={'action': 'start'}).status_code == 404
    assert rd._radiance_summary() is None and rd._radiance_alerts() == [] and rd._radiance_metrics() == []


def test_status_inactive_skips_http(fake):
    fake.show = _show_text(ActiveState='inactive', SubState='dead', MainPID='0')
    fake.http = lambda *a: (_ for _ in ()).throw(AssertionError('no HTTP while inactive'))
    st = fake.client.get('/api/radiance').get_json()
    assert st['running'] is False and st['metrics'] is None and 'version' not in st


# ─── Actions ──────────────────────────────────────────────────────────

def test_action_argv_and_conflict_note(fake):
    fake.conflict = 'Id=llama-server.service\nLoadState=loaded\nActiveState=active\nUnitFileState=enabled\n'
    r = fake.client.post('/api/radiance/action', json={'action': 'start'}).get_json()
    assert r['success'] and r['action'] == 'start' and r['warm_start_s'] == 180
    assert 'llama-server was stopped' in r['note']
    assert ['systemctl', 'start', 'vllm-qwen.service'] in fake.calls
    for a in ('stop', 'restart', 'enable', 'disable'):
        assert fake.client.post('/api/radiance/action', json={'action': a}).get_json()['success']
        assert ['systemctl', a, 'vllm-qwen.service'] in fake.calls
    assert fake.client.post('/api/radiance/action', json={'action': 'kill'}).status_code == 400
    assert fake.client.post('/api/radiance/action', json={'action': 'start; rm'}).status_code == 400


def test_action_readonly_is_refused(fake, monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('ro', 'readonly'))
    assert fake.client.get('/api/radiance').status_code == 200
    assert fake.client.post('/api/radiance/action', json={'action': 'stop'}).status_code == 403
    assert fake.client.post('/api/radiance/params', json={'overrides': {}}).status_code == 403
    assert not any(c[0] == 'systemctl' and c[1] != 'show' for c in fake.calls)


# ─── Boot parameters (drop-in) ────────────────────────────────────────

def test_params_write_dropin_as_root(fake):
    body = {'overrides': {'PORT': '8081', 'MAXSEQS': '3', 'KV_MEM': 'auto', 'GPUS': ''}}
    r = fake.client.post('/api/radiance/params', json=body)
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    # MAXSEQS=3 equals the unit file → no override; GPUS '' = no override
    assert j['overrides'] == {'PORT': '8081', 'KV_MEM': 'auto'} and j['dropin_present'] and not j['restarted']
    assert fake.dropin.read_text() == rd.render_dropin({'PORT': '8081', 'KV_MEM': 'auto'})
    assert stat.S_IMODE(fake.dropin.stat().st_mode) == 0o644
    assert ['systemctl', 'daemon-reload'] in fake.calls
    assert ['systemctl', 'restart', 'vllm-qwen.service'] not in fake.calls
    # the page now reads the drop-in back (systemctl show is still the capture,
    # so `value` is the effective one systemd reports — the override shows on its row)
    st = fake.client.get('/api/radiance').get_json()
    assert st['dropin_present'] and st['overrides'] == {'PORT': '8081', 'KV_MEM': 'auto'}
    rows = {k['key']: k for k in st['knobs']}
    assert rows['PORT']['source'] == 'override' and rows['PORT']['override'] == '8081'
    assert 'Environment=PORT=8081' in st['dropin_text']


def test_params_shape_change_under_kv_pin_needs_confirm(fake):
    # the unit pins KV_MEM=6539000000 (measured at MAXSEQS=3); MAXSEQS=8 under it is refused
    r = fake.client.post('/api/radiance/params', json={'overrides': {'MAXSEQS': '8'}}).get_json()
    assert r['success'] is False and r['needs_confirm'] and 'MAXSEQS' in r['warning'] and '6539000000' in r['warning']
    assert not fake.dropin.exists()
    # KV_MEM=auto alongside → no pin after the change → written without confirm
    r = fake.client.post('/api/radiance/params', json={'overrides': {'MAXSEQS': '8', 'KV_MEM': 'auto'}}).get_json()
    assert r['success'] and r['overrides'] == {'MAXSEQS': '8', 'KV_MEM': 'auto'}
    # confirm keeps the pin
    r = fake.client.post('/api/radiance/params', json={'overrides': {'MAXSEQS': '8'}, 'confirm': True}).get_json()
    assert r['success'] and r['overrides'] == {'MAXSEQS': '8'}
    # a non-shape change under the pin never asks
    r = fake.client.post('/api/radiance/params', json={'overrides': {'PORT': '8081'}}).get_json()
    assert r['success'] and r['overrides'] == {'PORT': '8081'}


def test_params_invalid_writes_nothing(fake):
    for body in ({'overrides': {'TP': '0'}}, {'overrides': {'EXTRA': '--x'}}, {'overrides': {'ExecStart': '/bin/sh'}},
                 {'overrides': {'SNAP': '../x'}}, {'overrides': 'TP=2'}, {}, {'overrides': {'TP': ['2']}}):
        r = fake.client.post('/api/radiance/params', json=body)
        assert r.status_code == 400, body
    # dflash with the drafter pointed at the target
    r = fake.client.post('/api/radiance/params', json={'overrides': {'DRAFTER': '/home/backupuser/models/Qwen3.8-27B-Uncensored-NVFP4'}})
    assert r.status_code == 400 and 'DRAFTER' in r.get_json()['error']
    assert not fake.dropin.exists()
    assert ['systemctl', 'daemon-reload'] not in fake.calls


def test_params_with_restart_and_reset(fake):
    r = fake.client.post('/api/radiance/params', json={'overrides': {'PORT': '8081'}, 'restart': True}).get_json()
    assert r['success'] and r['restarted'] and r['warm_start_s'] == 180
    assert fake.calls.index(['systemctl', 'daemon-reload']) < fake.calls.index(['systemctl', 'restart', 'vllm-qwen.service'])
    assert fake.dropin.exists()
    r = fake.client.post('/api/radiance/params/reset', json={}).get_json()
    assert r['success'] and r['overrides'] == {} and not r['dropin_present'] and not r['restarted']
    assert not fake.dropin.exists() and not fake.dropin.parent.exists()
    assert fake.calls.count(['systemctl', 'daemon-reload']) == 2
    # an all-empty override map is the same as reset
    fake.client.post('/api/radiance/params', json={'overrides': {'PORT': '8081'}})
    assert fake.dropin.exists()
    r = fake.client.post('/api/radiance/params', json={'overrides': {'PORT': ''}}).get_json()
    assert r['success'] and not r['dropin_present'] and not fake.dropin.exists()


def test_params_reset_leaves_other_files_in_the_dir(fake):
    fake.dropin.parent.mkdir(parents=True)
    (fake.dropin.parent / '10-site.conf').write_text('[Service]\nEnvironment=X=1\n')
    assert fake.client.post('/api/radiance/params', json={'overrides': {'PORT': '8081'}}).get_json()['dropin_present']
    assert fake.dropin.exists()
    r = fake.client.post('/api/radiance/params/reset', json={}).get_json()
    assert r['success'] and not fake.dropin.exists()
    assert (fake.dropin.parent / '10-site.conf').exists()


def test_params_not_root_uses_helper(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(rd.os, 'geteuid', lambda: 999)
    # helper missing → read-only, nothing run
    monkeypatch.setattr(rd, 'RADIANCE_HELPER', str(tmp_path / 'no-such-helper'))
    st = fake.client.get('/api/radiance').get_json()
    assert st['editable'] is False and st['helper'].endswith('no-such-helper')
    r = fake.client.post('/api/radiance/params', json={'overrides': {'PORT': '8081'}})
    assert r.status_code == 400 and 'read-only' in r.get_json()['error']
    # helper present → its argv + the drop-in text on stdin; nothing written by the app
    helper = tmp_path / 'nexus-dashboard-radiance'
    helper.write_text('#!/bin/sh\n')
    helper.chmod(0o755)
    monkeypatch.setattr(rd, 'RADIANCE_HELPER', str(helper))
    r = fake.client.post('/api/radiance/params', json={'overrides': {'PORT': '8081'}}).get_json()
    assert r['success']
    i = fake.calls.index([str(helper), 'write-dropin', 'vllm-qwen'])
    assert fake.inputs[i] == rd.render_dropin({'PORT': '8081'})
    assert not fake.dropin.exists()
    assert ['systemctl', 'daemon-reload'] not in fake.calls       # the helper does that
    fake.client.post('/api/radiance/params/reset', json={})
    assert [str(helper), 'reset', 'vllm-qwen'] in fake.calls


# ─── Logs ─────────────────────────────────────────────────────────────

def test_logs(fake):
    r = fake.client.get('/api/radiance/logs?source=container&tail=50').get_json()
    assert r['name'] == 'vllmnvfp4' and 'GPU KV cache size' in r['logs']
    r = fake.client.get('/api/radiance/logs?source=journal&tail=50').get_json()
    assert r['name'] == 'vllm-qwen.service' and 'serve-tp1.sh' in r['logs']
    assert ['journalctl', '-u', 'vllm-qwen.service', '--no-pager', '-n', '50', '--output=short-iso'] in fake.calls
    assert fake.client.get('/api/radiance/logs?source=syslog').status_code == 400


# ─── Hooks ────────────────────────────────────────────────────────────

def test_summary_block(fake):
    s = rd._radiance_summary()
    assert s['installed'] and s['state'] == 'active' and s['running'] and s['ready'] and s['enabled']
    # model = the loaded checkpoint, not the first served alias (operator: "it only says qwen 3.8")
    assert s['model'] == 'Qwen3.8-27B-Uncensored-NVFP4' and s['served_name'] == 'Qwen3.8' and s['tp'] == '1'
    assert s['image'] == 'stilldeadcode/vllm-radiance:0.9.3' and s['port'] == 8080
    assert s['requests_running'] == 0 and s['kv_cache_pct'] == 0.0 and s['requests_total'] == 24
    assert s['has_overrides'] is False and s['nrestarts'] == 0
    # engine still loading: active, no metrics → not ready, model falls back to the checkpoint
    fake.metrics = None
    s = rd._radiance_summary()
    assert s['running'] and s['ready'] is False and s['model'] == 'Qwen3.8-27B-Uncensored-NVFP4' and s['served_name'] is None
    # through the real aggregator, toggle-gated like every module hook
    r = fake.client.get('/api/summary').get_json()
    assert r['radiance']['model'] == 'Qwen3.8-27B-Uncensored-NVFP4'


def test_alerts(fake):
    assert rd._radiance_alerts() == []
    fake.show = _show_text(ActiveState='failed', SubState='failed', NRestarts='2')
    keys = {a['key']: a['message'] for a in rd._radiance_alerts()}
    assert 'radiance_down' in keys and 'failed' in keys['radiance_down']
    assert 'radiance_restarting' in keys and '2 time(s)' in keys['radiance_restarting']
    # disabled + stopped on purpose is not an alert
    fake.show = _show_text(ActiveState='inactive', SubState='dead', UnitFileState='disabled')
    assert rd._radiance_alerts() == []
    # queue
    fake.show = SHOW
    fake.metrics = METRICS.replace('vllm:num_requests_waiting{engine="0",model_name="Qwen3.8"} 0.0',
                                   'vllm:num_requests_waiting{engine="0",model_name="Qwen3.8"} 2.0')
    keys = {a['key']: a['message'] for a in rd._radiance_alerts()}
    assert keys == {'radiance_queue': keys['radiance_queue']} and '2 request(s) waiting' in keys['radiance_queue']
    # both enabled at boot
    fake.metrics = METRICS
    fake.conflict = 'Id=llama-server.service\nLoadState=loaded\nActiveState=inactive\nUnitFileState=enabled\n'
    keys = {a['key'] for a in rd._radiance_alerts()}
    assert keys == {'radiance_conflict'}
    fake.conflict = ''                                 # llama-server not installed at all
    assert rd._radiance_alerts() == []


def test_history_rows_and_token_delta(fake):
    rows = rd._radiance_history()
    assert {r[0] for r in rows} == rd.RADIANCE_HISTORY_METRICS
    assert all(r[1] == 'vllm-qwen' for r in rows)
    d = {r[0]: r[2] for r in rows}
    assert d['radiance_running'] == 0 and d['radiance_kv_pct'] == 0.0
    assert d['radiance_accept'] == round(4786 / 13244 * 100, 1)
    assert d['radiance_tokens'] is None                # first tick: nothing to diff against
    assert json.load(open(rd.RADIANCE_PREV_FILE))['generation_tokens_total'] == 6677
    fake.metrics = METRICS.replace('generation_tokens_total{engine="0",model_name="Qwen3.8"} 6677.0',
                                   'generation_tokens_total{engine="0",model_name="Qwen3.8"} 7000.0')
    d = {r[0]: r[2] for r in rd._radiance_history()}
    assert d['radiance_tokens'] == 323
    fake.metrics = METRICS                             # counter went backwards (restart) → 0, never negative
    assert {r[0]: r[2] for r in rd._radiance_history()}['radiance_tokens'] == 0
    fake.metrics = None
    assert rd._radiance_history() == []
    from nexusdash.core import history
    assert rd.RADIANCE_HISTORY_METRICS <= history.HISTORY_METRICS


def test_metrics_lines(fake):
    lines = rd._radiance_metrics()
    assert 'storagedash_radiance_up 1' in lines
    assert 'storagedash_radiance_requests_total 24' in lines
    assert any(l.startswith('storagedash_radiance_draft_accept_ratio ') for l in lines)
    fake.show = _show_text(ActiveState='inactive', SubState='dead')
    assert rd._radiance_metrics()[-1] == 'storagedash_radiance_up 0'


def test_module_descriptor_and_toggle(fake, monkeypatch):
    m = app._DESCRIPTORS['radiance']
    assert m['default_enabled'] is False and m['category'] == 'AI Tools' and m['order'] == 123
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'radiance'})
    r = fake.client.get('/api/radiance')
    assert r.status_code == 403 and 'disabled' in r.get_json()['error']


# ─── TP through the serve-tpN.sh wrapper ──────────────────────────────

def test_launcher_parsing():
    assert rd.parse_exec_start(rd.parse_show(SHOW)['ExecStart']) == '/home/backupuser/vllm-mxfp4/serve-tp1.sh'
    assert rd.launcher_tp('/x/serve-tp4.sh') == '4' and rd.launcher_tp('/x/serve-mxfp4.sh') is None
    assert rd.parse_unit_exec(UNIT) == '/home/backupuser/vllm-mxfp4/serve-tp1.sh'
    assert rd.parse_unit_exec('[Service]\nExecStart=\nExecStart=/a/serve-tp2.sh\n') == '/a/serve-tp2.sh'
    assert rd.parse_unit_exec('[Service]\nExecStart=/a/serve-tp2.sh\nExecStart=\n') == ''
    # the TP override renders as an ExecStart swap only when the unit uses a wrapper
    t = rd.render_dropin({'TP': '2', 'KV_MEM': 'auto'}, '/home/backupuser/vllm-mxfp4/serve-tp1.sh')
    assert 'ExecStart=\nExecStart=/home/backupuser/vllm-mxfp4/serve-tp2.sh\n' in t and 'TP=' not in t
    t = rd.render_dropin({'TP': '2'}, '/home/backupuser/vllm-mxfp4/serve-mxfp4.sh')
    assert 'Environment=TP=2' in t and 'ExecStart' not in t


def test_tp_knob_reports_the_wrapper_pin(fake):
    st = fake.client.get('/api/radiance').get_json()
    assert st['launcher'].endswith('/serve-tp1.sh') and st['launcher_tp'] == '1' and st['unit_launcher_tp'] == '1'
    tp = [k for k in st['knobs'] if k['key'] == 'TP'][0]
    assert tp['value'] == '1' and tp['unit_value'] == '1' and tp['source'] == 'launcher'
    assert st['tp'] == '1'


def test_tp_override_swaps_the_wrapper(fake, monkeypatch):
    # no sibling wrapper → refused, nothing written
    monkeypatch.setattr(rd.os.path, 'isfile', lambda p: False)
    r = fake.client.post('/api/radiance/params', json={'overrides': {'TP': '2', 'KV_MEM': 'auto'}})
    assert r.status_code == 400 and 'serve-tp2.sh' in r.get_json()['error'] and not fake.dropin.exists()
    # TP equal to the wrapper's own pin is not an override
    r = fake.client.post('/api/radiance/params', json={'overrides': {'TP': '1'}}).get_json()
    assert r['success'] and r['overrides'] == {} and not r['dropin_present']
    # sibling present → ExecStart swap in the drop-in; the page reads TP=2 back as an override
    monkeypatch.setattr(rd.os.path, 'isfile', lambda p: p.endswith('/serve-tp2.sh'))
    r = fake.client.post('/api/radiance/params', json={'overrides': {'TP': '2', 'KV_MEM': 'auto'}}).get_json()
    assert r['success'] and r['overrides'] == {'TP': '2', 'KV_MEM': 'auto'}
    text = fake.dropin.read_text()
    assert 'ExecStart=\nExecStart=/home/backupuser/vllm-mxfp4/serve-tp2.sh\n' in text and 'Environment=TP' not in text
    st = fake.client.get('/api/radiance').get_json()
    assert st['overrides'] == {'KV_MEM': 'auto', 'TP': '2'}
    tp = [k for k in st['knobs'] if k['key'] == 'TP'][0]
    assert tp['source'] == 'override' and tp['override'] == '2'
    # a TP change is a shape change: under the unit's byte pin it asks first
    r = fake.client.post('/api/radiance/params', json={'overrides': {'TP': '2'}}).get_json()
    assert r['needs_confirm'] and 'TP' in r['warning']


# ─── The PARO launcher shape (node6: run_paroquant.sh, 2x R9700) ───────

PARO_UNIT = '''[Unit]
Description=vLLM Qwen3.8-27B int5 ParoQuant + DFlash2 on 2x R9700 (replaces llama-server)
[Service]
Type=simple
User=backupuser
Environment=HOME=/home/backupuser
Environment=RUNTIME=docker MODE=prod MODEL_DIR=Qwen3.8-27B-PARO-int5 SPEC=7 PORT=8080 GPUS=0,1
Environment=R4D_KEY=b9e42ab-rx6
Environment="SERVED_NAMES=Qwen3.8 Qwen3.8-PARO Qwen3.8-27B"
ExecStart=/home/backupuser/vllm-mxfp4/paroquant/run_paroquant.sh
ExecStop=/usr/bin/docker stop -t 30 vllmparo
[Install]
WantedBy=multi-user.target
'''
PARO_MODELS = json.dumps({'object': 'list', 'data': [
    {'id': 'Qwen3.8', 'object': 'model', 'root': '/models/Qwen3.8-27B-PARO-int5', 'max_model_len': 262144},
    {'id': 'Qwen3.8-PARO', 'object': 'model', 'root': '/models/Qwen3.8-27B-PARO-int5', 'max_model_len': 262144}]})


@pytest.fixture
def paro(fake, monkeypatch):
    show = _show_text(
        Environment='HOME=/home/backupuser RUNTIME=docker MODE=prod MODEL_DIR=Qwen3.8-27B-PARO-int5 SPEC=7 PORT=8080 GPUS=0,1 '
                    'R4D_KEY=b9e42ab-rx6 CHAT_TEMPLATE=/home/backupuser/vllm-mxfp4/qwen-fixed-v22.3.jinja '
                    '"SERVED_NAMES=Qwen3.8 Qwen3.8-PARO Qwen3.8-27B"',
        ExecStart='{ path=/home/backupuser/vllm-mxfp4/paroquant/run_paroquant.sh ; argv[]=/home/backupuser/vllm-mxfp4/paroquant/run_paroquant.sh ; ignore_errors=no }',
        ExecStop='{ path=/usr/bin/docker ; argv[]=/usr/bin/docker stop -t 30 vllmparo ; ignore_errors=no }')
    fake.show = show
    real_read = rd._read_text
    monkeypatch.setattr(rd, '_read_text', lambda p: PARO_UNIT if p == UNIT_PATH else real_read(p))
    def http(base, path, timeout):
        if path == '/v1/models':
            return PARO_MODELS, None
        return fake.http(base, path, timeout)
    monkeypatch.setattr(rd, '_http_get', http)
    def draw(method, path, body=None, timeout=60):
        assert path.startswith('/containers/vllmparo/logs?'), path
        return fake.docker_raw(method, path.replace('vllmparo', 'vllmnvfp4'), body, timeout)
    monkeypatch.setattr(rd, 'docker_raw', draw)
    return fake


def test_paro_launcher_shape(paro):
    assert rd.parse_exec_stop_name(rd.parse_show(paro.show)['ExecStop']) == 'vllmparo'
    assert rd.parse_exec_stop_name('{ path=/usr/bin/docker ; argv[]=/usr/bin/docker stop -t 30 vllmnvfp4 ; }') == 'vllmnvfp4'
    assert rd.parse_exec_stop_name('') == ''
    st = paro.client.get('/api/radiance').get_json()
    assert st['container'] == 'vllmparo'                 # from ExecStop — NAME is not in the environment
    assert st['tp'] == '2' and st['launcher_tp'] is None  # two GPUS, no wrapper, no TP= → implied
    assert st['checkpoint'] == 'Qwen3.8-27B-PARO-int5'    # MODEL_DIR, not SNAP
    rows = {k['key']: k for k in st['knobs']}
    assert rows['TP']['value'] == '2' and rows['TP']['source'] == 'gpus'
    assert rows['MODEL_DIR']['source'] == 'unit' and rows['MODE']['value'] == 'prod'
    assert rows['R4D_KEY']['group'] == 'other' and rows['R4D_KEY']['type'] == 'readonly'
    assert 'HOME' not in rows
    s = rd._radiance_summary()
    assert s['model'] == 'Qwen3.8-27B-PARO-int5' and s['served_name'] == 'Qwen3.8' and s['tp'] == '2'
    # a TP override on a non-wrapper launcher is a plain Environment= line
    r = paro.client.post('/api/radiance/params', json={'overrides': {'TP': '4'}}).get_json()
    assert r['success'] and 'Environment=TP=4' in paro.dropin.read_text()
    # MODEL_DIR is a directory NAME; a path or junk is refused
    for bad in ('/models/x', '../x', 'a b'):
        assert paro.client.post('/api/radiance/params', json={'overrides': {'MODEL_DIR': bad}}).status_code == 400
    r = paro.client.post('/api/radiance/params', json={'overrides': {'MODEL_DIR': 'Qwen3.8-27B-PARO-MXFP4-ft', 'MODE': 'eval'}}).get_json()
    assert r['success'] and r['overrides'] == {'MODEL_DIR': 'Qwen3.8-27B-PARO-MXFP4-ft', 'MODE': 'eval'}


def test_checkpoint_falls_back_to_the_served_models_root(paro):
    # neither SNAP nor MODEL_DIR in the environment → /v1/models' root names the weights
    paro.show = _show_text(Environment='RUNTIME=docker PORT=8080 GPUS=0,1',
                           ExecStop='{ path=/usr/bin/docker ; argv[]=/usr/bin/docker stop -t 30 vllmparo ; }')
    st = paro.client.get('/api/radiance').get_json()
    assert st['checkpoint'] == 'Qwen3.8-27B-PARO-int5'
    assert rd._radiance_summary()['model'] == 'Qwen3.8-27B-PARO-int5'
