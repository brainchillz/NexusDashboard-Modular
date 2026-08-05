"""Out-of-tree plugin loader: happy path, guard rails, error isolation.

Each test builds a FRESH app (fresh_registry) against a tmp plugin dir; the
canonical registry state is restored by the fixture afterwards.
"""
import json
import os
import sys
import textwrap

import pytest

import app
from nexusdash.core import registry, discovery


GOOD_PLUGIN = textwrap.dedent("""
    from flask import Blueprint, jsonify

    bp = Blueprint('{pid}', __name__)

    @bp.route('/api/{pid}/hello')
    def hello():
        return jsonify({{'hello': '{pid}'}})

    MODULE = {{'id': '{pid}', 'label': 'Test Plugin', 'category': 'Examples',
               'blueprint': bp, 'version': '1.0',
               'assets': {{'js': ['plugin.js']}},
               'nav': {{'cat': 'examples', 'cat_order': 90, 'pages': [
                   {{'id': '{pid}', 'label': 'Test Plugin', 'icon': 'pkg'}}]}}}}
""")


def make_plugin(base, pid, body=None):
    d = base / pid
    d.mkdir()
    (d / 'plugin.py').write_text(body if body is not None
                                 else GOOD_PLUGIN.format(pid=pid))
    return d


@pytest.fixture
def fresh_app(fresh_registry, monkeypatch, tmp_path):
    """Build a fresh app against tmp plugin+modules state; returns a factory
    so tests populate the plugin dir first."""
    pdir = tmp_path / 'plugins'
    pdir.mkdir()
    monkeypatch.setenv('DASHBOARD_PLUGINS_DIR', str(pdir))
    monkeypatch.setattr(registry, 'MODULES_FILE', str(tmp_path / 'modules.json'))

    def build():
        import nexusdash
        fresh = nexusdash.create_app()
        fresh.config['TESTING'] = True
        return fresh

    return pdir, build


def _client(fresh, monkeypatch, role='admin'):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', role))
    return fresh.test_client()


def test_good_plugin_loads_default_off(fresh_app, monkeypatch):
    pdir, build = fresh_app
    d = make_plugin(pdir, 'myplug')
    (d / 'static').mkdir()
    (d / 'static' / 'plugin.js').write_text('function page_myplug() {}\n')
    fresh = build()
    c = _client(fresh, monkeypatch)

    # registered, routes live, but FORCED default-off -> gate 403s
    assert 'myplug' in registry.MODULE_IDS
    assert registry._SOURCES['myplug'] == 'plugin'
    assert 'myplug' in registry.DEFAULT_OFF
    r = c.get('/api/myplug/hello')
    assert r.status_code == 403
    assert "module 'myplug' is disabled" in r.get_json()['error']

    # operator opt-in via the Modules page -> live immediately
    assert c.post('/api/modules',
                  json={'id': 'myplug', 'enabled': True}).status_code == 200
    r = c.get('/api/myplug/hello')
    assert r.status_code == 200 and r.get_json() == {'hello': 'myplug'}

    # nav manifest: three-state + normalized asset URLs + nav category
    body = c.get('/api/modules/nav').get_json()
    m = next(x for x in body['modules'] if x['id'] == 'myplug')
    assert m['builtin'] is False and m['loaded'] is True
    assert m['installed'] is True and m['enabled'] is True
    assert m['assets'] == {'js': ['/plugin-assets/myplug/plugin.js?v=1.0']}
    cats = {c_['cat']: c_ for c_ in body['nav']['categories']}
    assert cats['examples']['items'][0]['page'] == 'myplug'
    # capabilities advertise the enabled plugin (controller discovery)
    assert 'myplug' in registry._enabled_module_ids()

    # /api/plugins inventory + asset serving + traversal confinement
    assert c.get('/api/plugins').get_json()['plugins'][0]['status'] == 'ok'
    r = c.get('/plugin-assets/myplug/plugin.js')
    assert r.status_code == 200 and b'page_myplug' in r.get_data()
    assert c.get('/plugin-assets/myplug/../plugin.py').status_code == 404


def test_broken_plugin_never_fails_boot(fresh_app, monkeypatch):
    pdir, build = fresh_app
    make_plugin(pdir, 'broken', body='raise RuntimeError("boom at import")\n')
    fresh = build()                       # must not raise
    c = _client(fresh, monkeypatch)
    # stub registered: visible, not loaded (= not installed), no routes
    assert 'broken' in registry.MODULE_IDS
    assert 'broken' not in registry._LOADED
    m = next(x for x in c.get('/api/modules/nav').get_json()['modules']
             if x['id'] == 'broken')
    assert m['installed'] is False
    p = c.get('/api/plugins').get_json()['plugins'][0]
    assert p['status'] == 'error' and 'boom at import' in p['error']
    assert c.get('/plugin-assets/broken/x.js').status_code == 404


def test_plugin_error_detail_admin_only(fresh_app, monkeypatch):
    pdir, build = fresh_app
    make_plugin(pdir, 'broken', body='raise RuntimeError("secret detail")\n')
    fresh = build()
    c = _client(fresh, monkeypatch, role='readonly')
    p = c.get('/api/plugins').get_json()['plugins'][0]
    assert p['status'] == 'error'
    assert 'error' not in p and 'path' not in p


def test_builtin_id_collision_rejected(fresh_app, monkeypatch):
    pdir, build = fresh_app
    d = pdir / 'zfs'
    d.mkdir()
    (d / 'plugin.py').write_text('MODULE = {}\n')
    build()
    # builtin zfs untouched; collision recorded
    assert registry._SOURCES['zfs'] == 'builtin'
    assert 'collides' in registry._PLUGIN_ERRORS['zfs']


def test_id_and_blueprint_rules(fresh_app, monkeypatch):
    pdir, build = fresh_app
    # id != dirname
    make_plugin(pdir, 'alpha-x', body=(
        "MODULE = {'id': 'other', 'label': 'x', 'category': 'X'}\n"))
    # blueprint name != id
    make_plugin(pdir, 'beta-x', body=textwrap.dedent("""
        from flask import Blueprint
        bp = Blueprint('wrongname', __name__)
        MODULE = {'id': 'beta-x', 'label': 'x', 'category': 'X', 'blueprint': bp}
    """))
    # bad directory name
    d = pdir / 'Bad_Name'
    d.mkdir()
    (d / 'plugin.py').write_text('MODULE = {}\n')
    build()
    assert 'must equal the directory name' in registry._PLUGIN_ERRORS['alpha-x']
    assert 'blueprint name' in registry._PLUGIN_ERRORS['beta-x']
    assert 'invalid plugin directory name' in registry._PLUGIN_ERRORS['Bad_Name']


def test_both_tiers_present_rejected(fresh_app, monkeypatch):
    pdir, build = fresh_app
    d = make_plugin(pdir, 'dualplug')
    (d / 'plugin.yaml').write_text('schema: 1\n')
    build()
    assert 'both present' in registry._PLUGIN_ERRORS['dualplug']


def test_world_writable_refused(fresh_app, monkeypatch):
    pdir, build = fresh_app
    d = make_plugin(pdir, 'looseplug')
    os.chmod(d / 'plugin.py', 0o666)
    build()
    assert 'world-writable' in registry._PLUGIN_ERRORS['looseplug']


def test_plugin_not_facade_merged(fresh_app, monkeypatch):
    pdir, build = fresh_app
    make_plugin(pdir, 'facadeplug', body=(
        "SOME_PLUGIN_GLOBAL = 42\n"
        "MODULE = {'id': 'facadeplug', 'label': 'x', 'category': 'X',\n"
        "          'blueprint': None}\n"))
    build()
    assert 'nexusdash_plugins.facadeplug' in sys.modules
    assert not hasattr(app, 'SOME_PLUGIN_GLOBAL')
    # patch path for plugin tests is the sys.modules entry, not the facade
    assert sys.modules['nexusdash_plugins.facadeplug'].SOME_PLUGIN_GLOBAL == 42


def test_plugin_orders_after_builtins(fresh_app, monkeypatch):
    pdir, build = fresh_app
    make_plugin(pdir, 'aaa-plug', body=(
        "MODULE = {'id': 'aaa-plug', 'label': 'x', 'category': 'X',\n"
        "          'blueprint': None}\n"))
    build()
    ids = [m['id'] for m in registry.MODULES]
    assert ids.index('aaa-plug') > ids.index('metrics')   # after ALL builtins
