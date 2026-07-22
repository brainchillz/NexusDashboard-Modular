"""Feature-module toggle tests — load_disabled_modules filtering and the
module/id catalog. Disabling a module only hides it from the nav (no data risk),
but the persisted state must round-trip cleanly and ignore stale/unknown ids.
"""
import json
import app


def test_modules_catalog_ids_are_unique_and_known():
    ids = [m['id'] for m in app.MODULES]
    assert len(ids) == len(set(ids))            # no duplicates
    assert app.MODULE_IDS == set(ids)
    # Every module carries the fields the UI renders.
    for m in app.MODULES:
        assert m['id'] and m['label'] and m['category']


# Default-off modules (e.g. the Prometheus /metrics endpoint) are reported as
# disabled until the operator explicitly enables them, so every expectation
# below folds in app.DEFAULT_OFF.
def test_load_disabled_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'MODULES_FILE', str(tmp_path / 'nope.json'))
    assert app.load_disabled_modules() == set(app.DEFAULT_OFF)


def test_load_disabled_reads_known_ids(tmp_path, monkeypatch):
    p = tmp_path / 'modules.json'
    p.write_text(json.dumps({'disabled': ['lvm', 'nfs']}))
    monkeypatch.setattr(app, 'MODULES_FILE', str(p))
    assert app.load_disabled_modules() == {'lvm', 'nfs'} | set(app.DEFAULT_OFF)


def test_load_disabled_ignores_unknown_and_bad_json(tmp_path, monkeypatch):
    p = tmp_path / 'modules.json'
    # 'bogus' is not a real module → must be dropped; real ids kept.
    p.write_text(json.dumps({'disabled': ['zfs', 'bogus']}))
    monkeypatch.setattr(app, 'MODULES_FILE', str(p))
    assert app.load_disabled_modules() == {'zfs'} | set(app.DEFAULT_OFF)

    p.write_text('{ this is not json')
    assert app.load_disabled_modules() == set(app.DEFAULT_OFF)


def test_default_off_module_honors_enabled_optin(tmp_path, monkeypatch):
    # metrics is default-off: disabled with no file, disabled when merely absent,
    # enabled only when it appears in the positive `enabled` opt-in list.
    mf = tmp_path / 'modules.json'
    monkeypatch.setattr(app, 'MODULES_FILE', str(mf))
    assert 'metrics' in app.load_disabled_modules()                 # no file → off
    mf.write_text(json.dumps({'disabled': [], 'enabled': ['metrics']}))
    assert 'metrics' not in app.load_disabled_modules()             # opted in → on
    mf.write_text(json.dumps({'disabled': [], 'enabled': []}))
    assert 'metrics' in app.load_disabled_modules()                 # opt-in gone → off


def _fake_run_factory(active_map, enabled_state):
    """Build a fake run() that answers systemctl is-active/is-enabled from maps
    and reports no zpools, so _compute_alerts exercises only service logic."""
    def fake_run(args, **kw):
        if args[:1] == ['systemctl'] and 'is-active' in args:
            unit = args[-1]
            return (active_map.get(unit, 'inactive'), '', 0)
        if args[:1] == ['systemctl'] and 'is-enabled' in args:
            return (enabled_state, '', 0)
        if args[:1] == ['zpool']:
            return ('', '', 1)   # no pools
        return ('', '', 0)
    return fake_run


def test_alerts_suppressed_for_disabled_module(monkeypatch):
    # smbd inactive but the SMB *module* is disabled → no Samba alert.
    monkeypatch.setattr(app, 'run', _fake_run_factory({'zfs.target': 'active'}, 'enabled'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'smb', 'iscsi', 'nfs'})
    monkeypatch.setattr(app, '_smart_health_ok', lambda: True)
    keys = {a['key'] for a in app._compute_alerts()}
    assert 'service:smb' not in keys
    assert 'service:nfs' not in keys


def test_alerts_suppressed_for_boot_disabled_unit(monkeypatch):
    # All services inactive AND disabled at boot → intentional, no alerts.
    monkeypatch.setattr(app, 'run', _fake_run_factory({}, 'disabled'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(app, '_smart_health_ok', lambda: True)
    # Keep the firewall hook out of it (ufw presence varies by test host).
    monkeypatch.setitem(app._DESCRIPTORS['firewall'], 'alerts', lambda: [])
    assert app._compute_alerts() == []


def test_alerts_fire_for_enabled_inactive_service(monkeypatch):
    # Enabled but inactive (and module not disabled) → that IS an issue.
    monkeypatch.setattr(app, 'run', _fake_run_factory({'zfs.target': 'active'}, 'enabled'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(app, '_smart_health_ok', lambda: True)
    keys = {a['key'] for a in app._compute_alerts()}
    assert 'service:smb' in keys      # smbd inactive + enabled + module on
    assert 'service:zfs' not in keys  # zfs.target active


def test_service_keys_are_module_ids():
    # The dashboard hides a service line when its module is disabled; that filter
    # keys the SYSTEM_SERVICES dict by module id, so every service key must be a
    # real module id or it could never be filtered.
    assert set(app.SYSTEM_SERVICES) <= app.MODULE_IDS


def test_manager_lists_container_docker_firewall():
    # The service manager derives its rows from SYSTEM_SERVICES; the container
    # runtime, Docker, and the firewall each need an entry so they are
    # controllable there. All three are absent on many nodes by design, so none
    # raises a health alert (alert=False).
    for key in ('instances', 'docker', 'firewall'):
        assert key in app.SYSTEM_SERVICES
        assert app.SYSTEM_SERVICES[key]['alert'] is False


def test_container_service_detection_by_socket(monkeypatch):
    # The container runtime's systemd unit depends on which of LXD-snap, LXD-deb,
    # or Incus owns the socket — detected by socket path, mirroring the containers
    # client. An explicit DASHBOARD_LXD_SOCKET override wins over fs probes.
    import os
    from nexusdash.core import services as svc
    monkeypatch.delenv('DASHBOARD_LXD_SOCKET', raising=False)
    cases = [
        ('/var/snap/lxd/common/lxd/unix.socket', 'snap.lxd.daemon', 'LXD'),
        ('/var/lib/lxd/unix.socket',             'lxd',             'LXD'),
        ('/var/lib/incus/unix.socket',           'incus',           'Incus'),
    ]
    for present, unit, name in cases:
        monkeypatch.setattr(os.path, 'exists', lambda p, _p=present: p == _p)
        got = svc._detect_container_service()
        assert (got['service'], got['name']) == (unit, name)
    # Nothing present → still returns an entry (shows Missing), defaulting to LXD.
    monkeypatch.setattr(os.path, 'exists', lambda p: False)
    assert svc._detect_container_service()['name'] == 'LXD'
    # An incus socket override forces the incus unit regardless of fs probes.
    monkeypatch.setenv('DASHBOARD_LXD_SOCKET', '/run/incus/unix.socket')
    assert svc._detect_container_service()['service'] == 'incus'


def test_summary_drops_disabled_module_service(monkeypatch):
    # A disabled module must not appear in /api/summary's services block, even
    # though its unit may still be active (the Services page lists it separately).
    monkeypatch.setattr(app, 'run', _fake_run_factory(
        {s['service']: 'active' for s in app.SYSTEM_SERVICES.values()}, 'enabled'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'smb'})
    with app.app.test_request_context():
        services = app.api_summary().get_json()['services']
    assert 'smb' not in services
    assert 'zfs' in services          # an enabled module is still listed
