"""Byte-identity characterization for the 3.0.0 discovery refactor.

Golden files pin the exact serialized responses of the controller-facing
endpoints with every system access stubbed deterministically, so any drift the
refactor introduces — key changes, entry order in the plain-text /metrics
exposition, /api/tasks list order, dropped fields — fails byte-for-byte.

Volatile-by-design fields are masked before comparison (documented per test):
the /api/summary `system` block (hostname/uptime/ip) and /api/me's
`fqdn`/`version` (version bumps intentionally at release).

Regenerate ONLY for an intentional contract change:

    GOLDEN_UPDATE=1 ./venv/bin/python -m pytest tests/test_byte_identity.py

Goldens are generated with FAMILY == 'debian' (dev box + CI); the FAMILY-
dependent tests skip elsewhere rather than fail on rpm-flavored pkg names.
"""
import json
import os
from pathlib import Path

import pytest

import app

GOLDEN_DIR = Path(__file__).parent / 'goldens'
_UPDATE = os.environ.get('GOLDEN_UPDATE') == '1'


class _NoPath:
    """pathlib.Path stand-in whose exists() is always False, so 'installed'
    flags never depend on what the machine running the suite has on disk."""
    def __init__(self, *parts):
        pass

    def exists(self):
        return False


_FIXED_RESOURCES = {
    'uptime_seconds': 4242,
    'load': {'1': 0.5, '5': 0.25, '15': 0.125},
    'cpus': 4,
    'cpu_pct': 12.5,
    'memory': {'total': 8 * 1024**3, 'available': 6 * 1024**3,
               'used': 2 * 1024**3, 'pct': 25.0},
    'swap': {'total': 1024**3, 'used': 0, 'pct': 0.0},
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Every command fails cleanly and identically (services inactive, empty
    # parses); identity is a fixed admin; no module is disabled; nothing on
    # the local disk leaks into 'installed' flags or share/export counts.
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(app, 'MODULES_FILE', str(tmp_path / 'modules.json'))
    monkeypatch.setattr(app, '_unit_present', lambda unit: False)
    monkeypatch.setattr(app, 'Path', _NoPath)
    monkeypatch.setattr(app, '_system_resources', lambda: dict(_FIXED_RESOURCES))
    monkeypatch.setattr(app, 'parse_exports', lambda *a, **k: [])
    monkeypatch.setattr(app, 'smbconf_parse', lambda *a, **k: [])
    monkeypatch.setattr(app, '_smart_health_ok', lambda *a, **k: None)
    monkeypatch.setattr(app, '_fs_alerts', lambda: [])
    monkeypatch.setattr(app, '_md_alerts', lambda: [])
    monkeypatch.setattr(app, '_mdadm_conf_arrays', lambda: [])
    monkeypatch.setattr(app, '_users', lambda: {})
    # Registry hook contributors hold module-level fns captured in the
    # descriptor dicts, so facade patches don't reach them — stub the
    # descriptor entries directly (test_registry.py precedent). Deterministic
    # stand-ins keep the hook DISPATCH itself under golden coverage.
    monkeypatch.setitem(app._DESCRIPTORS['firewall'], 'alerts', lambda: [])
    monkeypatch.setitem(app._DESCRIPTORS['dnsmasq'], 'alerts', lambda: [])
    monkeypatch.setitem(app._DESCRIPTORS['docker'], 'summary',
                        lambda: {'golden': 'docker'})
    monkeypatch.setitem(app._DESCRIPTORS['dnsmasq'], 'summary',
                        lambda: {'golden': 'dnsmasq'})
    # The NUT pair reads config off disk and shells out to `upsc`; stub both
    # hooks so the goldens never depend on whether the machine running the
    # suite happens to have NUT installed.
    monkeypatch.setitem(app._DESCRIPTORS['nut'], 'alerts', lambda: [])
    monkeypatch.setitem(app._DESCRIPTORS['upsmon'], 'alerts', lambda: [])
    monkeypatch.setitem(app._DESCRIPTORS['nut'], 'summary',
                        lambda: {'golden': 'nut'})
    monkeypatch.setitem(app._DESCRIPTORS['upsmon'], 'summary',
                        lambda: {'golden': 'upsmon'})
    app.app.config['TESTING'] = True
    return app.app.test_client()


def _check(name, payload_bytes):
    """Compare against (or with GOLDEN_UPDATE=1, rewrite) a golden file."""
    path = GOLDEN_DIR / name
    if _UPDATE:
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_bytes(payload_bytes)
        return
    assert path.exists(), (
        f'missing golden {name} — generate with GOLDEN_UPDATE=1')
    golden = path.read_bytes()
    assert payload_bytes == golden, (
        f'{name} drifted from golden.\n--- golden ---\n'
        f'{golden.decode(errors="replace")}\n--- current ---\n'
        f'{payload_bytes.decode(errors="replace")}')


def _masked_json(resp, mask_keys):
    """Parse a JSON response, replace named top-level keys with a sentinel,
    re-serialize canonically (sorted keys, tight separators)."""
    data = json.loads(resp.get_data(as_text=True))
    for k in mask_keys:
        assert k in data, f'expected volatile key {k!r} present'
        data[k] = '__MASKED__'
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode()


def test_api_status_bytes(client):
    if app.FAMILY != 'debian':
        pytest.skip('goldens generated on debian-family')
    r = client.get('/api/status')
    assert r.status_code == 200
    _check('api_status.json', r.get_data())


def test_api_install_status_bytes(client):
    if app.FAMILY != 'debian':
        pytest.skip('goldens generated on debian-family')
    r = client.get('/api/install/status')
    assert r.status_code == 200
    _check('api_install_status.json', r.get_data())


def test_api_modules_bytes(client):
    r = client.get('/api/modules')
    assert r.status_code == 200
    _check('api_modules.json', r.get_data())


def test_api_tasks_bytes(client):
    r = client.get('/api/tasks')
    assert r.status_code == 200
    _check('api_tasks.json', r.get_data())


def test_api_summary_bytes(client):
    r = client.get('/api/summary')
    assert r.status_code == 200
    # `system` carries hostname/uptime/primary-IP — volatile by design.
    _check('api_summary.json', _masked_json(r, ['system']))


def test_api_me_bytes(client):
    r = client.get('/api/me')
    assert r.status_code == 200
    # fqdn is host-dependent; version bumps intentionally at releases.
    _check('api_me.json', _masked_json(r, ['fqdn', 'version']))


def test_metrics_bytes(client):
    if app.FAMILY != 'debian':
        pytest.skip('goldens generated on debian-family')
    r = client.get('/metrics')
    assert r.status_code == 200
    # Plain text: line ORDER is byte-significant (unlike sorted-key JSON) —
    # this golden is the primary guard for SYSTEM_SERVICES iteration order.
    _check('metrics.txt', r.get_data())
