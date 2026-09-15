"""Regression tests for the 3.4.3 codebase review.

One test per defect found, each named for the behaviour it pins rather than
the code it touches, so a future reader can tell what used to go wrong.
"""
import http.server
import os
import threading

import pytest

import app
from nexusdash.core import auth
from nexusdash.modules import updates as upd
from nexusdash.modules import replication as repl
from nexusdash.modules import smb, dnsmasq, mdraid


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── /api/logs/<service> lived on the disks blueprint ─────────────────────

def test_service_logs_survive_a_disabled_disks_module(client, monkeypatch):
    """The Services page's Logs button calls /api/logs/<service>, which was
    defined on the DISKS blueprint — so disabling the Disks module (the docker
    host does) answered "module 'disks' is disabled on this node"."""
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'disks'})
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('line one\nline two\n', '', 0))
    r = client.get('/api/logs/zfs')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert 'line one' in r.get_json()['logs']
    assert app.app.view_functions['logs.api_logs']       # and not disks.*
    assert 'disks.api_logs' not in app.app.view_functions


# ─── POST /api/users overwrote an existing account ───────────────────────

def test_users_create_refuses_an_existing_username(client, monkeypatch):
    """Creating 'admin' again with role=readonly silently replaced the record —
    resetting the password AND demoting the only administrator, which the
    role route explicitly refuses."""
    store = {'users': {'admin': {'password': 'x', 'role': 'admin', 'smb': False}}}
    saved = []
    monkeypatch.setattr(app, 'load_config', lambda: store)
    monkeypatch.setattr(app, 'save_config', lambda cfg: saved.append(cfg))
    r = client.post('/api/users', json={'username': 'admin', 'password': 'pw12345678',
                                        'role': 'readonly'})
    assert r.status_code == 409
    assert 'already exists' in r.get_json()['error']
    assert saved == []                                   # nothing written
    assert store['users']['admin']['role'] == 'admin'    # nothing changed


# ─── updates: a crash in the check thread wedged `checking` ───────────────

def test_updates_refresh_always_clears_checking(monkeypatch):
    """`checking` gates every later check AND every apply. The background
    thread used to die on an IndexError (whitespace-only stderr) before it
    could clear the flag — the module then refused applies until a restart."""
    monkeypatch.setattr(upd, '_check_debian', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(upd, '_check_rhel', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setitem(upd._state, 'checking', True)
    upd._refresh()
    assert upd._state['checking'] is False
    assert 'boom' in upd._state['error']


def test_updates_last_line_tolerates_blank_output():
    """The specific crash: `(e or out).strip().splitlines()[-1]` on '  \\n'."""
    assert upd._last_line('  \n', 'apt-get failed') == 'apt-get failed'
    assert upd._last_line('E: one\nE: two\n', 'x') == 'E: two'
    assert upd._last_line(None, 'dnf failed') == 'dnf failed'


def test_dhcp_probe_error_tolerates_blank_stderr(monkeypatch):
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '  \n', 1))
    assert dnsmasq.probe_for_foreign_dhcp(['eth0']) == {'servers': [], 'error': 'probe failed'}


# ─── replication: SSH failure was mistaken for "target does not exist" ────

def _job():
    return {'source': 'tank/data', 'target': 'backup/data', 'host': 'h', 'user': 'u',
            'port': 22, 'recursive': False}


def test_remote_snaps_separates_missing_dataset_from_failure(monkeypatch):
    calls = []

    def fake(args, **kw):
        calls.append(args)
        return fake.reply
    monkeypatch.setattr(app, 'run', fake)
    fake.reply = ('backup/data@a\nbackup/data@b\n', '', 0)
    assert repl._remote_snaps(_job()) == (['a', 'b'], None)
    fake.reply = ('', "cannot open 'backup/data': dataset does not exist\n", 1)
    assert repl._remote_snaps(_job()) == (None, None)
    fake.reply = ('', 'ssh: connect to host h port 22: No route to host\n', 255)
    snaps, error = repl._remote_snaps(_job())
    assert snaps is None and 'No route to host' in error


def test_replicate_job_aborts_instead_of_full_sending_on_a_listing_failure(monkeypatch):
    """A full stream is received with `-F` — for an existing target that is an
    OVERWRITE. A transient listing failure must abort, not re-seed."""
    monkeypatch.setattr(repl, '_local_snaps', lambda ds: ['a', 'b'])
    monkeypatch.setattr(repl, '_remote_snaps', lambda job: (None, 'sudo: a password is required'))
    sent = []
    monkeypatch.setattr(repl, '_pipe_send_recv', lambda s, r: sent.append((s, r)) or (True, ''))
    res = repl.replicate_job(_job())
    assert res['ok'] is False
    assert 'password is required' in res['error']
    assert sent == []                                    # nothing was streamed


# ─── zfs: `zpool list` column guard ───────────────────────────────────────

def test_zfs_pools_does_not_index_past_an_eight_column_line(client, monkeypatch):
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('tank\t1T\t500G\t500G\t50%\t3%\t1.00x\tONLINE\n', '', 0))
    assert client.get('/api/zfs/pools').get_json() == []          # short row skipped, no 500
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('tank\t1T\t500G\t500G\t50%\t3%\t1.00x\tONLINE\t-\n', '', 0))
    assert client.get('/api/zfs/pools').get_json()[0]['altroot'] == '-'


# ─── smb: Samba booleans are case-insensitive ─────────────────────────────

def test_share_row_normalizes_samba_booleans():
    row = smb._share_row('media', {'available': 'No', 'read only': 'Yes',
                                    'guest ok': 'true', 'browseable': '0'}, 'file')
    assert (row['available'], row['read_only'], row['guest_ok'], row['browseable']) == \
        ('no', 'yes', 'yes', 'no')


def test_share_toggle_re_enables_a_hand_written_capitalized_no(client, monkeypatch):
    """`available = No` (hand-edited smb.conf) rendered as enabled and, because
    the toggle compared the raw text, could never be switched back on."""
    monkeypatch.setattr(smb, 'registry_conf_list', lambda: ({}, False))
    monkeypatch.setattr(smb, 'smbconf_parse', lambda: {'global': {}, 'media': {'path': '/srv', 'available': 'No'}})
    applied = []
    monkeypatch.setattr(smb, 'smbconf_apply', lambda s: applied.append(s) or {'success': True})
    assert client.post('/api/smb/shares/media/toggle').status_code == 200
    assert 'available' not in applied[0]['media']         # re-enabled, not re-disabled


# ─── nfs: /etc/exports is whitespace-delimited and '#' starts a comment ───

@pytest.mark.parametrize('path', ['/srv/my share', '/srv/a#b', '/srv/"q"', '/srv/back\\slash'])
def test_nfs_export_refuses_paths_that_break_the_exports_line(client, monkeypatch, path):
    monkeypatch.setattr(app, 'run_safe', lambda *a, **k: pytest.fail('must not write'))
    r = client.post('/api/nfs/exports', json={'path': path, 'clients': []})
    assert r.status_code == 400
    assert 'may not contain' in r.get_json()['error']


# ─── request.get_json() is None for a literal `null` body ─────────────────

@pytest.mark.parametrize('path', ['/api/zfs/datasets', '/api/zfs/snapshots',
                                  '/api/iscsi/targets', '/api/smb/users', '/api/nfs/exports'])
def test_a_null_json_body_is_a_400_not_a_500(client, monkeypatch, path):
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    r = client.post(path, data='null', content_type='application/json')
    assert r.status_code == 400, r.get_data(as_text=True)


# ─── validators: `$` let a trailing newline through ────────────────────────

@pytest.mark.parametrize('rx, value', [
    (app.RE_POOL, 'tank\n'), (app.RE_DATASET, 'tank/data\n'), (app.RE_SHARE, 'media\n'),
    (app.RE_USER, 'bob\n'), (app.RE_PATH, '/srv/x\n'), (app.RE_COMMENT, 'hi\n'),
    (auth.RE_USERNAME, 'admin\n'), (app.RE_IQN, 'iqn.2025-01.com.example:t\n'),
])
def test_input_validators_reject_a_trailing_newline(rx, value):
    """re.match + `$` matches before a final '\\n'; every input validator is
    now \\Z-anchored (the convention the newer ones already followed)."""
    assert rx.match(value) is None
    assert rx.match(value.rstrip('\n')) is not None


def test_mdadm_array_name_cannot_be_a_dot_path_component():
    assert mdraid.RE_MDNAME.match('data') and mdraid.RE_MDNAME.match('raid_1.a')
    for bad in ('.', '..', '-x', '.hidden'):
        assert mdraid.RE_MDNAME.match(bad) is None


# ─── unhandled exceptions came back as an HTML 500 page ───────────────────

def test_unhandled_exception_is_a_json_500(client, monkeypatch):
    def boom():
        raise RuntimeError('kaboom')
    monkeypatch.setitem(app.app.view_functions, 'zfs.zfs_pools', boom)
    r = client.get('/api/zfs/pools')
    assert r.status_code == 500
    body = r.get_json()
    assert body['success'] is False and 'RuntimeError' in body['error'] and 'kaboom' in body['error']


def test_http_errors_keep_their_own_bodies(client):
    assert client.get('/api/no/such/route').status_code == 404


# ─── containers: unguarded int() on a request field ───────────────────────

def test_instance_state_junk_timeout_is_a_400(client, monkeypatch):
    monkeypatch.setattr(app, 'lxd_request', lambda *a, **k: pytest.fail('must not reach LXD'))
    r = client.put('/api/instances/box/state', json={'action': 'stop', 'timeout': 'soon'})
    assert r.status_code == 400
    assert 'timeout' in r.get_json()['error']


# ─── llama: a complete .partial made every resume a 416 ───────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b''
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get('Range')
        self.hits.append(rng)
        start = int(rng.split('=')[1].split('-')[0]) if rng else 0
        if start >= len(self.payload):
            self.send_response(416)
            self.send_header('Content-Range', 'bytes */%d' % len(self.payload))
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = self.payload[start:]
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header('Content-Range', 'bytes %d-%d/%d'
                             % (start, len(self.payload) - 1, len(self.payload)))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server416():
    _Handler.hits = []
    srv = http.server.HTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield 'http://127.0.0.1:%d/f' % srv.server_address[1]
    srv.shutdown()


def test_fetch_finalizes_a_partial_that_is_already_the_whole_file(server416, tmp_path):
    """Worker died between the last write and the rename: the .partial IS the
    file. Resume asked for bytes=<size>- and got 416 forever."""
    _Handler.payload = b'X' * 4096
    dest = str(tmp_path / 'm.gguf')
    with open(dest + '.partial', 'wb') as f:
        f.write(_Handler.payload)
    assert app._fetch_file(server416, dest, '', 0, lambda n: None, lambda: False) == 4096
    assert open(dest, 'rb').read() == _Handler.payload
    assert not os.path.exists(dest + '.partial')
    assert _Handler.hits == ['bytes=4096-']              # one probe, no re-download


def test_fetch_discards_a_partial_longer_than_the_remote_file(server416, tmp_path):
    """A .partial LONGER than the file cannot be right; start over clean."""
    _Handler.payload = b'Y' * 2048
    dest = str(tmp_path / 'm.gguf')
    with open(dest + '.partial', 'wb') as f:
        f.write(b'Z' * 3000)
    assert app._fetch_file(server416, dest, '', 0, lambda n: None, lambda: False) == 2048
    assert open(dest, 'rb').read() == _Handler.payload
    assert _Handler.hits == ['bytes=3000-', None]        # 416, then a clean GET


def test_content_range_total_parsing():
    assert app._content_range_total('bytes */1234') == 1234
    assert app._content_range_total('bytes 0-9/10') == 10
    assert app._content_range_total(None) is None
    assert app._content_range_total('bytes */x') is None
