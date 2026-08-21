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
    # create_app() ran at facade import: all dashboard modules registered.
    ids = [m['id'] for m in app.MODULES]
    assert ids == ['disks', 'zfs', 'lvm', 'mdraid', 'schedules', 'replication',
                   'maintenance', 'iscsi', 'nfs', 'smb', 'minidlna', 'llamacpp', 'gpu',
                   'instances', 'images', 'ctnetworks', 'portforward', 'docker',
                   'compose', 'firewall', 'caddy', 'dnsmasq', 'metrics',
                   'updates', 'nut', 'upsmon']
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


def test_cli_commands_first_registration_wins(monkeypatch):
    """CLI names are systemd-facing — a later-registered descriptor (e.g. an
    operator plugin) must never shadow an existing tick command."""
    fake = lambda: 99
    monkeypatch.setitem(registry._DESCRIPTORS, 'zztest',
                        {'id': 'zztest', 'cli': {'autosnap-tick': fake,
                                                 'zztest-tick': fake}})
    cmds = registry.cli_commands()
    assert cmds['autosnap-tick'] is not fake     # schedules' original kept
    assert cmds['zztest-tick'] is fake           # non-colliding name lands


def test_descriptor_order_keys_match_nav_sequence():
    """Every descriptor carries a unique int `order`, and registration order
    (== nav order) is exactly ascending-order — the invariant the discovery
    loader sorts by."""
    orders = [registry._DESCRIPTORS[m['id']].get('order') for m in app.MODULES]
    assert all(isinstance(o, int) for o in orders)
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)


def test_registry_sources_and_reset(fresh_registry):
    """reset() (via the fixture) clears every registry structure in place;
    register_module records its source."""
    assert registry.MODULES == [] and registry.MODULE_IDS == set()
    assert registry.DEFAULT_OFF == set() and registry._DESCRIPTORS == {}
    assert registry._BP_TO_MODULE == {} and registry._LOADED == set()
    assert registry._SOURCES == {}
    registry.register_module({'id': 'x', 'label': 'X', 'category': 'C',
                              'blueprint': None}, source='plugin')
    assert registry._SOURCES == {'x': 'plugin'}


def test_module_hooks_skip_disabled(monkeypatch):
    calls = []
    desc = registry._DESCRIPTORS['zfs']
    monkeypatch.setitem(desc, 'alerts', lambda: calls.append('zfs') or [{'key': 'k', 'message': 'm'}])
    # firewall, dnsmasq, nut and upsmon ship real alerts hooks — disable them
    # so only the injected zfs hook is in play.
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'firewall', 'dnsmasq', 'nut', 'upsmon'})
    got = list(registry.module_hooks('alerts'))
    assert [mid for mid, _ in got] == ['zfs']
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'zfs', 'firewall', 'dnsmasq', 'nut', 'upsmon'})
    assert list(registry.module_hooks('alerts')) == []
    monkeypatch.delitem(desc, 'alerts')


def test_finalize_idempotent_and_seed_ordered():
    """finalize() rebuilds the merged tables in place; running it again must
    be a no-op, and SYSTEM_SERVICES iteration order must equal the seed order
    (byte-significant for the /metrics text exposition)."""
    from nexusdash.core import services as svc
    before_services = {k: dict(v) for k, v in app.SYSTEM_SERVICES.items()}
    before_tasks = [dict(t) for t in app.MANAGED_TASKS]
    services_obj, tasks_obj = app.SYSTEM_SERVICES, app.MANAGED_TASKS
    registry.finalize()
    assert app.SYSTEM_SERVICES is services_obj      # in-place, never rebound
    assert app.MANAGED_TASKS is tasks_obj
    assert {k: dict(v) for k, v in app.SYSTEM_SERVICES.items()} == before_services
    assert [dict(t) for t in app.MANAGED_TASKS] == before_tasks
    # The seed list is the byte-significant PREFIX; services contributed by
    # modules registered after it follow in registration order.
    seed = svc._SERVICE_SEED_ORDER
    keys = tuple(app.SYSTEM_SERVICES)
    assert keys[:len(seed)] == seed
    assert keys[len(seed):] == ('nut', 'upsmon')
    assert [t['id'] for t in app.MANAGED_TASKS] == [
        'autosnap', 'replicate', 'alerts', 'maintenance', 'history']


def test_dnsmasq_history_contribution_wired():
    """dnsmasq's history sampler + metric allowlist ride its descriptor now
    (the hand-written core wiring is gone)."""
    from nexusdash.modules import dnsmasq
    assert registry._DESCRIPTORS['dnsmasq']['history'] is dnsmasq.collect_history_samples
    assert {'dns_hits', 'dns_misses', 'dns_cache_size',
            'dhcp_leases'} <= app.HISTORY_METRICS


def test_history_hook_skips_disabled_module(monkeypatch):
    """module_hooks('history') skipping disabled modules replaces the old
    hand-written 'dnsmasq in load_disabled_modules' guard in history.py."""
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    assert 'dnsmasq' in [mid for mid, _ in registry.module_hooks('history')]
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'dnsmasq'})
    assert 'dnsmasq' not in [mid for mid, _ in registry.module_hooks('history')]


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


def test_install_status_survives_disabled_minidlna(client, monkeypatch):
    """Regression: /api/install/status reports on EVERY service but used to
    live in the minidlna blueprint — disabling that module 403'd it and the
    Services page died with 'module minidlna is disabled on this node'."""
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'minidlna'})
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    r = client.get('/api/install/status')
    assert r.status_code == 200
    body = r.get_json()
    assert 'minidlna' in body and 'zfs' in body   # still reports every service


def test_status_and_install_flag_disabled_modules(client, monkeypatch):
    """Entries are never omitted (the controller polls /api/status) but carry
    module_disabled so the Services page can hide intentionally-off rows and
    skip them in the not-installed nag."""
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'zfs', 'minidlna'})
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('inactive', '', 3))
    j = client.get('/api/status').get_json()
    assert j['zfs']['module_disabled'] is True
    assert j['smb']['module_disabled'] is False
    j = client.get('/api/install/status').get_json()
    assert j['minidlna']['module_disabled'] is True
    assert j['smb']['module_disabled'] is False
    assert 'zfs' in j and 'minidlna' in j        # still reports every service


def test_disabled_module_routes_still_registered(tmp_path, monkeypatch, fresh_registry):
    """A module disabled at boot is declared AND has routes (runtime-gated 403,
    not 404) so enabling it from the Modules page works without a restart.
    fresh_registry resets ALL registry globals in place (the old hand-rolled
    monkeypatch rebinds missed DEFAULT_OFF and desynchronized the facade)."""
    mf = tmp_path / 'modules.json'
    mf.write_text(json.dumps({'disabled': ['gpu']}))
    monkeypatch.setattr(registry, 'MODULES_FILE', str(mf))
    import nexusdash
    fresh = nexusdash.create_app()
    rules = {r.rule for r in fresh.url_map.iter_rules()}
    assert '/api/gpu' in rules                           # registered despite toggle
    assert '/api/zfs/pools' in rules
    assert 'gpu' in {m['id'] for m in registry.MODULES}
    assert 'gpu' in registry._LOADED


def test_metrics_hook_appends_exposition_lines(client, monkeypatch):
    """The descriptor `metrics` hook appends complete exposition lines after
    the inline families (zero builtin producers -> byte-identical without)."""
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    monkeypatch.setitem(registry._DESCRIPTORS['docker'], 'metrics',
                        lambda: ['# HELP x_up test', '# TYPE x_up gauge', 'x_up 1'])
    text = client.get('/metrics').get_data(as_text=True)
    assert text.endswith('# HELP x_up test\n# TYPE x_up gauge\nx_up 1\n')


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
