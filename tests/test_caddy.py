"""Caddy module — Caddyfile parsing, simple-site recognition, and the
helper-mediated add/edit/delete flow. All caddy/systemctl output is faked and
the helper never runs; no caddy or root needed."""
import pytest

import app


SAMPLE = """# front door
{
\temail admin@example.com
}

(logs) {
\tlog
}

import conf.d/*.caddy

books.example.com {
\treverse_proxy https://127.0.0.1:8000 {
\t\ttransport http {
\t\t\ttls_insecure_skip_verify
\t\t}
\t}
}

photos.example.com {
\treverse_proxy 127.0.0.1:2283
}

a.example.com, b.example.com {
\treverse_proxy 127.0.0.1:9000
}

complex.example.com {
\tencode gzip
\treverse_proxy 127.0.0.1:9100
}
"""


def _fake_run(responses=None, apply_rc=0):
    """run() stub: records argv + input_data, answers status probes."""
    calls = []
    responses = responses or {}
    def fake(args, input_data=None, **kw):
        calls.append((list(args), input_data))
        for key, resp in responses.items():
            if tuple(args[:len(key)]) == key:
                return resp
        if args[0] == 'caddy':
            return ('v2.8.4 h1:abc', '', 0)
        if args[:2] == ['systemctl', 'is-active']:
            return ('active\n', '', 0)
        return ('applied', '', apply_rc)
    return fake, calls


def _applied(calls):
    """The Caddyfile text handed to the helper (last apply call)."""
    for args, input_data in reversed(calls):
        if args[1:2] == ['apply']:
            return input_data
    return None


# ─── Parsing ─────────────────────────────────────────────────────────────

def test_parse_blocks_and_spans():
    blocks = app._parse_caddyfile(SAMPLE)
    labels = [b['label'] for b in blocks]
    assert labels == ['', '(logs)', 'books.example.com', 'photos.example.com',
                      'a.example.com, b.example.com', 'complex.example.com']
    lines = SAMPLE.splitlines()
    for b in blocks:
        assert lines[b['start']].rstrip().endswith('{')
        assert lines[b['end'] - 1].strip() == '}'


def test_sites_classification():
    sites = app._sites(app._parse_caddyfile(SAMPLE))
    by_host = {s['addresses'][0]: s for s in sites}
    assert set(by_host) == {'books.example.com', 'photos.example.com',
                            'a.example.com', 'complex.example.com'}
    books = by_host['books.example.com']
    assert books['upstream'] == 'https://127.0.0.1:8000'
    assert books['skip_tls_verify'] and books['simple']
    photos = by_host['photos.example.com']
    assert photos['upstream'] == '127.0.0.1:2283'
    assert not photos['skip_tls_verify'] and photos['simple']
    multi = by_host['a.example.com']
    assert multi['addresses'] == ['a.example.com', 'b.example.com']
    assert multi['upstream'] == '127.0.0.1:9000' and not multi['simple']
    cx = by_host['complex.example.com']
    assert cx['upstream'] is None and not cx['simple']


def test_parse_ignores_noise():
    assert app._parse_caddyfile('') == []
    assert app._parse_caddyfile(None) == []
    assert app._parse_caddyfile('# just a comment\nimport x\n') == []


def test_match_simple_shapes():
    assert app._match_simple(['reverse_proxy 127.0.0.1:80']) == ('127.0.0.1:80', False, None)
    assert app._match_simple(['reverse_proxy https://up:8443 {',
                              'transport http {', 'tls_insecure_skip_verify',
                              '}', '}']) == ('https://up:8443', True, None)
    assert app._match_simple(['tls /etc/caddy/certs/a.crt /etc/caddy/certs/a.key',
                              'reverse_proxy 127.0.0.1:80']) == \
        ('127.0.0.1:80', False, ('/etc/caddy/certs/a.crt', '/etc/caddy/certs/a.key'))
    assert app._match_simple([]) is None
    assert app._match_simple(['tls internal', 'reverse_proxy x']) is None
    assert app._match_simple(['encode gzip', 'reverse_proxy x']) is None
    assert app._match_simple(['reverse_proxy a b']) is None   # two upstreams


def test_render_matches_parser_roundtrip():
    # What we write must be recognized as simple when read back.
    for skip in (False, True):
        for tls in (None, ('/etc/caddy/certs/s.crt', '/etc/caddy/certs/s.key')):
            text = app._render_site('app.example.com:8000', '127.0.0.1:3000',
                                    skip, tls)
            blocks = app._parse_caddyfile(text)
            assert len(blocks) == 1
            assert blocks[0]['label'] == 'app.example.com:8000'
            assert app._match_simple(blocks[0]['body']) == ('127.0.0.1:3000', skip, tls)


# ─── Validation ──────────────────────────────────────────────────────────

def test_validate_site_accepts_normal_input():
    assert app._validate_site('app.example.com', '127.0.0.1:8080') is None
    assert app._validate_site('*.example.com:8443', 'https://backend:443') is None
    assert app._validate_site('localhost', 'unit-1.internal') is None


@pytest.mark.parametrize('host,upstream', [
    ('', '127.0.0.1:80'),
    ('bad host', '127.0.0.1:80'),
    ('app.example.com; rm -rf /', '127.0.0.1:80'),
    ('app.example.com', ''),
    ('app.example.com', '127.0.0.1:80 evil'),
    ('app.example.com', 'ftp://x'),
])
def test_validate_site_rejects_bad_input(host, upstream):
    assert app._validate_site(host, upstream) is not None


# ─── Status ──────────────────────────────────────────────────────────────

def test_status_not_available_without_caddy(monkeypatch):
    monkeypatch.setattr(app.shutil, 'which', lambda n: None)
    st = app._caddy_status()
    assert not st['available'] and st['sites'] == []


def test_status_reads_version_active_and_sites(tmp_path, monkeypatch):
    cf = tmp_path / 'Caddyfile'
    cf.write_text(SAMPLE)
    monkeypatch.setattr(app, 'CADDYFILE', str(cf))
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/bin/caddy')
    fake, _ = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    st = app._caddy_status()
    assert st['available'] and st['active'] and st['version'] == 'v2.8.4'
    assert st['file_readable'] and len(st['sites']) == 4


def test_status_degrades_when_file_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'CADDYFILE', str(tmp_path / 'nope'))
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/bin/caddy')
    fake, _ = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    st = app._caddy_status()
    assert st['available'] and not st['file_readable'] and st['sites'] == []


# ─── Routes (through the facade test client) ─────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


@pytest.fixture
def caddy_env(tmp_path, monkeypatch):
    """caddy installed, helper present, sample Caddyfile readable."""
    cf = tmp_path / 'Caddyfile'
    cf.write_text(SAMPLE)
    helper = tmp_path / 'helper'
    helper.write_text('#!/bin/sh\n')
    monkeypatch.setattr(app, 'CADDYFILE', str(cf))
    monkeypatch.setattr(app, 'CADDY_HELPER', str(helper))
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/bin/caddy')
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    return {'caddyfile': cf, 'helper': str(helper), 'calls': calls}


def test_site_add_appends_block_via_helper(client, caddy_env):
    r = client.post('/api/caddy/site', json={
        'host': 'new.example.com', 'upstream': '127.0.0.1:7000'})
    assert r.status_code == 200 and r.get_json()['success']
    applied = _applied(caddy_env['calls'])
    assert applied.startswith(SAMPLE.rstrip('\n') + '\n\n')
    assert applied.endswith('new.example.com {\n\treverse_proxy 127.0.0.1:7000\n}\n')
    args, _ = caddy_env['calls'][-1]
    assert args == [caddy_env['helper'], 'apply']


def test_site_add_skip_verify_renders_transport_block(client, caddy_env):
    client.post('/api/caddy/site', json={
        'host': 'new.example.com', 'upstream': 'https://127.0.0.1:8443',
        'skip_tls_verify': True})
    applied = _applied(caddy_env['calls'])
    assert 'tls_insecure_skip_verify' in applied
    # The block we just wrote parses back as an editable simple site.
    sites = app._sites(app._parse_caddyfile(applied))
    new = next(s for s in sites if s['addresses'] == ['new.example.com'])
    assert new['simple'] and new['skip_tls_verify']


def test_site_add_rejects_duplicate_and_bad_input(client, caddy_env):
    r = client.post('/api/caddy/site', json={
        'host': 'photos.example.com', 'upstream': '127.0.0.1:1'})
    assert r.status_code == 400 and 'already exists' in r.get_json()['error']
    r = client.post('/api/caddy/site', json={'host': 'x y', 'upstream': 'a:1'})
    assert r.status_code == 400
    # b.example.com only appears as a secondary address — still a duplicate.
    r = client.post('/api/caddy/site', json={
        'host': 'b.example.com', 'upstream': '127.0.0.1:1'})
    assert r.status_code == 400 and 'already exists' in r.get_json()['error']
    assert _applied(caddy_env['calls']) is None


def test_site_delete_splices_block_out(client, caddy_env):
    r = client.post('/api/caddy/site/delete', json={'host': 'photos.example.com'})
    assert r.get_json()['success']
    applied = _applied(caddy_env['calls'])
    assert 'photos.example.com' not in applied
    for kept in ('books.example.com', 'a.example.com', 'complex.example.com',
                 'email admin@example.com', 'import conf.d/*.caddy'):
        assert kept in applied
    assert '\n\n\n' not in applied            # separator blank line swallowed


def test_site_delete_unknown_404(client, caddy_env):
    r = client.post('/api/caddy/site/delete', json={'host': 'ghost.example.com'})
    assert r.status_code == 404
    assert _applied(caddy_env['calls']) is None


def test_site_update_replaces_in_place(client, caddy_env):
    r = client.post('/api/caddy/site/update', json={
        'host': 'photos.example.com',
        'new': {'host': 'pics.example.com', 'upstream': '127.0.0.1:2283'}})
    assert r.get_json()['success']
    applied = _applied(caddy_env['calls'])
    assert 'photos.example.com' not in applied
    # Replaced at the same position: pics comes before the multi-address block.
    assert applied.index('pics.example.com') < applied.index('a.example.com')


def test_site_update_refuses_complex_and_multi_address(client, caddy_env):
    for host in ('complex.example.com', 'a.example.com'):
        r = client.post('/api/caddy/site/update', json={
            'host': host, 'new': {'host': host, 'upstream': '127.0.0.1:1'}})
        assert r.status_code == 400
        assert 'edit the Caddyfile' in r.get_json()['error']
    r = client.post('/api/caddy/site/update', json={
        'host': 'ghost.example.com',
        'new': {'host': 'g.example.com', 'upstream': '127.0.0.1:1'}})
    assert r.status_code == 404
    # Renaming onto an existing host is refused.
    r = client.post('/api/caddy/site/update', json={
        'host': 'photos.example.com',
        'new': {'host': 'books.example.com', 'upstream': '127.0.0.1:1'}})
    assert r.status_code == 400 and 'already exists' in r.get_json()['error']
    assert _applied(caddy_env['calls']) is None


def test_file_get_and_save_verbatim(client, caddy_env):
    r = client.get('/api/caddy/file')
    j = r.get_json()
    assert j['content'] == SAMPLE and j['editable']
    new = 'app.example.com {\n\treverse_proxy 127.0.0.1:1234\n}\n'
    r = client.post('/api/caddy/file', json={'content': new})
    assert r.get_json()['success']
    assert _applied(caddy_env['calls']) == new
    r = client.post('/api/caddy/file', json={'content': '   '})
    assert r.status_code == 400


def test_apply_failure_surfaces_helper_stderr(client, caddy_env, monkeypatch):
    fake, calls = _fake_run(apply_rc=1)
    monkeypatch.setattr(app, 'run', lambda args, **kw:
                        ('', 'caddy validate rejected the config: boom', 1)
                        if args[1:2] == ['apply'] else fake(args, **kw))
    r = client.post('/api/caddy/site', json={
        'host': 'new.example.com', 'upstream': '127.0.0.1:7000'})
    assert r.status_code == 400
    assert 'rejected' in r.get_json()['error']


def test_mutations_refused_without_helper(client, caddy_env, monkeypatch):
    monkeypatch.setattr(app, 'CADDY_HELPER', str(caddy_env['caddyfile']) + '.nohelper')
    for url, body in (('/api/caddy/site',
                       {'host': 'n.example.com', 'upstream': 'a:1'}),
                      ('/api/caddy/file', {'content': 'x {\n}\n'})):
        r = client.post(url, json=body)
        assert r.status_code == 400
        assert 'helper' in r.get_json()['error']
    assert _applied(caddy_env['calls']) is None


def test_file_routes_403_when_unreadable(client, caddy_env, monkeypatch):
    monkeypatch.setattr(app, 'CADDYFILE', str(caddy_env['caddyfile']) + '.gone')
    assert client.get('/api/caddy/file').status_code == 403
    r = client.post('/api/caddy/site', json={'host': 'n.example.com',
                                             'upstream': 'a:1'})
    assert r.status_code == 403


def test_get_status_route(client, caddy_env):
    j = client.get('/api/caddy').get_json()
    assert j['available'] and j['editable'] and j['file_readable']
    assert len(j['sites']) == 4


# ─── TLS pairs + certificate replacement ─────────────────────────────────

SAMPLE_TLS = """books.example.com {
\ttls /etc/caddy/certs/star.crt /etc/caddy/certs/star.key
\treverse_proxy 127.0.0.1:8000
}

host.example.com:8000 {
\ttls /etc/caddy/certs/star.crt /etc/caddy/certs/star.key
\treverse_proxy 127.0.0.1:8000
}
"""

CERT_PEM = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----'
KEY_PEM = '-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----'


def test_tls_pairs_dedupe_and_site_fields():
    blocks = app._parse_caddyfile(SAMPLE_TLS)
    assert app._tls_pairs(blocks) == [('/etc/caddy/certs/star.crt',
                                       '/etc/caddy/certs/star.key')]
    sites = app._sites(blocks)
    assert all(s['simple'] for s in sites)
    assert all(s['tls_cert'] == '/etc/caddy/certs/star.crt' for s in sites)
    assert sites[1]['addresses'] == ['host.example.com:8000']


@pytest.fixture
def caddy_tls_env(tmp_path, monkeypatch):
    cf = tmp_path / 'Caddyfile'
    cf.write_text(SAMPLE_TLS)
    helper = tmp_path / 'helper'
    helper.write_text('#!/bin/sh\n')
    monkeypatch.setattr(app, 'CADDYFILE', str(cf))
    monkeypatch.setattr(app, 'CADDY_HELPER', str(helper))
    monkeypatch.setattr(app.shutil, 'which', lambda n: '/usr/bin/caddy')
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    return {'helper': str(helper), 'calls': calls}


def test_status_lists_cert_pairs(client, caddy_tls_env):
    j = client.get('/api/caddy').get_json()
    assert len(j['certs']) == 1
    assert j['certs'][0]['cert'] == '/etc/caddy/certs/star.crt'
    assert j['certs'][0]['key'] == '/etc/caddy/certs/star.key'
    assert j['certs'][0]['present'] is False   # no such file on the test box


def test_site_add_with_tls_pair(client, caddy_tls_env):
    r = client.post('/api/caddy/site', json={
        'host': 'new.example.com:9000', 'upstream': '127.0.0.1:9000',
        'tls_cert': '/etc/caddy/certs/star.crt',
        'tls_key': '/etc/caddy/certs/star.key'})
    assert r.get_json()['success']
    applied = _applied(caddy_tls_env['calls'])
    assert applied.endswith('new.example.com:9000 {\n'
                            '\ttls /etc/caddy/certs/star.crt /etc/caddy/certs/star.key\n'
                            '\treverse_proxy 127.0.0.1:9000\n}\n')


def test_site_add_rejects_unlisted_tls_pair(client, caddy_tls_env):
    r = client.post('/api/caddy/site', json={
        'host': 'new.example.com', 'upstream': '127.0.0.1:9000',
        'tls_cert': '/etc/passwd', 'tls_key': '/etc/shadow'})
    assert r.status_code == 400
    assert 'not referenced' in r.get_json()['error']
    assert _applied(caddy_tls_env['calls']) is None


def test_site_update_keeps_tls_line(client, caddy_tls_env):
    r = client.post('/api/caddy/site/update', json={
        'host': 'host.example.com:8000',
        'new': {'host': 'host.example.com:8000', 'upstream': '127.0.0.1:8100',
                'tls_cert': '/etc/caddy/certs/star.crt',
                'tls_key': '/etc/caddy/certs/star.key'}})
    assert r.get_json()['success']
    applied = _applied(caddy_tls_env['calls'])
    assert applied.count('tls /etc/caddy/certs/star.crt') == 2
    assert '127.0.0.1:8100' in applied


def test_cert_replace_calls_helper_with_json(client, caddy_tls_env):
    r = client.post('/api/caddy/cert', json={
        'cert_path': '/etc/caddy/certs/star.crt',
        'key_path': '/etc/caddy/certs/star.key',
        'cert': CERT_PEM, 'key': KEY_PEM})
    assert r.get_json()['success']
    args, input_data = caddy_tls_env['calls'][-1]
    assert args == [caddy_tls_env['helper'], 'cert',
                    '/etc/caddy/certs/star.crt', '/etc/caddy/certs/star.key']
    import json as _json
    payload = _json.loads(input_data)
    assert payload == {'cert': CERT_PEM, 'key': KEY_PEM}


def test_cert_replace_rejects_unknown_pair_and_bad_pem(client, caddy_tls_env):
    r = client.post('/api/caddy/cert', json={
        'cert_path': '/etc/passwd', 'key_path': '/etc/shadow',
        'cert': CERT_PEM, 'key': KEY_PEM})
    assert r.status_code == 404
    r = client.post('/api/caddy/cert', json={
        'cert_path': '/etc/caddy/certs/star.crt',
        'key_path': '/etc/caddy/certs/star.key',
        'cert': 'not pem', 'key': KEY_PEM})
    assert r.status_code == 400
    r = client.post('/api/caddy/cert', json={
        'cert_path': '/etc/caddy/certs/star.crt',
        'key_path': '/etc/caddy/certs/star.key',
        'cert': CERT_PEM, 'key': 'not pem'})
    assert r.status_code == 400
    assert not any(a[1:2] == ['cert'] for a, _ in caddy_tls_env['calls'])


# ─── Registration ────────────────────────────────────────────────────────

def test_caddy_module_registered():
    assert 'caddy' in app.MODULE_IDS
    assert app._DESCRIPTORS['caddy']['category'] == 'Web'
    assert 'caddy' in app.SYSTEM_SERVICES
    assert app.SYSTEM_SERVICES['caddy']['alert'] is False
