"""Feature 02 — GPU monitoring parsers.

Pure tests for the nvidia-smi CSV and rocm-smi JSON parsers, normalized to a
common shape. Real rocm-smi output from an AMD/ROCm host (.73) drives the AMD
cases. No GPU or tooling needed to run these.
"""
import pytest
import app


ROCM_JSON = (
    '{"card0": {"Temperature (Sensor edge) (C)": "35.0", '
    '"Temperature (Sensor junction) (C)": "36.0", '
    '"Average Graphics Package Power (W)": "19.0", "GPU use (%)": "3", '
    '"GPU Memory Allocated (VRAM%)": "85", "Card Series": "N/A", '
    '"Card Model": "0x7551", "Card SKU": "APM107573", "GFX Version": "gfx1201", '
    '"VRAM Total Memory (B)": "34208743424", '
    '"VRAM Total Used Memory (B)": "29225381888"}, '
    '"card1": {"Temperature (Sensor edge) (C)": "39.0", '
    '"Current Socket Graphics Package Power (W)": "12.134", "GPU use (%)": "0", '
    '"GPU Memory Allocated (VRAM%)": "1", "Card SKU": "PHXGENERIC", '
    '"GFX Version": "gfx1103", "VRAM Total Memory (B)": "4294967296", '
    '"VRAM Total Used Memory (B)": "72495104"}}'
)


def test_rocm_parse_two_cards():
    gpus = app._parse_rocm_smi(ROCM_JSON)
    assert len(gpus) == 2
    a, b = gpus
    assert a['index'] == 0 and b['index'] == 1
    assert a['vendor'] == 'amd'


def test_rocm_normalized_values():
    a = app._parse_rocm_smi(ROCM_JSON)[0]
    assert a['util'] == 3.0
    assert a['mem_pct'] == 85.0
    assert a['mem_used'] == 29225381888
    assert a['mem_total'] == 34208743424
    assert a['temp'] == 36.0          # junction preferred over edge
    assert a['power'] == 19.0
    assert 'gfx1201' in a['name'] and 'APM107573' in a['name']


def test_rocm_name_falls_back_to_gfx_when_series_na():
    # card0 Series is "N/A" -> SKU used, gfx appended
    a = app._parse_rocm_smi(ROCM_JSON)[0]
    assert a['name'] == 'APM107573 (gfx1201)'


def test_rocm_power_alt_key():
    # card1 reports "Current Socket Graphics Package Power (W)"
    b = app._parse_rocm_smi(ROCM_JSON)[1]
    assert b['power'] == 12.134


def test_rocm_mem_pct_computed_when_absent():
    j = ('{"card0": {"GPU use (%)": "10", "VRAM Total Memory (B)": "1000", '
         '"VRAM Total Used Memory (B)": "250"}}')
    g = app._parse_rocm_smi(j)[0]
    assert g['mem_pct'] == 25.0


def test_rocm_bad_json_returns_empty():
    assert app._parse_rocm_smi('not json') == []
    assert app._parse_rocm_smi('') == []
    assert app._parse_rocm_smi('[1,2,3]') == []


def test_nvidia_parse():
    csv = '0, NVIDIA GeForce RTX 4090, 45, 8192, 24576, 61, 210.5'
    g = app._parse_nvidia_smi(csv)[0]
    assert g['index'] == 0
    assert g['vendor'] == 'nvidia'
    assert g['name'] == 'NVIDIA GeForce RTX 4090'
    assert g['util'] == 45.0
    assert g['mem_used'] == 8192 * 1024 * 1024
    assert g['mem_total'] == 24576 * 1024 * 1024
    assert g['mem_pct'] == 33.3
    assert g['temp'] == 61.0
    assert g['power'] == 210.5


def test_nvidia_multi_and_short_lines():
    csv = ('0, A, 10, 100, 200, 50, 30\n'
           'garbage line\n'
           '1, B, 20, 50, 200, 55, 40')
    gpus = app._parse_nvidia_smi(csv)
    assert [g['index'] for g in gpus] == [0, 1]


def test_nvidia_handles_na_values():
    csv = '0, GPU, [N/A], [N/A], [N/A], [N/A], [N/A]'
    g = app._parse_nvidia_smi(csv)[0]
    assert g['util'] is None and g['mem_pct'] is None and g['power'] is None


def test_gpu_vendor_none_when_no_tools(monkeypatch):
    monkeypatch.setattr(app.shutil, 'which', lambda _n: None)
    monkeypatch.setattr(app, '_GPU_TOOL_DIRS', ())
    assert app._gpu_vendor() is None


def test_gpu_tool_falls_back_to_known_dirs(monkeypatch, tmp_path):
    # TheRock ROCm on amd-halo: /opt/rocm/bin is on PATH only for login
    # shells, so which() misses it under systemd — the dir fallback must hit.
    monkeypatch.setattr(app.shutil, 'which', lambda _n: None)
    tool = tmp_path / 'rocm-smi'
    tool.write_text('#!/bin/sh\n')
    tool.chmod(0o755)
    monkeypatch.setattr(app, '_GPU_TOOL_DIRS', (str(tmp_path),))
    assert app._gpu_tool('rocm-smi') == str(tool)
    assert app._gpu_tool('nvidia-smi') is None
    assert app._gpu_vendor() == 'amd'


def test_gpu_history_samples_emit_per_gpu(monkeypatch):
    monkeypatch.setattr(app, '_gpu_snapshot', lambda *a, **k: {
        'available': True, 'vendor': 'amd',
        'gpus': [{'index': 0, 'util': 3.0, 'mem_pct': 85.0, 'temp': 36.0},
                 {'index': 1, 'util': 0.0, 'mem_pct': 1.0, 'temp': 39.0}]})
    rows = app._gpu_history_samples()
    assert ('gpu_util', 'gpu0', 3.0) in rows
    assert ('gpu_mem_pct', 'gpu1', 1.0) in rows
    assert ('gpu_temp', 'gpu0', 36.0) in rows
    # labels used are valid history labels
    for _m, lbl, _v in rows:
        assert app.RE_HISTORY_LABEL.match(lbl)


def test_gpu_history_samples_empty_when_no_gpu(monkeypatch):
    monkeypatch.setattr(app, '_gpu_snapshot', lambda *a, **k: {'available': False, 'gpus': []})
    assert app._gpu_history_samples() == []


def test_gpu_module_registered():
    assert 'gpu' in app.MODULE_IDS


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── GPU tunables (power cap + power profile) ───────────────────────────
# Fixtures are the real amd-smi JSON from node6's Radeon AI PRO R9700.

AMD_STATIC = [{
    'gpu': 0,
    'asic': {'market_name': 'AMD Radeon AI PRO R9700'},
    'board': {'product_name': 'Navi 48 [Radeon AI PRO R9700]'},
    'limit': {
        'ppt0': {'max_power_limit': {'value': 300, 'unit': 'W'},
                 'min_power_limit': {'value': 210, 'unit': 'W'},
                 'socket_power_limit': {'value': 211, 'unit': 'W'}},
        'ppt1': {'max_power_limit': 'N/A', 'min_power_limit': 'N/A',
                 'socket_power_limit': 'N/A'},
        'slowdown_hotspot_temperature': {'value': 110, 'unit': 'C'},
        'shutdown_hotspot_temperature': {'value': 115, 'unit': 'C'}},
    'profile': {'available_profiles': ['CUSTOM', 'VIDEO', 'POWER_SAVING', 'COMPUTE',
                                       'VR', '3D_FULL_SCREEN', 'BOOTUP_DEFAULT'],
                'current': 'BOOTUP_DEFAULT', 'num_profiles': 24},
    # every one of these is N/A on this silicon — the reason partitioning is
    # not offered at all
    'soc_pstate': 'N/A', 'mem_carveout': 'N/A', 'xgmi_plpd': 'N/A',
}]
AMD_METRIC = [{'gpu': 0, 'power': {'socket_power': {'value': 21, 'unit': 'W'},
                                   'throttle_status': 'THROTTLED',
                                   'power_management': 'ENABLED'}}]


@pytest.fixture
def amd_gpu(monkeypatch):
    monkeypatch.setattr(app, '_gpu_tool', lambda n: '/opt/rocm/bin/' + n if n == 'amd-smi' else None)
    monkeypatch.setattr(app, '_gpu_vendor', lambda: 'amd')
    monkeypatch.setattr(app, '_amd_smi_json',
                        lambda args: AMD_STATIC if 'static' in args else AMD_METRIC)
    app._gpu_tun_cache['ts'] = 0.0
    yield
    app._gpu_tun_cache['ts'] = 0.0


def test_amd_tunables_normalize_from_real_output(amd_gpu):
    g = app._gpu_tunables(force=True)['gpus'][0]
    assert g['name'] == 'AMD Radeon AI PRO R9700'
    assert g['power_cap'] == {'current': 211.0, 'min': 210.0, 'max': 300.0, 'unit': 'W'}
    assert g['power_now'] == 21.0 and g['throttled'] is True
    assert g['profile']['current'] == 'BOOTUP_DEFAULT'
    assert 'COMPUTE' in g['profile']['available']
    assert sorted(g['settable']) == ['power_cap', 'profile']
    assert g['slowdown_temp'] == 110.0


def test_power_cap_outside_the_cards_own_range_is_refused(client, amd_gpu, monkeypatch):
    """The range comes from the CARD, not from a constant we picked."""
    called = []
    monkeypatch.setattr(app, '_run_gpu_helper', lambda *a: called.append(a))
    monkeypatch.setattr(app.os.path, 'exists', lambda p: True)
    for bad in (209, 301, 0):
        r = client.post('/api/gpu/0/power-cap', json={'watts': bad})
        assert r.status_code == 400, bad
        assert '210 and 300' in r.get_json()['error']
    assert called == []                      # never reached the helper


def test_power_cap_within_range_reaches_the_helper(client, amd_gpu, monkeypatch):
    import flask
    seen = {}

    def fake_helper(*args):
        seen['args'] = args
        return flask.jsonify({'success': True})
    monkeypatch.setattr(app, '_run_gpu_helper', fake_helper)
    r = client.post('/api/gpu/0/power-cap', json={'watts': 250})
    assert r.status_code == 200
    assert seen['args'] == ('power-cap', 0, 250)


def test_non_numeric_power_cap_is_refused(client, amd_gpu, monkeypatch):
    monkeypatch.setattr(app, '_run_gpu_helper', lambda *a: None)
    assert client.post('/api/gpu/0/power-cap', json={'watts': '250; reboot'}).status_code == 400
    assert client.post('/api/gpu/0/power-cap', json={}).status_code == 400


def test_profile_must_be_one_the_card_offers(client, amd_gpu, monkeypatch):
    called = []
    monkeypatch.setattr(app, '_run_gpu_helper', lambda *a: called.append(a))
    r = client.post('/api/gpu/0/profile', json={'profile': 'TURBO'})
    assert r.status_code == 400 and 'does not offer' in r.get_json()['error']
    r = client.post('/api/gpu/0/profile', json={'profile': 'rm -rf /'})
    assert r.status_code == 400 and 'Invalid profile' in r.get_json()['error']
    assert called == []


def test_unknown_gpu_index_is_404(client, amd_gpu, monkeypatch):
    monkeypatch.setattr(app, '_run_gpu_helper', lambda *a: None)
    assert client.post('/api/gpu/7/power-cap', json={'watts': 250}).status_code == 404


def test_render_group_problem_is_reported_not_swallowed(monkeypatch):
    """rocm-smi telemetry works from sysfs while amd-smi needs /dev/kfd, so this
    is the exact state a node lands in before the service user joins `render`."""
    monkeypatch.setattr(app, '_gpu_vendor', lambda: 'amd')
    monkeypatch.setattr(app, '_gpu_tool', lambda n: '/opt/rocm/bin/amd-smi' if n == 'amd-smi' else None)
    monkeypatch.setattr(app, '_amd_smi_json', lambda args: None)
    d = app._gpu_tunables(force=True)
    assert d['gpus'] == []
    assert 'render' in d['problem']


def test_nvidia_tunables_shape_matches_amd(monkeypatch):
    """No NVIDIA card exists in this fleet, so the parser is pinned to captured
    output — and must produce the SAME shape so the UI needs no vendor branch."""
    csv = '0, NVIDIA RTX PRO 6000, 300.00, 100.00, 600.00, 450.00, 55.12\n'
    monkeypatch.setattr(app, '_gpu_vendor', lambda: 'nvidia')
    monkeypatch.setattr(app, '_gpu_tool', lambda n: '/usr/bin/nvidia-smi' if n == 'nvidia-smi' else None)
    monkeypatch.setattr(app, 'run', lambda *a, **k: (csv, '', 0))
    g = app._gpu_tunables(force=True)['gpus'][0]
    assert g['vendor'] == 'nvidia' and g['name'] == 'NVIDIA RTX PRO 6000'
    assert g['power_cap']['min'] == 100.0 and g['power_cap']['max'] == 600.0
    assert g['power_cap']['default'] == 450.0
    assert g['settable'] == ['power_cap']
    assert g['profile'] == {'current': None, 'available': []}   # shape parity


# node4's Ryzen AI APU: amd-smi answers unsupported features with an ERROR
# STRING rather than a dict, and reports no power limits at all. Verbatim from
# that card — it returned a 500 from the tunables endpoint until handled.
AMD_APU_STATIC = [{
    'gpu': 0,
    'asic': {'market_name': 'AMD Radeon Graphics'},
    'limit': {'ppt0': {'max_power_limit': 'N/A', 'min_power_limit': 'N/A',
                       'socket_power_limit': 'N/A'},
              'ppt1': {'max_power_limit': 'N/A', 'min_power_limit': 'N/A',
                       'socket_power_limit': 'N/A'}},
    'profile': 'AMDSMI_STATUS_NOT_SUPPORTED - Feature not supported',
    'mem_carveout': 'N/A',
}]


def test_card_without_tunables_reports_none_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(app, '_gpu_vendor', lambda: 'amd')
    monkeypatch.setattr(app, '_gpu_tool', lambda n: '/opt/rocm/bin/amd-smi' if n == 'amd-smi' else None)
    monkeypatch.setattr(app, '_amd_smi_json',
                        lambda args: AMD_APU_STATIC if 'static' in args else [])
    app._gpu_tun_cache['ts'] = 0.0
    g = app._gpu_tunables(force=True)['gpus'][0]
    assert g['name'] == 'AMD Radeon Graphics'
    assert g['settable'] == []                       # nothing offered, no controls drawn
    assert g['power_cap']['current'] is None
    assert g['profile']['available'] == []
    app._gpu_tun_cache['ts'] = 0.0


def test_tunables_endpoint_survives_a_card_with_no_tunables(client, monkeypatch):
    monkeypatch.setattr(app, '_gpu_vendor', lambda: 'amd')
    monkeypatch.setattr(app, '_gpu_tool', lambda n: '/opt/rocm/bin/amd-smi' if n == 'amd-smi' else None)
    monkeypatch.setattr(app, '_amd_smi_json',
                        lambda args: AMD_APU_STATIC if 'static' in args else [])
    app._gpu_tun_cache['ts'] = 0.0
    r = client.get('/api/gpu/tunables')
    assert r.status_code == 200                      # was a 500 on node4
    app._gpu_tun_cache['ts'] = 0.0
