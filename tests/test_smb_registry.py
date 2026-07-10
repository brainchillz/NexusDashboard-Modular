"""Samba registry share backend (`net conf`) — the Cockpit file-sharing model.

Nodes previously managed by cockpit-file-sharing (node2) keep their shares
in Samba's registry with only `include = registry` in smb.conf. The smb module
surfaces both stores side by side: rows carry backend 'file'|'registry',
writes route to the store the share lives in, and registry updates are
param-level merges so Cockpit-written params the UI doesn't manage survive.
Everything degrades to file-only when `net conf` is unavailable (no samba /
no sudoers grant) — that is the state of every non-registry fleet node.
"""
import pytest

import app

# Modeled on node2's real `net conf list` output (cockpit-file-sharing).
NET_CONF_LIST = """\
[global]
\tworkgroup = WORKGROUP
\tvfs objects = catia fruit streams_xattr

[ISO]
\tpath = /volume01/iso
\tcomment =
\tbrowseable = yes
\tread only = no
\tinherit permissions = no
\twrite list = alice
\tguest ok = yes

[Video]
\tpath = /volume01/video
\tread only = no
\tavailable = no
"""


def _fake_run(monkeypatch, net_rc=0, net_out=NET_CONF_LIST):
    """run() stand-in: `net conf list` returns the canned registry; everything
    else (testparm, smbstatus, ...) succeeds empty."""
    def fake(args, **kw):
        if list(args)[:3] == ['net', 'conf', 'list']:
            return (net_out if net_rc == 0 else ''), '', net_rc
        return '', '', 0
    monkeypatch.setattr(app, 'run', fake)


def _record_run_safe(monkeypatch):
    calls = []
    def fake(args, input_data=None):
        calls.append(list(args))
        return {'success': True, 'stdout': '', 'stderr': '', 'returncode': 0}
    monkeypatch.setattr(app, 'run_safe', fake)
    return calls


def _file_conf(monkeypatch, tmp_path, content):
    conf = tmp_path / 'smb.conf'
    conf.write_text(content)
    monkeypatch.setattr(app, 'SMBCONF_FILE', str(conf))
    return conf


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── Parsing / detection ────────────────────────────────────────────────

def test_registry_conf_list_parses_net_conf_output(monkeypatch):
    _fake_run(monkeypatch)
    sections, ok = app.registry_conf_list()
    assert ok
    assert sections['ISO']['path'] == '/volume01/iso'
    assert sections['ISO']['write list'] == 'alice'
    assert sections['Video']['available'] == 'no'
    assert 'global' in sections


def test_registry_conf_list_degrades_on_failure(monkeypatch):
    # No sudoers grant / no samba: sudo -n or the binary fails → ({}, False).
    _fake_run(monkeypatch, net_rc=255)
    sections, ok = app.registry_conf_list()
    assert (sections, ok) == ({}, False)


def test_registry_included_detection(monkeypatch, tmp_path):
    _file_conf(monkeypatch, tmp_path,
               '[global]\n\t# cockpit-file-sharing:\n\tinclude = registry\n')
    assert app.registry_included()
    _file_conf(monkeypatch, tmp_path, '[global]\n\tworkgroup = W\n')
    assert not app.registry_included()


# ─── Listing: merge + backend tags ──────────────────────────────────────

def test_shares_list_merges_both_backends(client, monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    _file_conf(monkeypatch, tmp_path,
               '[global]\n\tinclude = registry\n[filedata]\n\tpath = /srv/data\n')
    shares = {s['name']: s for s in client.get('/api/smb/shares').get_json()}
    assert shares['filedata']['backend'] == 'file'
    assert shares['ISO']['backend'] == 'registry'
    assert shares['Video']['available'] == 'no'
    assert 'global' not in shares


def test_shares_list_registry_wins_on_duplicate_name(client, monkeypatch, tmp_path):
    # smbd loads the registry include last, so its definition wins — mirror that.
    _fake_run(monkeypatch)
    _file_conf(monkeypatch, tmp_path,
               '[global]\n\tinclude = registry\n[ISO]\n\tpath = /stale/file/copy\n')
    rows = [s for s in client.get('/api/smb/shares').get_json() if s['name'] == 'ISO']
    assert len(rows) == 1
    assert rows[0]['backend'] == 'registry'
    assert rows[0]['path'] == '/volume01/iso'


def test_shares_list_file_only_when_registry_unavailable(client, monkeypatch, tmp_path):
    _fake_run(monkeypatch, net_rc=255)
    _file_conf(monkeypatch, tmp_path, '[global]\n[filedata]\n\tpath = /srv/data\n')
    shares = client.get('/api/smb/shares').get_json()
    assert [s['name'] for s in shares] == ['filedata']
    assert shares[0]['backend'] == 'file'


def test_registry_status_endpoint(client, monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    _file_conf(monkeypatch, tmp_path, '[global]\n\tinclude = registry\n')
    body = client.get('/api/smb/registry').get_json()
    assert body == {'enabled': True, 'accessible': True, 'share_count': 2}
    _fake_run(monkeypatch, net_rc=255)
    _file_conf(monkeypatch, tmp_path, '[global]\n')
    body = client.get('/api/smb/registry').get_json()
    assert body == {'enabled': False, 'accessible': False, 'share_count': 0}


# ─── Writes: create / update / delete / toggle routing ──────────────────

def test_create_registry_share(client, monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    _file_conf(monkeypatch, tmp_path, '[global]\n\tinclude = registry\n')
    calls = _record_run_safe(monkeypatch)
    r = client.post('/api/smb/shares', json={
        'name': 'newshare', 'path': '/volume01/new', 'backend': 'registry',
        'read_only': 'no', 'guest_ok': 'no'})
    assert r.get_json()['success']
    assert ['net', 'conf', 'addshare', 'newshare', '/volume01/new'] in calls
    assert ['net', 'conf', 'setparm', 'newshare', 'read only', 'no'] in calls
    # No smb.conf rewrite happened (no tee).
    assert not any(c[0] == 'tee' for c in calls)


def test_update_registry_share_merges_managed_params_only(client, monkeypatch, tmp_path):
    """Editing ISO must not touch Cockpit's `inherit permissions`, must only
    set changed params, and must delparm a cleared managed param."""
    _fake_run(monkeypatch)
    _file_conf(monkeypatch, tmp_path, '[global]\n\tinclude = registry\n')
    calls = _record_run_safe(monkeypatch)
    # Same as current except: write_list cleared, read_only flipped to yes.
    r = client.post('/api/smb/shares', json={
        'name': 'ISO', 'path': '/volume01/iso',
        'read_only': 'yes', 'guest_ok': 'yes', 'browseable': 'yes'})
    assert r.get_json()['success']
    net = [c for c in calls if c[0] == 'net']
    assert ['net', 'conf', 'setparm', 'ISO', 'read only', 'yes'] in net
    assert ['net', 'conf', 'delparm', 'ISO', 'write list'] in net
    # Unchanged param not rewritten; unmanaged Cockpit param untouched.
    assert ['net', 'conf', 'setparm', 'ISO', 'path', '/volume01/iso'] not in net
    assert not any('inherit permissions' in c for c in net)
    assert not any(c[:3] == ['net', 'conf', 'addshare'] for c in net)


def test_default_backend_keeps_share_where_it_lives(client, monkeypatch, tmp_path):
    # No backend field (e.g. the ZFS wizard): an existing registry share stays
    # in the registry instead of being duplicated into smb.conf.
    _fake_run(monkeypatch)
    _file_conf(monkeypatch, tmp_path, '[global]\n\tinclude = registry\n')
    calls = _record_run_safe(monkeypatch)
    r = client.post('/api/smb/shares', json={
        'name': 'ISO', 'path': '/volume01/iso', 'read_only': 'no', 'guest_ok': 'yes'})
    assert r.get_json()['success']
    assert any(c[:2] == ['net', 'conf'] for c in calls)
    assert not any(c[0] == 'tee' for c in calls)


def test_registry_backend_refused_when_unavailable(client, monkeypatch, tmp_path):
    _fake_run(monkeypatch, net_rc=255)
    _file_conf(monkeypatch, tmp_path, '[global]\n')
    _record_run_safe(monkeypatch)
    r = client.post('/api/smb/shares', json={
        'name': 'x', 'path': '/srv/x', 'backend': 'registry'})
    assert r.status_code == 400
    assert 'not accessible' in r.get_json()['error']


def test_delete_routes_to_registry(client, monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    _file_conf(monkeypatch, tmp_path, '[global]\n\tinclude = registry\n')
    calls = _record_run_safe(monkeypatch)
    assert client.delete('/api/smb/shares/ISO').get_json()['success']
    assert calls == [['net', 'conf', 'delshare', 'ISO']]


def test_toggle_registry_share_both_directions(client, monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    _file_conf(monkeypatch, tmp_path, '[global]\n\tinclude = registry\n')
    calls = _record_run_safe(monkeypatch)
    assert client.post('/api/smb/shares/ISO/toggle').get_json()['success']
    assert calls == [['net', 'conf', 'setparm', 'ISO', 'available', 'no']]
    calls.clear()
    assert client.post('/api/smb/shares/Video/toggle').get_json()['success']
    assert calls == [['net', 'conf', 'delparm', 'Video', 'available']]


def test_file_share_write_still_file_backed(client, monkeypatch, tmp_path):
    """Legacy behavior intact: a file share (or registry-less node) goes
    through testparm + tee + reload exactly as before."""
    _fake_run(monkeypatch, net_rc=255)
    _file_conf(monkeypatch, tmp_path, '[global]\n[data]\n\tpath = /srv/data\n')
    calls = _record_run_safe(monkeypatch)
    r = client.post('/api/smb/shares', json={
        'name': 'data', 'path': '/srv/data', 'read_only': 'yes', 'guest_ok': 'no'})
    assert r.get_json()['success']
    assert any(c[0] == 'tee' for c in calls)
    assert not any(c[0] == 'net' for c in calls)
