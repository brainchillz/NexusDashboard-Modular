"""The shipped example plugins must always compile through the REAL
validator — the docs can't rot. (APP_VERSION is patched to the 3.0.0 floor
the examples declare, so this holds on pre-release trees too.)"""
import os

import pytest

from nexusdash.plugins import plugin_yaml

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'examples', 'plugins')


@pytest.fixture(autouse=True)
def app_version_3(monkeypatch):
    monkeypatch.setattr(plugin_yaml, 'APP_VERSION', '3.0.0')


def test_hello_world_compiles():
    desc, err = plugin_yaml.compile_manifest(os.path.join(EXAMPLES, 'hello-world'))
    assert err is None
    assert desc['id'] == 'hello-world'
    assert desc['nav']['pages'][0]['id'] == 'hello-world'
    types = [w['type'] for w in desc['ui_pages'][0]['widgets']]
    assert types == ['markdown', 'command_table', 'link']
    # unprivileged by design — the no-sudoers-needed example
    # (sanitized ui carries no exec keys; check the compiled route exists)
    assert any(r.rule == '/api/plugin/hello-world/widget/0/1'
               for r in _rules(desc))


def test_wireguard_compiles_with_service():
    desc, err = plugin_yaml.compile_manifest(os.path.join(EXAMPLES, 'wireguard'))
    assert err is None
    assert desc['services']['wireguard']['service'] == 'wg-quick@wg0'
    assert desc['services']['wireguard']['alert'] is False
    assert desc['dashboard_card'] == {'type': 'service_status',
                                      'unit': 'wg-quick@wg0'}
    peers = desc['ui_pages'][0]['widgets'][1]
    assert peers['type'] == 'command_table'
    assert 'command' not in peers            # sanitized for the browser
    # the secret-column rule: no column may select field 1 (preshared key)
    assert all(c.get('index') != 1 for c in peers['columns'])


def _rules(desc):
    from flask import Flask
    a = Flask(__name__)
    a.register_blueprint(desc['blueprint'])
    return a.url_map.iter_rules()


def test_example_readmes_exist():
    for name in ('hello-world', 'wireguard'):
        assert os.path.isfile(os.path.join(EXAMPLES, name, 'README.md'))
