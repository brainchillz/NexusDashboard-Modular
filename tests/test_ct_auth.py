import app


def test_user_role_and_count():
    users = {
        'admin': {'password': 'x', 'role': 'admin'},
        'bob': {'password': 'y', 'role': 'readonly'},
        'legacy': 'barehash',  # legacy bare-string record → treated as admin
    }
    assert app._user_role(users['admin']) == 'admin'
    assert app._user_role(users['bob']) == 'readonly'
    assert app._user_role(users['legacy']) == 'admin'
    assert app._count_admins(users) == 2


def test_token_hash_and_resolve(monkeypatch):
    secret = app.TOKEN_PREFIX + 'abcdef123456'
    rec = {'id': 'tok-1', 'name': 't', 'role': 'admin', 'hash': app._hash_token(secret)}
    monkeypatch.setattr(app, '_tokens', lambda: [rec])
    assert app._resolve_token(secret) is rec
    assert app._resolve_token(app.TOKEN_PREFIX + 'wrong') is None
    assert app._resolve_token('no-prefix') is None          # missing prefix rejected
    assert app._resolve_token('') is None


def test_modules_capabilities(monkeypatch, tmp_path):
    # With nothing disabled, every module id is a capability.
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    assert set(app._enabled_module_ids()) == app.MODULE_IDS
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'images'})
    assert 'images' not in app._enabled_module_ids()
    assert 'instances' in app._enabled_module_ids()


def test_write_json_atomic_roundtrip(tmp_path):
    p = tmp_path / 'x.json'
    app.write_json_atomic(str(p), {'a': 1})
    import json
    assert json.load(open(p)) == {'a': 1}
    # No temp litter left behind.
    assert not list(tmp_path.glob('*.tmp.*'))
