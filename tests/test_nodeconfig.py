"""Node configuration export / restore (3.6.0). Everything runs against a
temp APP_DIR; openssl (the TLS module's dependency) does the AES."""
import os
import json
import base64
import pytest

import app
from nexusdash.core import nodeconfig as nc


@pytest.fixture
def appdir(monkeypatch, tmp_path):
    d = tmp_path / 'app'
    d.mkdir()
    (d / 'certs').mkdir()
    monkeypatch.setattr(nc, 'APP_DIR', str(d))
    monkeypatch.setattr(nc, 'TLS_CERT', str(d / 'certs' / 'dashboard.crt'))
    monkeypatch.setattr(nc, 'TLS_KEY', str(d / 'certs' / 'dashboard.key'))
    (d / 'auth.json').write_text('{"users": {"admin": {"password": "scrypt$x", "role": "admin"}}}')
    os.chmod(d / 'auth.json', 0o600)
    (d / 'modules.json').write_text('{"disabled": ["nut"], "enabled": ["halogen"]}')
    os.chmod(d / 'modules.json', 0o644)                   # the CI runner's umask is 002
    (d / 'certs' / 'dashboard.crt').write_text('CERT')
    (d / 'certs' / 'dashboard.key').write_text('KEY')
    os.chmod(d / 'certs' / 'dashboard.key', 0o600)
    (d / 'dnsmasq' / 'state').mkdir(parents=True)
    (d / 'dnsmasq' / 'state' / 'hosts.json').write_text('[]')
    (d / 'dnsmasq' / 'state' / '.tmp').write_text('junk')                 # dotfiles are skipped
    (d / 'history.db').write_text('not exported')
    return d


def test_inventory_lists_present_items(appdir):
    inv = {r['id']: r for r in nc.inventory()}
    assert inv['auth']['present'] and inv['auth']['secret']
    assert inv['tls']['files'] == 2
    assert inv['dnsmasq']['files'] == 1 and inv['compose']['present'] is False
    assert inv['sso']['present'] is False


def test_bundle_round_trip_plain(appdir):
    blob, manifest = nc.build_bundle()
    assert manifest['items'] and {f['path'] for f in manifest['files']} == {
        'auth.json', 'modules.json', 'certs/dashboard.crt', 'certs/dashboard.key', 'dnsmasq/state/hosts.json'}
    assert not nc.is_encrypted(blob)
    m2, files = nc.read_bundle(blob)
    assert files['auth.json'][1] == 0o600 and files['certs/dashboard.key'][1] == 0o600
    assert files['modules.json'][1] == 0o644


def test_bundle_round_trip_encrypted(appdir):
    blob, _ = nc.build_bundle(['auth', 'modules'])
    enc = nc.encrypt(blob, 'correct horse battery')
    assert nc.is_encrypted(enc) and enc != blob
    m, files = nc.read_bundle(enc, 'correct horse battery')
    assert set(files) == {'auth.json', 'modules.json'}
    with pytest.raises(ValueError, match='wrong passphrase'):
        nc.read_bundle(enc, 'nope')
    with pytest.raises(ValueError, match='passphrase required'):
        nc.read_bundle(enc, '')
    tampered = enc[:-5] + b'XXXXX'
    with pytest.raises(ValueError, match='corrupted'):
        nc.read_bundle(tampered, 'correct horse battery')


def test_restore_into_a_fresh_dir(appdir, monkeypatch, tmp_path):
    blob, _ = nc.build_bundle()
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    monkeypatch.setattr(nc, 'APP_DIR', str(fresh))
    monkeypatch.setattr(nc, 'TLS_CERT', str(fresh / 'certs' / 'dashboard.crt'))
    monkeypatch.setattr(nc, 'TLS_KEY', str(fresh / 'certs' / 'dashboard.key'))
    insp = nc.inspect_bundle(blob)
    assert {r['status'] for r in insp['files']} == {'new'} and insp['manifest']['hostname']
    res = nc.apply_bundle(blob)
    assert res['restart_recommended'] is True and len(res['written']) == 5
    assert json.load(open(fresh / 'auth.json'))['users']['admin']['role'] == 'admin'
    assert oct(os.stat(fresh / 'auth.json').st_mode & 0o777) == '0o600'
    assert (fresh / 'dnsmasq' / 'state' / 'hosts.json').read_text() == '[]'
    assert not (fresh / 'history.db').exists()
    # Second inspect: everything is 'same'; a changed file shows 'differs'.
    (fresh / 'modules.json').write_text('{}')
    st = {r['path']: r['status'] for r in nc.inspect_bundle(blob)['files']}
    assert st['auth.json'] == 'same' and st['modules.json'] == 'differs'
    # Selective restore leaves the others alone and does not flag a restart.
    res = nc.apply_bundle(blob, items=['modules'])
    assert res['written'] == ['modules.json'] and res['restart_recommended'] is False
    assert (fresh / 'modules.json').read_text() != '{}'


def _tar_with(manifest_files, members):
    """Hand-built bundle for the refusal tests."""
    import io, tarfile, hashlib, time
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        man = {'format': 1, 'items': sorted({f['item'] for f in manifest_files}), 'files': manifest_files,
               'hostname': 'x', 'app_version': '3.6.0', 'created': int(time.time())}
        md = json.dumps(man).encode()
        ti = tarfile.TarInfo(nc.MANIFEST); ti.size = len(md); tar.addfile(ti, io.BytesIO(md))
        for name, data in members.items():
            ti = tarfile.TarInfo(name); ti.size = len(data); tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_read_bundle_refuses_paths_outside_the_allowlist(appdir):
    import hashlib
    data = b'{}'
    sha = hashlib.sha256(data).hexdigest()
    for path, item in (('../../etc/passwd', 'auth'), ('/etc/passwd', 'auth'), ('modules.json', 'auth'),
                       ('certs/other.key', 'tls'), ('compose/../auth.json', 'compose'), ('.hidden', 'auth')):
        blob = _tar_with([{'path': path, 'item': item, 'sha256': sha, 'mode': 0o600, 'bytes': 2}], {path: data})
        with pytest.raises(ValueError):
            nc.read_bundle(blob)
    blob = _tar_with([{'path': 'auth.json', 'item': 'auth', 'sha256': 'deadbeef', 'mode': 0o600, 'bytes': 2}], {'auth.json': data})
    with pytest.raises(ValueError, match='checksum'):
        nc.read_bundle(blob)
    blob = _tar_with([{'path': 'x.json', 'item': 'nope', 'sha256': sha, 'mode': 0o600, 'bytes': 2}], {'x.json': data})
    with pytest.raises(ValueError, match='unknown item'):
        nc.read_bundle(blob)
    with pytest.raises(ValueError, match='not a bundle'):
        nc.read_bundle(b'garbage')


@pytest.fixture
def client(monkeypatch, appdir):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('a', 'admin'))
    return app.app.test_client()


def test_routes(client, monkeypatch):
    inv = client.get('/api/config/inventory').get_json()
    assert {r['id'] for r in inv['items']} == {i[0] for i in nc.ITEMS}
    r = client.post('/api/config/export', json={'passphrase': 'short'})
    assert r.status_code == 400
    r = client.post('/api/config/export', json={'passphrase': 'long enough pw', 'include': ['auth', 'modules']})
    assert r.status_code == 200 and r.headers['Content-Disposition'].endswith('.tar.gz.enc"')
    blob = r.data
    b64 = base64.b64encode(blob).decode()
    assert client.post('/api/config/inspect', json={'bundle': b64, 'passphrase': 'bad'}).status_code == 400
    insp = client.post('/api/config/inspect', json={'bundle': b64, 'passphrase': 'long enough pw'}).get_json()
    assert {f['path'] for f in insp['files']} == {'auth.json', 'modules.json'}
    assert client.post('/api/config/restore', json={'bundle': b64, 'passphrase': 'long enough pw'}).status_code == 400   # no confirm
    res = client.post('/api/config/restore', json={'bundle': b64, 'passphrase': 'long enough pw', 'confirm': True}).get_json()
    assert res['success'] and sorted(res['written']) == ['auth.json', 'modules.json']
    assert client.post('/api/config/inspect', json={'bundle': 'not base64!!'}).status_code == 400


def test_routes_are_admin_only(client, monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('v', 'readonly'))
    assert client.get('/api/config/inventory').status_code == 403
    assert client.post('/api/config/export', json={}).status_code == 403


def test_cli_round_trip(appdir, monkeypatch, tmp_path, capsys):
    out = tmp_path / 'node.tar.gz.enc'
    monkeypatch.setenv('DASHBOARD_CONFIG_PASSPHRASE', 'cli passphrase 1')
    assert nc.cli_config_export(['app.py', 'config-export', str(out), '--items', 'auth,modules']) == 0
    assert nc.is_encrypted(out.read_bytes()) and oct(os.stat(out).st_mode & 0o777) == '0o600'
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    monkeypatch.setattr(nc, 'APP_DIR', str(fresh))
    assert nc.cli_config_restore(['app.py', 'config-restore', str(out)]) == 0
    assert (fresh / 'auth.json').exists() and (fresh / 'modules.json').exists()
    monkeypatch.setenv('DASHBOARD_CONFIG_PASSPHRASE', 'wrong')
    assert nc.cli_config_restore(['app.py', 'config-restore', str(out)]) == 1
    assert 'refused' in capsys.readouterr().out
    assert 'config-export' in app.COMMANDS and 'config-restore' in app.COMMANDS
