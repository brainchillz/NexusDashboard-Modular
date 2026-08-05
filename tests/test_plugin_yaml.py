"""Declarative (plugin.yaml) tier: compiler validation, sanitization, the
widget execution surface, and gate/RBAC integration."""
import json
import textwrap

import pytest

import app
from nexusdash.core import registry
from nexusdash.plugins import plugin_yaml


GOOD_YAML = textwrap.dedent("""
    schema: 1
    id: acme
    label: Acme Backup
    category: Backup
    min_app_version: "2.0.0"
    version: "1.2"
    service:
      unit: acme-backup
      name: Acme Backup daemon
    dashboard_card: true
    pages:
      - id: acme
        label: Acme Backup
        widgets:
          - type: markdown
            title: About
            content: |
              **Acme** snapshots targets nightly. See `acme.conf`.
          - type: service_status
          - type: command_table
            title: Recent runs
            command: [acme, list, --tab]
            sudo: true
            timeout: 10
            parse: {mode: tsv, skip_lines: 1}
            columns:
              - {title: When, index: 0, transform: epoch_ago}
              - {title: Target, index: 1}
              - {title: Bytes, index: 2, transform: human_bytes}
          - type: action_button
            label: Run backup now
            command: [systemctl, start, acme-backup]
            sudo: true
            danger: true
          - type: log_tail
            unit: acme-backup
            lines: 50
            admin_only: true
          - type: link
            label: Docs
            url: https://example.com/acme
          - type: iframe
            src: https://example.com/acme/report
            height: 300
      - id: extras
        label: Extras
        widgets:
          - type: markdown
            content: extras page
""")


def write_plugin(base, name, yaml_text):
    d = base / name
    d.mkdir()
    (d / 'plugin.yaml').write_text(yaml_text)
    return d


@pytest.fixture
def fresh_app(fresh_registry, monkeypatch, tmp_path):
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


def _enable(c, mid):
    assert c.post('/api/modules', json={'id': mid, 'enabled': True}).status_code == 200


# ─── compiler happy path ────────────────────────────────────────────────

def test_compile_good_manifest(tmp_path):
    write_plugin(tmp_path, 'acme', GOOD_YAML)
    desc, err = plugin_yaml.compile_manifest(str(tmp_path / 'acme'))
    assert err is None
    assert (desc['id'], desc['label'], desc['category']) == ('acme', 'Acme Backup', 'Backup')
    assert desc['blueprint'].name == 'acme'
    # page uid namespacing: page id == plugin id keeps bare id; others prefixed
    assert [p['id'] for p in desc['ui_pages']] == ['acme', 'acme-extras']
    assert desc['nav']['cat'] == 'backup' and desc['nav']['pages'][0]['id'] == 'acme'
    # service contribution rides SYSTEM_SERVICES machinery; alert never True
    assert desc['services']['acme']['service'] == 'acme-backup'
    assert desc['services']['acme']['alert'] is False
    assert desc['services']['acme']['binary'] == '/nonexistent'
    assert desc['dashboard_card'] == {'type': 'service_status', 'unit': 'acme-backup'}
    # sanitization: no exec-surface keys in any ui widget
    for pg in desc['ui_pages']:
        for w in pg['widgets']:
            assert not ({'command', 'sudo', 'timeout', 'parse'} & set(w))
            assert '_pi' in w and '_wi' in w


def test_full_stack_gate_and_service_injection(fresh_app, monkeypatch):
    pdir, build = fresh_app
    write_plugin(pdir, 'acme', GOOD_YAML)
    fresh = build()
    c = _client(fresh, monkeypatch)
    # default-off: widget endpoint 403s with the standard gate body
    r = c.get('/api/plugin/acme/widget/0/2')
    assert r.status_code == 403 and "module 'acme' is disabled" in r.get_json()['error']
    _enable(c, 'acme')
    # service entry landed in SYSTEM_SERVICES via finalize
    assert app.SYSTEM_SERVICES['acme']['name'] == 'Acme Backup daemon'
    # /api/status carries it (never omitted, module_disabled False now)
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    monkeypatch.setattr(app, '_unit_present', lambda u: False)
    st = c.get('/api/status').get_json()
    assert st['acme']['module_disabled'] is False
    # sanitized ui_pages ship in the nav manifest
    m = next(x for x in c.get('/api/modules/nav').get_json()['modules']
             if x['id'] == 'acme')
    for pg in m['ui_pages']:
        for w in pg['widgets']:
            assert 'command' not in w and 'sudo' not in w


# ─── widget execution ───────────────────────────────────────────────────

def test_command_table_tsv_transforms(fresh_app, monkeypatch):
    pdir, build = fresh_app
    write_plugin(pdir, 'acme', GOOD_YAML)
    fresh = build()
    c = _client(fresh, monkeypatch)
    _enable(c, 'acme')
    calls = []
    out = 'HEADER\n0\tvault\t1048576\n1700000000\tmedia\t2048\n'
    monkeypatch.setattr(app, 'run',
                        lambda argv, **kw: calls.append((argv, kw)) or (out, '', 0))
    d = c.get('/api/plugin/acme/widget/0/2').get_json()
    assert d['success'] is True
    assert d['columns'] == ['When', 'Target', 'Bytes']
    assert d['rows'][0] == ['never', 'vault', '1.0M']       # epoch 0 + human_bytes
    assert d['rows'][1][1:] == ['media', '2.0K']
    assert 'ago' in d['rows'][1][0]
    # exactly the declared argv ran, with sudo (declared) and timeout
    argv, kw = calls[0]
    assert argv == ['acme', 'list', '--tab']
    assert kw == {'no_sudo': False, 'timeout': 10}


def test_command_table_failure_is_clean(fresh_app, monkeypatch):
    pdir, build = fresh_app
    write_plugin(pdir, 'acme', GOOD_YAML)
    fresh = build()
    c = _client(fresh, monkeypatch)
    _enable(c, 'acme')
    monkeypatch.setattr(app, 'run',
                        lambda *a, **k: ('', 'sudo: a password is required', 1))
    d = c.get('/api/plugin/acme/widget/0/2').get_json()
    assert d['success'] is False and 'password is required' in d['error']


def test_action_button_rbac_and_exec(fresh_app, monkeypatch):
    pdir, build = fresh_app
    write_plugin(pdir, 'acme', GOOD_YAML)
    fresh = build()
    c = _client(fresh, monkeypatch)
    _enable(c, 'acme')
    # read-only: central RBAC rejects the POST outright
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('ro', 'readonly'))
    assert c.post('/api/plugin/acme/action/0/3').status_code == 403
    # admin: declared argv executes
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    calls = []
    monkeypatch.setattr(app, 'run',
                        lambda argv, **kw: calls.append(argv) or ('started', '', 0))
    d = c.post('/api/plugin/acme/action/0/3').get_json()
    assert d['success'] is True and d['output'] == 'started'
    assert calls == [['systemctl', 'start', 'acme-backup']]


def test_admin_only_widget_blocks_readonly(fresh_app, monkeypatch):
    pdir, build = fresh_app
    write_plugin(pdir, 'acme', GOOD_YAML)
    fresh = build()
    c = _client(fresh, monkeypatch, role='readonly')
    monkeypatch.setattr(registry, 'MODULES_FILE', None)  # keep default set
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    r = c.get('/api/plugin/acme/widget/0/4')            # the log_tail widget
    assert r.status_code == 403


def test_log_tail_and_service_status_argv(fresh_app, monkeypatch):
    pdir, build = fresh_app
    write_plugin(pdir, 'acme', GOOD_YAML)
    fresh = build()
    c = _client(fresh, monkeypatch)
    _enable(c, 'acme')
    calls = []
    monkeypatch.setattr(app, 'run',
                        lambda argv, **kw: calls.append(argv) or ('line1\n', '', 0))
    d = c.get('/api/plugin/acme/widget/0/4').get_json()
    assert d['success'] is True and d['logs'] == 'line1\n'
    assert calls[0] == ['journalctl', '-u', 'acme-backup', '-n', '50', '--no-pager']
    calls.clear()
    monkeypatch.setattr(app, 'run', lambda argv, **kw: calls.append(argv) or ('active\n', '', 0))
    d = c.get('/api/plugin/acme/widget/0/1').get_json()
    assert d == {'success': True, 'unit': 'acme-backup',
                 'active': 'active', 'enabled': 'active'}
    assert calls == [['systemctl', 'is-active', 'acme-backup'],
                     ['systemctl', 'is-enabled', 'acme-backup']]


# ─── validation rejections ──────────────────────────────────────────────

def _compile(tmp_path, name, yaml_text):
    write_plugin(tmp_path, name, yaml_text)
    return plugin_yaml.compile_manifest(str(tmp_path / name))


BASE = ('schema: 1\nid: {pid}\nlabel: X\ncategory: X\n'
        'min_app_version: "2.0.0"\npages:\n  - id: main\n    label: M\n'
        '    widgets:\n{widgets}')


@pytest.mark.parametrize('yaml_text,msg', [
    ('schema: 2\nid: badplug\n', 'schema: must be 1'),
    ('schema: 1\nid: other\nlabel: X\ncategory: X\nmin_app_version: "2.0.0"\n'
     'pages: []\n', 'must equal the directory name'),
    ('schema: 1\nid: badplug\nlabel: X\ncategory: X\n'
     'min_app_version: "99.0.0"\npages: []\n', 'needs app >='),
    ('schema: 1\nid: badplug\nlabel: X\ncategory: X\nmin_app_version: "2.0"\n'
     'pages: []\n', 'pages: must be a list of 1..8'),
    (BASE.format(pid='badplug', widgets=(
        '      - type: command_table\n        command: [sudo, ls]\n'
        '        columns: [{title: A, index: 0}]\n')), "'sudo' is not allowed"),
    (BASE.format(pid='badplug', widgets=(
        '      - type: command_table\n        command: [bash, -c, ls]\n'
        '        columns: [{title: A, index: 0}]\n')), "'bash' is not allowed"),
    (BASE.format(pid='badplug', widgets=(
        '      - type: command_table\n        command: ls -la\n'
        '        columns: [{title: A, index: 0}]\n')), 'list of 1..32 strings'),
    (BASE.format(pid='badplug', widgets=(
        '      - type: command_table\n        command: [ls]\n        timeout: 900\n'
        '        columns: [{title: A, index: 0}]\n')), 'timeout must be an int 1..60'),
    (BASE.format(pid='badplug', widgets=(
        '      - type: command_table\n        command: [ls]\n'
        '        parse: {mode: regex}\n'
        '        columns: [{title: A, index: 0}]\n')), 'parse.mode'),
    (BASE.format(pid='badplug', widgets=(
        '      - type: command_table\n        command: [ls]\n'
        '        columns: [{title: A, index: 0, transform: eval}]\n')),
     'transform'),
    (BASE.format(pid='badplug', widgets='      - type: telnet\n'),
     'unknown type'),
    (BASE.format(pid='badplug', widgets=(
        '      - type: log_tail\n        unit: "bad unit; rm"\n')),
     'invalid systemd unit'),
    (BASE.format(pid='badplug', widgets=(
        '      - type: link\n        label: x\n        url: "javascript:alert(1)"\n')),
     'http(s)'),
    (BASE.format(pid='badplug', widgets=(
        '      - type: service_status\n')), 'unit missing'),
])
def test_validation_rejections(tmp_path, yaml_text, msg):
    desc, err = _compile(tmp_path, 'badplug', yaml_text)
    assert desc is None and msg in err


def test_invalid_yaml_and_size_cap(tmp_path):
    desc, err = _compile(tmp_path, 'badplug', 'schema: [unclosed\n  - {')
    assert desc is None and 'invalid YAML' in err
    big = tmp_path / 'bigplug'
    big.mkdir()
    (big / 'plugin.yaml').write_text('#' + 'x' * (300 * 1024))
    desc, err = plugin_yaml.compile_manifest(str(big))
    assert desc is None and '256KB' in err


def test_pyyaml_missing_degrades_cleanly(tmp_path, monkeypatch):
    write_plugin(tmp_path, 'acme', GOOD_YAML)
    monkeypatch.setattr(plugin_yaml, '_yaml', None)
    desc, err = plugin_yaml.compile_manifest(str(tmp_path / 'acme'))
    assert desc is None and 'PyYAML' in err


def test_yaml_plugin_source_tag(fresh_app, monkeypatch):
    pdir, build = fresh_app
    write_plugin(pdir, 'acme', GOOD_YAML)
    fresh = build()
    c = _client(fresh, monkeypatch)
    assert registry._SOURCES['acme'] == 'yaml'
    p = c.get('/api/plugins').get_json()['plugins'][0]
    assert p == {'id': 'acme', 'source': 'yaml', 'loaded': True,
                 'status': 'ok', 'path': str(pdir / 'acme')}
