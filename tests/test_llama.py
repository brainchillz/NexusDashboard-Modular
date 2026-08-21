"""llama.cpp module tests — the pure logic behind model/arg editing. These guard
config-file injection (newlines/quotes into /etc/llama.conf) and argument
injection (flags/values that reach the llama-server command line), plus the
opts round-trip used by the editor.
"""
import os
import json
import time
import threading
import http.server
import shutil
import urllib.error
import pytest
import app


def test_llamacpp_registered_as_service_no_alerts():
    svc = app.SYSTEM_SERVICES.get('llamacpp')
    assert svc and svc['service'] == 'llama-server'
    assert svc.get('alert') is False        # never spams health alerts
    assert svc.get('pkg') is None           # not apt-managed


def test_llamacpp_is_a_toggleable_module():
    m = [x for x in app.MODULES if x['id'] == 'llamacpp']
    assert m and m[0]['category'] == 'AI Tools'


def test_parse_opts_handles_bool_value_and_equals():
    parsed = app._llama_parse_opts('--threads 8 --mlock --n-gpu-layers=99 -fa')
    assert {'flag': '--threads', 'value': '8'} in parsed
    assert {'flag': '--mlock', 'value': ''} in parsed         # known boolean
    assert {'flag': '--n-gpu-layers', 'value': '99'} in parsed  # --flag=value form
    assert {'flag': '-fa', 'value': ''} in parsed              # short boolean


def test_parse_opts_keeps_enum_flag_values():
    """-fa/--log-colors take 'on|off|auto' upstream, so their value must survive
    a parse/format round trip — listed as booleans they were silently dropped."""
    parsed = app._llama_parse_opts('-fa on --log-colors off --threads 8')
    assert {'flag': '-fa', 'value': 'on'} in parsed
    assert {'flag': '--log-colors', 'value': 'off'} in parsed
    assert app._llama_format_opts(parsed) == '-fa on --log-colors off --threads 8'


def test_opts_round_trip():
    s = '--threads 16 --n-gpu-layers 99 --mlock'
    assert app._llama_format_opts(app._llama_parse_opts(s)) == s


def test_format_opts_skips_empty_flags():
    assert app._llama_format_opts([{'flag': '', 'value': 'x'},
                                   {'flag': '--ctx-size', 'value': '4096'}]) == '--ctx-size 4096'


def test_flag_and_value_regexes_block_injection():
    assert app.RE_LLAMA_FLAG.match('--n-gpu-layers')
    assert app.RE_LLAMA_FLAG.match('-fa')
    assert not app.RE_LLAMA_FLAG.match('--bad;rm')       # shell metachar
    assert not app.RE_LLAMA_FLAG.match('--a b')          # space
    assert app.RE_LLAMA_VALUE.match('3,1')               # tensor-split style
    assert app.RE_LLAMA_VALUE.match('/usr/share/models/x.gguf')
    assert not app.RE_LLAMA_VALUE.match('x y')           # space (extra arg injection)
    assert not app.RE_LLAMA_VALUE.match('a\nLLAMA_OPTS=evil')  # newline (conf injection)
    assert not app.RE_LLAMA_VALUE.match('"quoted"')      # quote breaks LLAMA_OPTS="..."


def test_clean_args_validates_and_drops_model_flag():
    clean, e = app._llama_clean_args([
        {'flag': '--threads', 'value': '8'},
        {'flag': '', 'value': 'x'},          # empty flag -> dropped
        {'flag': '-m', 'value': '/x.gguf'},  # model flag -> dropped (managed separately)
        {'flag': '--mlock', 'value': ''},
    ])
    assert e is None
    assert clean == [{'flag': '--threads', 'value': '8'}, {'flag': '--mlock', 'value': ''}]
    # injection attempts are rejected
    assert app._llama_clean_args([{'flag': '--bad;rm', 'value': 'x'}])[1] is not None
    assert app._llama_clean_args([{'flag': '--threads', 'value': 'a\nevil'}])[1] is not None
    assert app._llama_clean_args('nope')[1] is not None


def test_preset_name_regex():
    assert app.RE_LLAMA_PRESET.match('GPU heavy 128k')
    assert app.RE_LLAMA_PRESET.match('cpu-only_v2')
    assert not app.RE_LLAMA_PRESET.match('')
    assert not app.RE_LLAMA_PRESET.match(' leading-space')
    assert not app.RE_LLAMA_PRESET.match('bad/slash')
    assert not app.RE_LLAMA_PRESET.match('x' * 65)


def test_presets_load_missing_and_bad(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'LLAMA_PRESETS_FILE', str(tmp_path / 'nope.json'))
    assert app._load_llama_presets() == {}
    p = tmp_path / 'p.json'
    p.write_text('{ not json')
    monkeypatch.setattr(app, 'LLAMA_PRESETS_FILE', str(p))
    assert app._load_llama_presets() == {}


def test_valid_model_confined_to_models_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'LLAMA_MODELS_DIR', str(tmp_path))
    good = tmp_path / 'qwen.gguf'
    good.write_text('x')
    assert app._llama_valid_model(str(good)) is True
    assert app._llama_valid_model(str(tmp_path / 'missing.gguf')) is False   # not present
    assert app._llama_valid_model(str(tmp_path / 'notes.txt')) is False      # not .gguf
    # Path traversal escaping the models dir is rejected.
    assert app._llama_valid_model(str(tmp_path / '..' / 'etc' / 'x.gguf')) is False
    assert app._llama_valid_model('/etc/passwd') is False


# ─── 06b — model + args profiles (back-compat normalization) ────────────

def test_norm_preset_backcompat():
    # Legacy shape: a bare args list -> {model:'', args:[...]}
    legacy = [{'flag': '--threads', 'value': '8'}]
    assert app._norm_preset(legacy) == {'model': '', 'args': legacy}
    # Current shape: {model, args} preserved
    assert app._norm_preset({'model': '/m/x.gguf', 'args': []}) == {'model': '/m/x.gguf', 'args': []}
    # Dict missing args -> empty list
    assert app._norm_preset({'model': '/m/x.gguf'}) == {'model': '/m/x.gguf', 'args': []}
    # Junk shapes normalize to empty
    assert app._norm_preset('nope') == {'model': '', 'args': []}
    assert app._norm_preset({'args': 'notalist'}) == {'model': '', 'args': []}


def test_presets_load_normalizes_legacy(tmp_path, monkeypatch):
    p = tmp_path / 'p.json'
    p.write_text(json.dumps({
        'old': [{'flag': '--mlock', 'value': ''}],
        'new': {'model': '/m/x.gguf', 'args': [{'flag': '--threads', 'value': '8'}]},
    }))
    monkeypatch.setattr(app, 'LLAMA_PRESETS_FILE', str(p))
    loaded = app._load_llama_presets()
    assert loaded['old'] == {'model': '', 'args': [{'flag': '--mlock', 'value': ''}]}
    assert loaded['new']['model'] == '/m/x.gguf'


# ─── 06c — profile portability (export/import between nodes) ────────────
# Export and import are implemented in ai.js and add NO endpoint: import POSTs
# the pasted document to the ordinary save endpoint. These guard that contract
# from the server side — the exported shape must be accepted, the extra
# provenance keys must be ignored, and a pasted document must not be able to
# smuggle anything past the validators the form is held to.

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    app.app.config['TESTING'] = True
    return app.app.test_client()


def _export_doc(**over):
    """The document ai.js llamaProfileDoc() emits."""
    doc = {
        'kind': 'nexus-dashboard/llama-profile',
        'version': 1,
        'name': 'Big context',
        'model': '',
        'args': [{'flag': '--ctx-size', 'value': '32768'},
                 {'flag': '--mlock', 'value': ''}],
        'exported_from': {'host': 'node1.example.com',
                          'app_version': '3.2.0', 'at': '2026-08-21T00:00:00Z'},
    }
    doc.update(over)
    return doc


def test_exported_document_imports_and_extra_keys_are_ignored(client, tmp_path, monkeypatch):
    """An export document POSTs straight to the save endpoint; kind/version/
    exported_from are provenance and must not reach the stored profile."""
    store = tmp_path / 'presets.json'
    monkeypatch.setattr(app, 'LLAMA_PRESETS_FILE', str(store))
    r = client.post('/api/llama/presets', json=_export_doc())
    assert r.status_code == 200 and r.get_json()['success'] is True
    saved = json.loads(store.read_text())
    assert set(saved) == {'Big context'}
    assert saved['Big context'] == {
        'model': '',
        'args': [{'flag': '--ctx-size', 'value': '32768'},
                 {'flag': '--mlock', 'value': ''}],
    }


def test_import_cannot_smuggle_an_injected_arg_value(client, tmp_path, monkeypatch):
    """A hand-edited document is still just a form submission — the same
    flag/value validators apply, so nothing reaches /etc/llama.conf unchecked."""
    monkeypatch.setattr(app, 'LLAMA_PRESETS_FILE', str(tmp_path / 'presets.json'))
    bad = _export_doc(args=[{'flag': '--ctx-size', 'value': '4096\nLLAMA_OPTS=--rm -rf'}])
    r = client.post('/api/llama/presets', json=bad)
    assert r.status_code == 400
    assert 'Invalid value' in r.get_json()['error']
    assert not (tmp_path / 'presets.json').exists()


def test_import_cannot_smuggle_a_foreign_model_path(client, tmp_path, monkeypatch):
    """ai.js re-resolves the model against the local models dir precisely
    because a path from another node will not exist here — and if one is sent
    anyway, the models-dir confinement check refuses it."""
    monkeypatch.setattr(app, 'LLAMA_PRESETS_FILE', str(tmp_path / 'presets.json'))
    monkeypatch.setattr(app, 'LLAMA_MODELS_DIR', str(tmp_path / 'models'))
    r = client.post('/api/llama/presets', json=_export_doc(model='/etc/passwd'))
    assert r.status_code == 400
    assert 'model' in r.get_json()['error'].lower()


def test_exported_arg_shape_survives_clean_args():
    """The {flag, value} pairs the export writes are exactly what the shared
    validator consumes — no translation layer to drift."""
    args = _export_doc()['args']
    clean, e = app._llama_clean_args(args)
    assert e is None and clean == args


# ─── 06a — Hugging Face model pull validators ───────────────────────────

def test_hf_repo_and_file_regexes():
    assert app.RE_HF_REPO.match('bartowski/Llama-3.2-3B-Instruct-GGUF')
    assert app.RE_HF_REPO.match('TheBloke/Mixtral-8x7B-GGUF')
    assert not app.RE_HF_REPO.match('noslash')
    assert not app.RE_HF_REPO.match('a/b/c')            # extra path segment
    assert not app.RE_HF_REPO.match('../etc/passwd')    # traversal (leading dot)
    assert not app.RE_HF_REPO.match('org/mo del')       # space
    # rfilenames may now be nested — that is how big split quants ship — so a
    # subdirectory is allowed, but traversal and absolute paths are not.
    assert app.RE_HF_RFILE.match('model-Q4_K_M.gguf')
    assert app.RE_HF_RFILE.match('Meta-Llama-3.1-70B-Q8_0/part-00001-of-00002.gguf')
    assert not app.RE_HF_RFILE.match('model.bin')       # not .gguf
    assert not app.RE_HF_RFILE.match('../x.gguf')       # traversal
    assert not app.RE_HF_RFILE.match('a/../../x.gguf')  # traversal mid-path
    assert not app.RE_HF_RFILE.match('/abs/x.gguf')     # absolute
    assert not app.RE_HF_RFILE.match('a//x.gguf')       # empty segment


# ─── 06d — Hugging Face GGUF download ───────────────────────────────────

def test_gguf_group_covers_both_repo_layouts():
    """Split quants ship two ways and both must fetch as one unit."""
    # a directory per quant (bartowski's layout for big models)
    assert app._gguf_group('Meta-Llama-3.1-70B-Q8_0/x-00001-of-00002.gguf') == 'Meta-Llama-3.1-70B-Q8_0'
    assert app._gguf_group('Meta-Llama-3.1-70B-Q8_0/x-00002-of-00002.gguf') == 'Meta-Llama-3.1-70B-Q8_0'
    # flat split parts -> the split suffix is stripped so parts group together
    assert app._gguf_group('Model-Q6_K-00001-of-00003.gguf') == 'Model-Q6_K'
    assert app._gguf_group('Model-Q6_K-00003-of-00003.gguf') == 'Model-Q6_K'
    # a plain single file is its own group
    assert app._gguf_group('Llama-3.2-3B-Instruct-IQ3_M.gguf') == 'Llama-3.2-3B-Instruct-IQ3_M'


def test_repo_without_gguf_says_llama_cpp_needs_a_gguf(monkeypatch):
    """The whole point of the check: a normal (safetensors) repo is refused in
    words that say what is wrong, not a bare 404."""
    monkeypatch.setattr(app, '_hf_get_json', lambda *a, **k: {'siblings': [
        {'rfilename': 'config.json'}, {'rfilename': 'model-00001-of-00004.safetensors'}]})
    groups, e = app._hf_gguf_groups('meta-llama/Llama-3.2-3B-Instruct', '')
    assert groups is None
    assert 'llama.cpp needs a GGUF' in e
    assert '-GGUF' in e          # points at the fix


def test_gguf_groups_sums_sizes_and_counts_parts(monkeypatch):
    monkeypatch.setattr(app, '_hf_get_json', lambda *a, **k: {'siblings': [
        {'rfilename': 'README.md', 'size': 10},
        {'rfilename': 'Q8_0/m-00001-of-00002.gguf', 'size': 100},
        {'rfilename': 'Q8_0/m-00002-of-00002.gguf', 'size': 50},
        {'rfilename': 'Small-Q4.gguf', 'size': 7},
    ]})
    groups, e = app._hf_gguf_groups('org/repo', '')
    assert e is None
    by = {g['name']: g for g in groups}
    assert by['Q8_0']['bytes'] == 150 and by['Q8_0']['parts'] == 2
    assert by['Small-Q4']['bytes'] == 7 and by['Small-Q4']['parts'] == 1


def test_gated_repo_asks_for_a_token(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError('u', 401, 'no', {}, None)
    monkeypatch.setattr(app, '_hf_get_json', boom)
    groups, e = app._hf_gguf_groups('meta-llama/Llama-3.2-3B-Instruct', '')
    assert groups is None and 'token' in e.lower()


def test_group_dest_dir_is_confined_to_the_models_dir(tmp_path, monkeypatch):
    """The group name comes from the Hugging Face API, so it is untrusted."""
    monkeypatch.setattr(app, 'LLAMA_MODELS_DIR', str(tmp_path))
    assert app._group_dest_dir('Q8_0') == str(tmp_path / 'Q8_0')
    assert app._group_dest_dir('../escape') is None
    assert app._group_dest_dir('/etc') is None
    assert app._group_dest_dir('a/b') is None        # no nesting
    assert app._group_dest_dir('') is None
    assert app._group_dest_dir('.') is None


def test_split_model_lists_only_its_first_part(tmp_path, monkeypatch):
    """The picker must offer one loadable entry per model, not every part."""
    monkeypatch.setattr(app, 'LLAMA_MODELS_DIR', str(tmp_path))
    d = tmp_path / 'Big-Q8_0'
    d.mkdir()
    for i in (1, 2, 3):
        (d / ('Big-Q8_0-0000%d-of-00003.gguf' % i)).write_text('x')
    (tmp_path / 'Small.gguf').write_text('x')
    (tmp_path / 'mmproj-thing.gguf').write_text('x')   # projector, never a model
    # macOS AppleDouble forks: ~4KB of metadata wearing a .gguf suffix. Seen on
    # node5, where three of them sat in the picker looking like real models.
    (tmp_path / '._Small.gguf').write_text('x')
    (d / '._Big-Q8_0-00001-of-00003.gguf').write_text('x')
    (tmp_path / '._mmproj-thing.gguf').write_text('x')   # defeats a bare mmproj- check
    names = [m['name'] for m in app._llama_models()]
    assert names == ['Big-Q8_0/Big-Q8_0-00001-of-00003.gguf', 'Small.gguf']
    big = [m for m in app._llama_models() if 'Big' in m['name']][0]
    assert big['parts'] == 3


def test_web_ui_link_data_from_the_configured_flags():
    args = [{'flag': '--host', 'value': '0.0.0.0'}, {'flag': '--port', 'value': '8081'}]
    assert app._llama_web_ui(args) == {'port': 8081, 'host': '0.0.0.0', 'reachable': True}
    # default when unset: llama-server's own default port, loopback bind
    d = app._llama_web_ui([])
    assert d['port'] == 8080 and d['reachable'] is False
    # a loopback bind is reported unreachable rather than offered as a dead link
    assert app._llama_web_ui([{'flag': '--host', 'value': '127.0.0.1'}])['reachable'] is False
    # -p is --prompt in llama.cpp, NOT a port
    assert app._llama_web_ui([{'flag': '-p', 'value': '9999'}])['port'] == 8080


def test_interrupted_job_is_reported_resumable_not_failed(tmp_path, monkeypatch):
    """A restart kills the worker; the partial files are fine, so the job must
    come back as resumable rather than as an error."""
    monkeypatch.setattr(app, 'MODEL_JOB_FILE', str(tmp_path / 'job.json'))
    monkeypatch.setattr(app, '_pid_alive', lambda pid: False)
    view = app._job_view({'state': 'downloading', 'pid': 424242, 'group': 'Q8_0'})
    assert view['state'] == 'interrupted'
    assert 'resumed' in view['error'] or 'resume' in view['error'].lower()
    # a live worker is left alone
    monkeypatch.setattr(app, '_pid_alive', lambda pid: True)
    assert app._job_view({'state': 'downloading', 'pid': 1})['state'] == 'downloading'


def test_job_view_never_leaks_a_token(tmp_path, monkeypatch):
    monkeypatch.setattr(app, '_pid_alive', lambda pid: True)
    assert 'token' not in app._job_view({'state': 'done', 'token': 'hf_secret'})


# ─── 06e — HF token + rate-limit settings ───────────────────────────────

def test_token_is_write_only_over_the_api(client, tmp_path, monkeypatch):
    """The browser can add or clear a token but must never read one back."""
    monkeypatch.setattr(app, 'LLAMA_HF_FILE', str(tmp_path / 'hf.json'))
    assert client.get('/api/llama/hf').get_json() == {
        'token_set': False, 'rate_mbps': 0, 'max_rate_mbps': app.MAX_RATE_MBPS}
    r = client.put('/api/llama/hf', json={'token': 'hf_abc123'})
    assert r.status_code == 200 and r.get_json()['token_set'] is True
    body = client.get('/api/llama/hf').get_json()
    assert body['token_set'] is True
    assert 'token' not in body and 'hf_abc123' not in json.dumps(body)
    assert app._hf_token() == 'hf_abc123'
    # stored 0600 — it is a credential
    assert oct(os.stat(str(tmp_path / 'hf.json')).st_mode)[-3:] == '600'
    assert client.delete('/api/llama/hf/token').get_json()['token_set'] is False
    assert app._hf_token() == ''


def test_saving_a_rate_does_not_wipe_the_token(client, tmp_path, monkeypatch):
    """Partial updates: the two settings share a file and one must not clear
    the other."""
    monkeypatch.setattr(app, 'LLAMA_HF_FILE', str(tmp_path / 'hf.json'))
    client.put('/api/llama/hf', json={'token': 'hf_keepme'})
    client.put('/api/llama/hf', json={'rate_mbps': 600})
    assert app._hf_token() == 'hf_keepme'
    assert app._hf_rate_mbps() == 600


def test_rate_limit_validation(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'LLAMA_HF_FILE', str(tmp_path / 'hf.json'))
    assert client.put('/api/llama/hf', json={'rate_mbps': 0}).status_code == 200   # unlimited
    assert client.put('/api/llama/hf', json={'rate_mbps': -1}).status_code == 400
    assert client.put('/api/llama/hf', json={'rate_mbps': 'fast'}).status_code == 400
    assert client.put('/api/llama/hf', json={'rate_mbps': 10 ** 9}).status_code == 400


def test_mbps_converts_to_decimal_bytes_per_second():
    """Link rates are decimal, so 600 Mbps is 75 MB/s — not 78.6."""
    assert int(600 * app._MBPS_TO_BPS) == 75_000_000


# ─── 06f — the transfer itself: resume, rate limiting, confinement ──────
# These run against a real local HTTP server that speaks Range, because resume
# is the whole reason a several-hundred-GB download is survivable and a mocked
# socket would not prove it.

class _RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b''
    serve_range = True
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = self.payload
        rng = self.headers.get('Range')
        self.hits.append(rng)
        if rng and self.serve_range:
            start = int(rng.split('=')[1].split('-')[0])
            body = body[start:]
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d'
                             % (start, len(self.payload) - 1, len(self.payload)))
        else:
            self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def http_server():
    _RangeHandler.hits = []
    srv = http.server.HTTPServer(('127.0.0.1', 0), _RangeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, 'http://127.0.0.1:%d/f' % srv.server_address[1]
    srv.shutdown()


def test_fetch_writes_through_a_partial_then_renames(http_server, tmp_path):
    """Nothing appears at the final path until it is complete, so a half-file
    can never be mistaken for a usable model."""
    srv, url = http_server
    _RangeHandler.payload = b'A' * 5000
    dest = str(tmp_path / 'm.gguf')
    got = app._fetch_file(url, dest, '', 0, lambda n: None, lambda: False)
    assert got == 5000
    assert open(dest, 'rb').read() == b'A' * 5000
    assert not os.path.exists(dest + '.partial')


def test_fetch_resumes_from_a_partial_with_a_range_request(http_server, tmp_path):
    """The interrupted case: bytes already on disk are not fetched again."""
    srv, url = http_server
    _RangeHandler.payload = bytes(range(256)) * 40      # 10240 bytes
    dest = str(tmp_path / 'm.gguf')
    with open(dest + '.partial', 'wb') as f:            # pretend 4000B arrived
        f.write(_RangeHandler.payload[:4000])
    app._fetch_file(url, dest, '', 0, lambda n: None, lambda: False)
    assert open(dest, 'rb').read() == _RangeHandler.payload
    assert _RangeHandler.hits == ['bytes=4000-']        # asked to resume


def test_fetch_discards_the_partial_if_the_server_ignores_range(http_server, tmp_path):
    """A server that answers 200 to a Range request is sending the WHOLE body.
    Appending it to the partial would silently corrupt the file."""
    srv, url = http_server
    _RangeHandler.payload = b'Z' * 3000
    _RangeHandler.serve_range = False
    try:
        dest = str(tmp_path / 'm.gguf')
        with open(dest + '.partial', 'wb') as f:
            f.write(b'garbage' * 100)
        app._fetch_file(url, dest, '', 0, lambda n: None, lambda: False)
        assert open(dest, 'rb').read() == b'Z' * 3000   # not 700 bytes longer
    finally:
        _RangeHandler.serve_range = True


def test_fetch_skips_a_file_that_is_already_complete(http_server, tmp_path):
    srv, url = http_server
    _RangeHandler.payload = b'Q' * 10
    dest = str(tmp_path / 'm.gguf')
    open(dest, 'wb').write(b'Q' * 10)
    assert app._fetch_file(url, dest, '', 0, lambda n: None, lambda: False) == 10
    assert _RangeHandler.hits == []                     # never asked the network


def test_fetch_stops_when_asked_and_keeps_the_partial(http_server, tmp_path):
    """Cancel must leave the bytes on disk — deleting a 300 GB partial because
    someone hit cancel would be its own kind of bug."""
    srv, url = http_server
    _RangeHandler.payload = b'B' * (4 << 20)            # 4 MiB = several chunks
    dest = str(tmp_path / 'm.gguf')
    calls = {'n': 0}

    def should_stop():
        calls['n'] += 1
        return calls['n'] > 2                            # bail after a chunk or two
    app._fetch_file(url, dest, '', 0, lambda n: None, should_stop)
    assert not os.path.exists(dest)                      # never renamed in
    assert os.path.getsize(dest + '.partial') > 0        # progress preserved


def test_rate_limit_actually_slows_the_transfer(http_server, tmp_path):
    """Not just arithmetic: the pacing has to show up as wall-clock time."""
    srv, url = http_server
    size = 2 << 20                                       # 2 MiB
    _RangeHandler.payload = b'C' * size
    dest = str(tmp_path / 'm.gguf')
    rate = size // 2                                     # bytes/sec -> ~2s of work
    t0 = time.monotonic()
    app._fetch_file(url, dest, '', rate, lambda n: None, lambda: False)
    elapsed = time.monotonic() - t0
    # Expect ~2s; allow a wide band so a loaded CI box does not flake, but it
    # must be clearly slower than the unthrottled case (which is milliseconds).
    assert 1.0 < elapsed < 6.0, elapsed
    assert os.path.getsize(dest) == size


def test_worker_downloads_every_part_and_finishes(http_server, tmp_path, monkeypatch):
    """The whole worker loop in-process: every part of a split model lands in
    one directory and the job ends `done` with the byte count filled in."""
    srv, url = http_server
    _RangeHandler.payload = b'P' * 1200
    monkeypatch.setattr(app, 'MODEL_JOB_FILE', str(tmp_path / 'job.json'))
    monkeypatch.setattr(app, 'LLAMA_HF_FILE', str(tmp_path / 'hf.json'))
    monkeypatch.setattr(app, '_hf_resolve', lambda repo, rf: url)
    dest_dir = tmp_path / 'Big-Q8_0'
    dest_dir.mkdir()
    files = [{'rfilename': 'Big-Q8_0/p-00001-of-00002.gguf',
              'dest': str(dest_dir / 'p-00001-of-00002.gguf')},
             {'rfilename': 'Big-Q8_0/p-00002-of-00002.gguf',
              'dest': str(dest_dir / 'p-00002-of-00002.gguf')}]
    app._save_model_job({'state': 'downloading', 'repo': 'org/repo',
                         'group': 'Big-Q8_0', 'dir': str(dest_dir),
                         'files': files, 'total': 2400, 'downloaded': 0,
                         'rate_mbps': 0})
    assert app.cli_llama_fetch() == 0
    job = app._load_model_job()
    assert job['state'] == 'done', job
    assert job['downloaded'] == 2400
    for f in files:
        assert os.path.getsize(f['dest']) == 1200
    assert not list(dest_dir.glob('*.partial'))


def test_worker_records_an_error_rather_than_hanging_at_downloading(tmp_path, monkeypatch):
    """A failure must leave a readable error state. A job stuck on `downloading`
    with a dead worker is the failure mode that strands the UI."""
    monkeypatch.setattr(app, 'MODEL_JOB_FILE', str(tmp_path / 'job.json'))
    monkeypatch.setattr(app, 'LLAMA_HF_FILE', str(tmp_path / 'hf.json'))
    monkeypatch.setattr(app, '_hf_resolve', lambda repo, rf: 'http://127.0.0.1:1/nope')
    app._save_model_job({'state': 'downloading', 'repo': 'org/repo', 'group': 'G',
                         'dir': str(tmp_path), 'rate_mbps': 0, 'total': 1,
                         'files': [{'rfilename': 'G/x.gguf',
                                    'dest': str(tmp_path / 'x.gguf')}]})
    assert app.cli_llama_fetch() == 1
    job = app._load_model_job()
    assert job['state'] == 'error' and job['error']


def test_worker_does_not_clobber_a_job_that_superseded_it(http_server, tmp_path, monkeypatch):
    """If a newer download replaced the job file while this worker was running,
    the old worker must exit quietly instead of writing `done` over the new
    job's state."""
    srv, url = http_server
    _RangeHandler.payload = b'X' * 10
    monkeypatch.setattr(app, 'MODEL_JOB_FILE', str(tmp_path / 'job.json'))
    monkeypatch.setattr(app, 'LLAMA_HF_FILE', str(tmp_path / 'hf.json'))
    monkeypatch.setattr(app, '_hf_resolve', lambda repo, rf: url)
    app._save_model_job({'state': 'downloading', 'repo': 'o/r', 'group': 'OLD',
                         'dir': str(tmp_path), 'rate_mbps': 0, 'total': 10,
                         'files': [{'rfilename': 'OLD/x.gguf',
                                    'dest': str(tmp_path / 'x.gguf')}]})

    real_fetch = app._fetch_file

    def fetch_then_supersede(*a, **k):
        n = real_fetch(*a, **k)
        # a newer pull takes over the job file mid-transfer
        app._save_model_job({'state': 'downloading', 'group': 'NEW',
                             'repo': 'o/r2', 'files': [], 'total': 99})
        return n
    monkeypatch.setattr(app, '_fetch_file', fetch_then_supersede)

    app.cli_llama_fetch()
    job = app._load_model_job()
    assert job['group'] == 'NEW'          # the newer job still owns the file
    assert job['state'] == 'downloading'  # NOT overwritten with the old one's 'done'


def test_llama_fetch_cli_is_registered():
    """The detached worker re-enters through `python app.py llama-fetch`; if the
    descriptor stops exporting it, every download breaks at spawn time."""
    from nexusdash.core import registry
    assert 'llama-fetch' in registry.cli_commands()

# ─── 06g — model backup / restore ───────────────────────────────────────

@pytest.fixture
def backup_env(tmp_path, monkeypatch):
    """A models dir with one split model and one loose gguf, plus a backup base."""
    models = tmp_path / 'models'
    base = tmp_path / 'share'
    (models / 'Big-Q8_0').mkdir(parents=True)
    (models / 'Big-Q8_0' / 'p-00001-of-00002.gguf').write_bytes(b'a' * 100)
    (models / 'Big-Q8_0' / 'p-00002-of-00002.gguf').write_bytes(b'b' * 50)
    (models / 'Small.gguf').write_bytes(b'c' * 10)
    base.mkdir()
    monkeypatch.setattr(app, 'LLAMA_MODELS_DIR', str(models))
    monkeypatch.setattr(app, 'LLAMA_HF_FILE', str(tmp_path / 'hf.json'))
    monkeypatch.setattr(app, 'BACKUP_JOB_FILE', str(tmp_path / 'bjob.json'))
    monkeypatch.setattr(app, 'BACKUP_DEFAULT_BASE', str(base))
    return {'models': models, 'base': base}


def test_unmounted_backup_target_is_refused(backup_env, monkeypatch):
    """THE guard. An unmounted mount point is an empty dir on the root disk, so
    copying there fills the system disk while looking like it went to the
    share. Observed live: /mnt/llm was a real NFS mount on one AI node and a
    bare empty directory on another."""
    monkeypatch.setattr(app, '_rsync_path', lambda: '/usr/bin/rsync')
    monkeypatch.setattr(app.os.path, 'ismount', lambda p: False)
    st = app._backup_status()
    assert st['mounted'] is False
    problem = app._backup_precondition(st)
    assert problem and 'not a mount point' in problem
    assert 'fill the local disk' in problem


def test_non_mounted_target_allowed_when_explicitly_opted_in(backup_env, monkeypatch, tmp_path):
    """The override exists because some people really do back up to local
    storage — but it has to be a deliberate choice, not the default."""
    monkeypatch.setattr(app, '_rsync_path', lambda: '/usr/bin/rsync')
    monkeypatch.setattr(app.os.path, 'ismount', lambda p: False)
    app.write_json_atomic(str(tmp_path / 'hf.json'), {'backup_allow_local': True}, 0o600)
    assert app._backup_precondition(app._backup_status()) is None


def test_missing_rsync_degrades_to_a_stated_refusal(backup_env, monkeypatch):
    """Two of three AI nodes shipped without rsync, so this is the common case,
    not a theoretical one."""
    monkeypatch.setattr(app, '_rsync_path', lambda: None)
    monkeypatch.setattr(app.os.path, 'ismount', lambda p: True)
    problem = app._backup_precondition(app._backup_status())
    assert problem and 'rsync is not installed' in problem


def test_backup_dir_is_confined(backup_env, monkeypatch):
    """The group reaches this from a URL path segment, so it is untrusted."""
    assert app._backup_dir('Big-Q8_0').endswith('/Big-Q8_0')
    assert app._backup_dir('../../etc') is None
    assert app._backup_dir('a/b') is None
    assert app._backup_dir('') is None


def test_local_groups_finds_split_dirs_and_loose_files(backup_env):
    groups = app._local_groups()
    assert set(groups) == {'Big-Q8_0', 'Small'}
    assert app._paths_bytes(groups['Big-Q8_0']) == 150      # both parts


def test_loose_split_shards_collapse_into_one_group(tmp_path, monkeypatch):
    """A split model stored as loose top-level files must be ONE backup unit.
    Real case from node4: gpt-oss-120b's three shards sat at the top level and
    were listed as three models, so backing up "shard 1" would have produced a
    13 MB backup of a 63 GB model — shard 1 genuinely is that small."""
    models = tmp_path / 'models'
    models.mkdir()
    stem = 'ggml-org_gpt-oss-120b-GGUF_gpt-oss-120b-mxfp4'
    for i, size in ((1, 13), (2, 300), (3, 290)):
        (models / ('%s-0000%d-of-00003.gguf' % (stem, i))).write_bytes(b'x' * size)
        (models / ('%s-0000%d-of-00003.gguf.etag' % (stem, i))).write_text('etag')
    monkeypatch.setattr(app, 'LLAMA_MODELS_DIR', str(models))
    groups = app._local_groups()
    assert list(groups) == [stem], groups          # one model, not three
    assert len(groups[stem]) == 3                  # all shards
    assert app._paths_bytes(groups[stem]) == 603   # the WHOLE model
    # the .etag sidecars are download bookkeeping, not model data
    assert all(p.endswith('.gguf') for p in groups[stem])


def test_backup_listing_reports_state_and_sizes(client, backup_env, monkeypatch):
    monkeypatch.setattr(app, '_rsync_path', lambda: '/usr/bin/rsync')
    monkeypatch.setattr(app.os.path, 'ismount', lambda p: True)
    root = backup_env['base'] / 'NexusDashboard' / 'Models' / 'Big-Q8_0'
    root.mkdir(parents=True)
    (root / 'p-00001-of-00002.gguf').write_bytes(b'a' * 100)
    app.write_json_atomic(str(root / app.BACKUP_MANIFEST),
                          {'group': 'Big-Q8_0', 'size_bytes': 150,
                           'source_node': 'node1', 'saved_at': 1}, 0o644)
    body = client.get('/api/llama/backups').get_json()
    assert body['problem'] is None
    b = [x for x in body['backups'] if x['group'] == 'Big-Q8_0'][0]
    assert b['size'] == 150 and b['source_node'] == 'node1'
    assert b['present_locally'] is True            # still in the models dir
    assert {g['group'] for g in body['local_groups']} == {'Big-Q8_0', 'Small'}


def test_backup_refuses_when_the_share_lacks_room(client, backup_env, monkeypatch):
    """Better a clear refusal than a dead partial copy of a 200GB model."""
    monkeypatch.setattr(app, '_rsync_path', lambda: '/usr/bin/rsync')
    monkeypatch.setattr(app.os.path, 'ismount', lambda p: True)
    monkeypatch.setattr(app, '_free_bytes', lambda p: 10)      # 10 bytes free
    r = client.post('/api/llama/backups/Big-Q8_0')
    assert r.status_code == 507
    assert 'Not enough space' in r.get_json()['error']


def test_backup_of_an_unknown_model_is_404(client, backup_env, monkeypatch):
    monkeypatch.setattr(app, '_rsync_path', lambda: '/usr/bin/rsync')
    monkeypatch.setattr(app.os.path, 'ismount', lambda p: True)
    assert client.post('/api/llama/backups/NotHere').status_code == 404


def test_interrupted_copy_reports_resumable(backup_env, monkeypatch):
    monkeypatch.setattr(app, '_pid_alive', lambda pid: False)
    v = app._backup_job_view({'state': 'running', 'pid': 4242, 'group': 'X'})
    assert v['state'] == 'interrupted' and 'resumes' in v['error']


def test_backup_worker_copies_and_writes_a_manifest(backup_env, monkeypatch):
    """End to end through the real rsync binary, if the box has one."""
    if not app._rsync_path():
        pytest.skip('rsync not installed here')
    src = str(backup_env['models'] / 'Big-Q8_0')
    dest = str(backup_env['base'] / 'NexusDashboard' / 'Models' / 'Big-Q8_0')
    os.makedirs(dest, exist_ok=True)
    app._save_backup_job({'state': 'running', 'op': 'backup', 'group': 'Big-Q8_0',
                          'srcs': [src], 'dest': dest, 'total': 150, 'done': 0})
    assert app.cli_llama_backup() == 0
    job = app._load_backup_job()
    assert job['state'] == 'done', job
    assert job['done'] == 150
    assert sorted(os.listdir(dest)) == [app.BACKUP_MANIFEST,
                                        'p-00001-of-00002.gguf',
                                        'p-00002-of-00002.gguf']
    man = json.loads(open(os.path.join(dest, app.BACKUP_MANIFEST)).read())
    assert man['group'] == 'Big-Q8_0' and man['size_bytes'] == 150


def test_restore_round_trips_the_model_back(backup_env, monkeypatch):
    if not app._rsync_path():
        pytest.skip('rsync not installed here')
    src = str(backup_env['models'] / 'Big-Q8_0')
    dest = str(backup_env['base'] / 'NexusDashboard' / 'Models' / 'Big-Q8_0')
    os.makedirs(dest, exist_ok=True)
    app._save_backup_job({'state': 'running', 'op': 'backup', 'group': 'Big-Q8_0',
                          'srcs': [src], 'dest': dest, 'total': 150, 'done': 0})
    app.cli_llama_backup()
    shutil.rmtree(src)                                  # lose the local copy
    app._save_backup_job({'state': 'running', 'op': 'restore', 'group': 'Big-Q8_0',
                          'srcs': [dest], 'dest': src, 'total': 150, 'done': 0})
    assert app.cli_llama_backup() == 0
    assert app._load_backup_job()['state'] == 'done'
    # the manifest describes the backup, not the model — it must NOT come back
    assert sorted(os.listdir(src)) == ['p-00001-of-00002.gguf',
                                       'p-00002-of-00002.gguf']
    # the model is loadable again: the parts are back, side by side
    assert os.path.getsize(os.path.join(src, 'p-00001-of-00002.gguf')) == 100


def test_worker_backs_up_loose_shards_into_one_directory(tmp_path, monkeypatch):
    """The multi-source rsync path: several loose shards must land side by side
    in the backup dir, which is what makes the restored model loadable."""
    if not app._rsync_path():
        pytest.skip('rsync not installed here')
    models = tmp_path / 'models'
    models.mkdir()
    stem = 'Split-Model-mxfp4'
    for i in (1, 2, 3):
        (models / ('%s-0000%d-of-00003.gguf' % (stem, i))).write_bytes(bytes([i]) * 40)
    monkeypatch.setattr(app, 'LLAMA_MODELS_DIR', str(models))
    monkeypatch.setattr(app, 'BACKUP_JOB_FILE', str(tmp_path / 'bjob.json'))
    dest = tmp_path / 'share' / stem
    dest.mkdir(parents=True)
    srcs = app._local_groups()[stem]
    app._save_backup_job({'state': 'running', 'op': 'backup', 'group': stem,
                          'srcs': srcs, 'dest': str(dest), 'total': 120, 'done': 0})
    assert app.cli_llama_backup() == 0
    job = app._load_backup_job()
    assert job['state'] == 'done', job
    got = sorted(f for f in os.listdir(str(dest)) if f.endswith('.gguf'))
    assert len(got) == 3 and got[0].endswith('-00001-of-00003.gguf')
    assert job['done'] >= 120


def test_llama_backup_cli_is_registered():
    from nexusdash.core import registry
    assert 'llama-backup' in registry.cli_commands()


# ─── 06c — in-memory tokens/sec derivation ──────────────────────────────

def test_llama_derive_rate(monkeypatch):
    app._llama_rate.update(ts=0.0, tokens=None)
    clock = [1000.0]
    monkeypatch.setattr(app.time, 'time', lambda: clock[0])
    # First sample: no prior -> no rate emitted, state primed
    r = {'metrics': {'tokens_predicted_total': 100}}
    app._llama_derive_rate(r)
    assert 'tokens_per_sec' not in r
    # +10s, +200 tokens -> 20 tok/s
    clock[0] = 1010.0
    r = {'metrics': {'tokens_predicted_total': 300}}
    app._llama_derive_rate(r)
    assert r['tokens_per_sec'] == 20.0
    # Counter went backwards (server restarted) -> interval skipped
    clock[0] = 1020.0
    r = {'metrics': {'tokens_predicted_total': 50}}
    app._llama_derive_rate(r)
    assert 'tokens_per_sec' not in r
    # Missing counter -> no crash, no rate
    r = {'metrics': {}}
    app._llama_derive_rate(r)
    assert 'tokens_per_sec' not in r
