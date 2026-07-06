"""Stage 2 registry: descriptors, hook dispatch, and HARD module disable.

Includes the first integration tests (Flask test client) of the security core:
always-registered routes with the runtime 403 gate, and the service-management
carve-out.
"""
import json
import importlib

import pytest

import app
from nexusdash.core import registry


def test_descriptors_registered_and_derived():
    # create_app() ran at facade import: all 13 dashboard modules registered.
    ids = [m['id'] for m in app.MODULES]
    assert ids == ['disks', 'zfs', 'lvm', 'mdraid', 'schedules', 'replication',
                   'maintenance', 'iscsi', 'nfs', 'smb', 'minidlna', 'llamacpp', 'gpu',
                   'instances', 'images', 'ctnetworks', 'portforward', 'docker',
                   'compose', 'firewall', 'metrics']
    assert app.MODULE_IDS == set(ids)
    # The containers group registered with the right nav category (split
    # from a shared 'Containers' bucket when the Docker module landed, so
    # the sidebar demarcates LXD pages from Docker pages).
    cats = {m['id']: m['category'] for m in app.MODULES}
    assert all(cats[i] == 'LXD / Incus' for i in
               ('instances', 'images', 'ctnetworks', 'portforward'))
    assert cats['docker'] == 'Docker'
    # register_module is idempotent — re-registering must not duplicate.
    before = len(app.MODULES)
    registry.register_module({'id': 'zfs', 'label': 'x', 'category': 'x', 'blueprint': None})
    assert len(app.MODULES) == before


def test_module_for_endpoint_mapping():
    assert registry.module_for_endpoint('zfs.zfs_pools') == 'zfs'
    assert registry.module_for_endpoint('llama.llama_get') == 'llamacpp'   # bp name != id
    # Core / system endpoints are never module-gated.
    for ep in ('auth.api_login', 'summary.api_summary', 'svc.service_start',
               'logs.logs_query', 'network.network_get', 'registry.modules_get',
               'static', None):
        assert registry.module_for_endpoint(ep) is None


def test_cli_commands_derived_from_descriptors():
    cmds = registry.cli_commands()
    assert set(cmds) >= {'autosnap-tick', 'replicate-tick', 'maintenance-tick'}


def test_module_hooks_skip_disabled(monkeypatch):
    calls = []
    desc = registry._DESCRIPTORS['zfs']
    monkeypatch.setitem(desc, 'alerts', lambda: calls.append('zfs') or [{'key': 'k', 'message': 'm'}])
    # firewall ships a real alerts hook — disable it so only the injected
    # zfs hook is in play.
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'firewall'})
    got = list(registry.module_hooks('alerts'))
    assert [mid for mid, _ in got] == ['zfs']
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'zfs', 'firewall'})
    assert list(registry.module_hooks('alerts')) == []
    monkeypatch.delitem(desc, 'alerts')


# ─── Integration: the hard-disable gate through a real test client ──────

@pytest.fixture
def client(monkeypatch):
    # Authenticated admin identity without touching auth.json.
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    app.app.config['TESTING'] = True
    return app.app.test_client()


def test_runtime_gate_blocks_disabled_module(client, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'zfs'})
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 0))
    r = client.get('/api/zfs/pools')
    assert r.status_code == 403
    assert "module 'zfs' is disabled" in r.get_json()['error']


def test_runtime_gate_open_when_enabled(client, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 0))
    assert client.get('/api/zfs/pools').status_code == 200


def test_service_management_carveout(client, monkeypatch):
    """A disabled module's daemon must still be controllable (core carve-out)."""
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'minidlna', 'zfs'})
    monkeypatch.setattr(app, 'run_safe', lambda *a, **k: {'success': True, 'stdout': '', 'stderr': '', 'returncode': 0})
    r = client.post('/api/service/minidlna/stop')
    assert r.status_code == 200
    # /api/status (Services page) also stays reachable.
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('inactive', '', 3))
    assert client.get('/api/status').status_code == 200


def test_disabled_module_routes_still_registered(tmp_path, monkeypatch):
    """A module disabled at boot is declared AND has routes (runtime-gated 403,
    not 404) so enabling it from the Modules page works without a restart."""
    mf = tmp_path / 'modules.json'
    mf.write_text(json.dumps({'disabled': ['gpu']}))
    monkeypatch.setattr(registry, 'MODULES_FILE', str(mf))
    # Fresh registry state + fresh app build.
    monkeypatch.setattr(registry, '_DESCRIPTORS', {})
    monkeypatch.setattr(registry, '_BP_TO_MODULE', {})
    monkeypatch.setattr(registry, '_LOADED', set())
    monkeypatch.setattr(registry, 'MODULES', [])
    monkeypatch.setattr(registry, 'MODULE_IDS', set())
    import nexusdash
    fresh = nexusdash.create_app()
    rules = {r.rule for r in fresh.url_map.iter_rules()}
    assert '/api/gpu' in rules                           # registered despite toggle
    assert '/api/zfs/pools' in rules
    assert 'gpu' in {m['id'] for m in registry.MODULES}
    assert 'gpu' in registry._LOADED


def test_modules_save_never_needs_restart(client, monkeypatch, tmp_path):
    mf = tmp_path / 'modules.json'
    monkeypatch.setattr(app, 'MODULES_FILE', str(mf))
    # Toggles apply live in both directions — restart_recommended stays False
    # (the key is kept for callers built against the old boot-skip behavior).
    r = client.post('/api/modules', json={'id': 'zfs', 'enabled': False})
    assert r.status_code == 200
    assert r.get_json()['restart_recommended'] is False
    assert json.loads(mf.read_text())['disabled'] == ['zfs']
    r = client.post('/api/modules', json={'id': 'zfs', 'enabled': True})
    assert r.get_json()['restart_recommended'] is False
    assert json.loads(mf.read_text())['disabled'] == []


def test_default_off_module_save_uses_enabled_list(client, monkeypatch, tmp_path):
    # Enabling a default-off module (metrics) persists a positive `enabled`
    # opt-in and flips the live disabled set; disabling clears the opt-in.
    mf = tmp_path / 'modules.json'
    monkeypatch.setattr(app, 'MODULES_FILE', str(mf))
    assert 'metrics' in app.load_disabled_modules()          # off by default
    client.post('/api/modules', json={'id': 'metrics', 'enabled': True})
    assert json.loads(mf.read_text())['enabled'] == ['metrics']
    assert 'metrics' not in app.load_disabled_modules()      # now on
    client.post('/api/modules', json={'id': 'metrics', 'enabled': False})
    assert json.loads(mf.read_text())['enabled'] == []
    assert 'metrics' in app.load_disabled_modules()          # off again
