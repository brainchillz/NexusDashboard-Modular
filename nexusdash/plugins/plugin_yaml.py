"""Declarative plugin tier — compile a plugin.yaml manifest into a regular
module descriptor with a real per-plugin Blueprint.

The security story is ONE sentence: only argv literally present in a
root-installed manifest file ever executes. There are no declared parameters
in schema v1 (a v2 design — closed literal enums substituted whole-element —
is documented in PLUGINS.md as designed-not-built): the frontend sends only
page/widget INDICES baked into static routes at compile time, so no request
data ever reaches a command line. Because argv is fixed, operators can pin
EXACT-argument sudoers grants (no wildcards — parses identically on classic
sudo and sudo-rs).

The per-plugin Blueprint is named after the plugin id, so the existing
runtime 403 gate / Modules-page toggle / capability advertising / audit all
apply with zero changes to auth or the registry. The manifest `service:` key
becomes a descriptor `services` contribution and rides registry.finalize()
into SYSTEM_SERVICES like any builtin's.

PyYAML is a guarded import: without it the tier reports cleanly per plugin
and the app boots normally (fleet nodes upgrade venvs via
`fleet-deploy.sh --deps`).
"""
import json
import os
import re
import time

from flask import Blueprint, jsonify

try:
    import yaml as _yaml
except ImportError:                                  # pragma: no cover
    _yaml = None

from ..core.config import APP_VERSION
from ..core.runcmd import run, err, _human_bytes
from ..core.services import RE_SERVICE

MAX_MANIFEST_BYTES = 256 * 1024
MAX_STDOUT_BYTES = 256 * 1024

RE_ID = re.compile(r'^[a-z][a-z0-9-]{1,31}$')
RE_ARGV0 = re.compile(r'^[A-Za-z0-9_./-]+$')
# never a shell/priv-escalation wrapper; `sudo:` is the only sudo path
ARGV0_DENY = {'sudo', 'su', 'sh', 'bash', 'dash', 'zsh', 'ksh', 'env',
              'nsenter', 'doas'}
PARSE_MODES = ('whitespace', 'tsv', 'lines', 'json_lines')
TRANSFORMS = ('none', 'epoch_ago', 'human_bytes')
WIDGET_TYPES = ('markdown', 'service_status', 'command_table',
                'action_button', 'log_tail', 'link', 'iframe')


class _Bad(Exception):
    """Manifest validation failure — message names the offending path."""


def _want(cond, where, msg):
    if not cond:
        raise _Bad(f'{where}: {msg}')


def _version_tuple(s):
    return tuple(int(x) for x in re.findall(r'\d+', str(s))[:3] or [0])


def _check_argv(argv, where):
    _want(isinstance(argv, list) and 1 <= len(argv) <= 32,
          where, 'command must be a list of 1..32 strings')
    for i, a in enumerate(argv):
        _want(isinstance(a, str) and 0 < len(a) <= 256, f'{where}[{i}]',
              'argv elements must be non-empty strings (max 256 chars)')
        _want('\x00' not in a and '\n' not in a and a.isprintable(),
              f'{where}[{i}]', 'argv element contains control characters')
    a0 = argv[0]
    _want(RE_ARGV0.match(a0) is not None, f'{where}[0]',
          'invalid command name')
    _want(os.path.basename(a0) not in ARGV0_DENY, f'{where}[0]',
          f'{os.path.basename(a0)!r} is not allowed (use the sudo: flag; '
          'shells are never allowed)')


def _check_unit(unit, where):
    _want(isinstance(unit, str) and RE_SERVICE.match(unit) is not None,
          where, 'invalid systemd unit name')


def _check_url(url, where):
    _want(isinstance(url, str) and re.match(r'^https?://[^\s]+$', url),
          where, 'url must be http(s)')


def _validate_widget(w, wi, page_where, default_unit):
    where = f'{page_where}.widgets[{wi}]'
    _want(isinstance(w, dict), where, 'widget must be a mapping')
    t = w.get('type')
    _want(t in WIDGET_TYPES, where,
          f'unknown type {t!r} (one of {", ".join(WIDGET_TYPES)})')
    if 'title' in w:
        _want(isinstance(w['title'], str) and len(w['title']) <= 80,
              where, 'title must be a string (max 80 chars)')
    _want(isinstance(w.get('admin_only', False), bool), where,
          'admin_only must be a boolean')
    if 'refresh_seconds' in w:
        _want(isinstance(w['refresh_seconds'], int)
              and 0 <= w['refresh_seconds'] <= 3600,
              where, 'refresh_seconds must be an int 0..3600')

    if t == 'markdown':
        _want(isinstance(w.get('content'), str)
              and len(w['content']) <= 64 * 1024,
              where, 'content must be a string (max 64KB)')
    elif t == 'service_status':
        unit = w.get('unit') or default_unit
        _want(bool(unit), where,
              'unit missing (and no manifest-level service.unit to default to)')
        _check_unit(unit, where + '.unit')
        w['unit'] = unit
    elif t == 'command_table':
        _check_argv(w.get('command'), where + '.command')
        _want(isinstance(w.get('sudo', False), bool), where,
              'sudo must be a boolean')
        _want(isinstance(w.get('timeout', 15), int)
              and 1 <= w.get('timeout', 15) <= 60,
              where, 'timeout must be an int 1..60 seconds')
        parse = w.get('parse') or {}
        _want(isinstance(parse, dict), where + '.parse', 'must be a mapping')
        _want(parse.get('mode', 'whitespace') in PARSE_MODES,
              where + '.parse.mode', f'one of {", ".join(PARSE_MODES)}')
        _want(isinstance(parse.get('skip_lines', 0), int)
              and parse.get('skip_lines', 0) >= 0,
              where + '.parse.skip_lines', 'must be an int >= 0')
        _want(isinstance(parse.get('max_rows', 200), int)
              and 1 <= parse.get('max_rows', 200) <= 500,
              where + '.parse.max_rows', 'must be an int 1..500')
        cols = w.get('columns')
        mode = parse.get('mode', 'whitespace')
        if mode == 'lines':
            _want(cols is None or (isinstance(cols, list) and len(cols) == 1),
                  where + '.columns', "mode 'lines' takes at most one column")
            w.setdefault('columns', [{'title': w.get('title') or 'Output'}])
        else:
            _want(isinstance(cols, list) and 1 <= len(cols) <= 12,
                  where + '.columns', 'must be a list of 1..12 columns')
        for ci, ccol in enumerate(w['columns'] if 'columns' in w else cols):
            cw = f'{where}.columns[{ci}]'
            _want(isinstance(ccol, dict) and isinstance(ccol.get('title'), str),
                  cw, 'column needs a title')
            if mode == 'json_lines':
                _want(isinstance(ccol.get('key'), str) and ccol['key'],
                      cw, "json_lines columns select by 'key'")
            elif mode != 'lines':
                _want(isinstance(ccol.get('index'), int) and ccol['index'] >= 0,
                      cw, "columns select by 'index' (int >= 0)")
            _want(ccol.get('transform', 'none') in TRANSFORMS,
                  cw + '.transform', f'one of {", ".join(TRANSFORMS)}')
    elif t == 'action_button':
        _want(isinstance(w.get('label'), str) and w['label'],
              where, 'action_button needs a label')
        _check_argv(w.get('command'), where + '.command')
        _want(isinstance(w.get('sudo', False), bool), where,
              'sudo must be a boolean')
        _want(isinstance(w.get('timeout', 15), int)
              and 1 <= w.get('timeout', 15) <= 60,
              where, 'timeout must be an int 1..60 seconds')
        _want(isinstance(w.get('confirm', True), bool), where,
              'confirm must be a boolean')
        _want(isinstance(w.get('danger', False), bool), where,
              'danger must be a boolean')
    elif t == 'log_tail':
        unit = w.get('unit') or default_unit
        _want(bool(unit), where, 'log_tail needs a unit')
        _check_unit(unit, where + '.unit')
        w['unit'] = unit
        _want(isinstance(w.get('lines', 100), int)
              and 1 <= w.get('lines', 100) <= 1000,
              where, 'lines must be an int 1..1000')
    elif t in ('link', 'iframe'):
        _check_url(w.get('url') or w.get('src'), where)
        if t == 'link':
            _want(isinstance(w.get('label'), str) and w['label'],
                  where, 'link needs a label')
        else:
            _want(isinstance(w.get('height', 480), int)
                  and 100 <= w.get('height', 480) <= 2000,
                  where, 'height must be an int 100..2000')


def _validate(man, name):
    _want(isinstance(man, dict), 'manifest', 'top level must be a mapping')
    _want(man.get('schema') == 1, 'schema', 'must be 1')
    _want(man.get('id') == name, 'id',
          f'must equal the directory name ({name!r})')
    _want(RE_ID.match(name) is not None, 'id', 'invalid plugin id')
    _want(isinstance(man.get('label'), str) and 0 < len(man['label']) <= 40
          and man['label'].isprintable(),
          'label', 'must be a printable string (max 40 chars)')
    _want(isinstance(man.get('category'), str) and 0 < len(man['category']) <= 24
          and man['category'].isprintable(),
          'category', 'must be a printable string (max 24 chars)')
    _want(bool(man.get('min_app_version')), 'min_app_version', 'is required')
    _want(_version_tuple(man['min_app_version']) <= _version_tuple(APP_VERSION),
          'min_app_version',
          f"needs app >= {man['min_app_version']} (this is {APP_VERSION})")
    svc = man.get('service')
    default_unit = None
    if svc is not None:
        _want(isinstance(svc, dict), 'service', 'must be a mapping')
        _check_unit(svc.get('unit'), 'service.unit')
        _want(isinstance(svc.get('name', ''), str), 'service.name',
              'must be a string')
        default_unit = svc['unit']
    pages = man.get('pages')
    _want(isinstance(pages, list) and 1 <= len(pages) <= 8,
          'pages', 'must be a list of 1..8 pages')
    for pi, pg in enumerate(pages):
        where = f'pages[{pi}]'
        _want(isinstance(pg, dict), where, 'page must be a mapping')
        _want(isinstance(pg.get('id'), str)
              and re.match(r'^[a-z][a-z0-9-]{0,31}$', pg['id']),
              where + '.id', 'invalid page id')
        _want(isinstance(pg.get('label'), str) and pg['label'],
              where + '.label', 'page needs a label')
        ws = pg.get('widgets')
        _want(isinstance(ws, list) and 1 <= len(ws) <= 12,
              where + '.widgets', 'must be a list of 1..12 widgets')
        for wi, w in enumerate(ws):
            _validate_widget(w, wi, where, default_unit)
    return default_unit


# ─── Widget execution (the ONLY exec path of the tier) ─────────────────

def _transform(value, kind):
    if kind == 'human_bytes':
        try:
            return _human_bytes(float(value))
        except (TypeError, ValueError):
            return str(value)
    if kind == 'epoch_ago':
        try:
            ts = int(float(value))
        except (TypeError, ValueError):
            return str(value)
        if ts <= 0:
            return 'never'
        d = max(0, int(time.time()) - ts)
        if d < 60:
            return f'{d}s ago'
        if d < 3600:
            return f'{d // 60}m ago'
        if d < 86400:
            return f'{d // 3600}h ago'
        return f'{d // 86400}d ago'
    return str(value)


def _parse_rows(out, w):
    parse = w.get('parse') or {}
    mode = parse.get('mode', 'whitespace')
    lines = out.split('\n')[parse.get('skip_lines', 0):]
    lines = [l for l in lines if l.strip()]
    max_rows = parse.get('max_rows', 200)
    truncated = len(lines) > max_rows
    cols = w['columns']
    rows, parse_errors = [], 0
    for line in lines[:max_rows]:
        if mode == 'lines':
            rows.append([line])
            continue
        if mode == 'json_lines':
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            rows.append([_transform(obj.get(c['key'], ''),
                                    c.get('transform', 'none')) for c in cols])
            continue
        parts = line.split('\t') if mode == 'tsv' else line.split()
        rows.append([_transform(parts[c['index']]
                                if c['index'] < len(parts) else '',
                                c.get('transform', 'none')) for c in cols])
    return rows, truncated, parse_errors


def _admin_blocked(w):
    if w.get('admin_only'):
        from ..core.auth import _is_admin
        if not _is_admin():
            return err('Administrator access required', 403)
    return None


def _make_table_view(w):
    def view():
        blocked = _admin_blocked(w)
        if blocked:
            return blocked
        out, stderr, rc = run(w['command'], no_sudo=not w.get('sudo', False),
                              timeout=w.get('timeout', 15))
        if rc != 0:
            return jsonify({'success': False, 'rc': rc,
                            'error': (stderr or out or 'command failed').strip()[-2000:]})
        out = out[:MAX_STDOUT_BYTES]
        rows, truncated, parse_errors = _parse_rows(out, w)
        return jsonify({'success': True,
                        'columns': [c['title'] for c in w['columns']],
                        'rows': rows, 'truncated': truncated,
                        'parse_errors': parse_errors})
    return view


def _make_service_view(w):
    def view():
        blocked = _admin_blocked(w)
        if blocked:
            return blocked
        unit = w['unit']
        active = (run(['systemctl', 'is-active', unit])[0] or '').strip() or 'inactive'
        enabled = (run(['systemctl', 'is-enabled', unit])[0] or '').strip() or 'disabled'
        return jsonify({'success': True, 'unit': unit,
                        'active': active, 'enabled': enabled})
    return view


def _make_logs_view(w):
    def view():
        blocked = _admin_blocked(w)
        if blocked:
            return blocked
        out, stderr, rc = run(['journalctl', '-u', w['unit'], '-n',
                               str(w.get('lines', 100)), '--no-pager'])
        if rc != 0:
            return jsonify({'success': False,
                            'error': (stderr or 'journalctl failed').strip()[-2000:]})
        return jsonify({'success': True, 'logs': out[-MAX_STDOUT_BYTES:]})
    return view


def _make_action_view(w):
    def view():
        # POST -> central RBAC already requires admin; per-widget admin_only
        # is therefore only meaningful on GET widgets, but re-check anyway.
        blocked = _admin_blocked(w)
        if blocked:
            return blocked
        out, stderr, rc = run(w['command'], no_sudo=not w.get('sudo', False),
                              timeout=w.get('timeout', 15))
        return jsonify({'success': rc == 0, 'rc': rc,
                        'output': (out or '').strip()[-4000:],
                        'error': (stderr or '').strip()[-2000:] or None})
    return view


_VIEW_MAKERS = {'command_table': _make_table_view,
                'service_status': _make_service_view,
                'log_tail': _make_logs_view}

# keys stripped from ui_pages before they reach any browser: the exec
# surface stays server-side; read-only users never see the exact argv.
_UI_STRIP = ('command', 'sudo', 'timeout', 'parse')


def _slug(label):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', label.lower())).strip('-')


def compile_manifest(pdir):
    """(descriptor, None) from plugins/<id>/plugin.yaml, or (None, error)."""
    name = os.path.basename(pdir.rstrip('/'))
    path = os.path.join(pdir, 'plugin.yaml')
    if _yaml is None:
        return None, ('declarative plugins unavailable: PyYAML is not '
                      'installed in this venv (pip install pyyaml)')
    try:
        if os.path.getsize(path) > MAX_MANIFEST_BYTES:
            return None, 'plugin.yaml exceeds 256KB'
        with open(path) as f:
            man = _yaml.safe_load(f)
        _validate(man, name)
    except _Bad as e:
        return None, str(e)
    except _yaml.YAMLError as e:
        return None, f'invalid YAML: {e}'
    except OSError as e:
        return None, f'cannot read plugin.yaml: {e}'

    pid = man['id']
    bp = Blueprint(pid, __name__)   # bp.name == id -> runtime gate applies
    ui_pages, nav_pages = [], []
    for pi, pg in enumerate(man['pages']):
        # page uid namespaces the SPA page id (plugins share one page-id
        # space with core); a page named after the plugin keeps the bare id
        uid = pid if pg['id'] == pid else f"{pid}-{pg['id']}"
        widgets_ui = []
        for wi, w in enumerate(pg['widgets']):
            maker = _VIEW_MAKERS.get(w['type'])
            if maker:
                bp.add_url_rule(f'/api/plugin/{pid}/widget/{pi}/{wi}',
                                f'widget_{pi}_{wi}', maker(w))
            elif w['type'] == 'action_button':
                bp.add_url_rule(f'/api/plugin/{pid}/action/{pi}/{wi}',
                                f'action_{pi}_{wi}', _make_action_view(w),
                                methods=['POST'])
            ui = {k: v for k, v in w.items() if k not in _UI_STRIP}
            ui['_pi'], ui['_wi'] = pi, wi
            widgets_ui.append(ui)
        ui_pages.append({'id': uid, 'label': pg['label'],
                         'admin_only': bool(pg.get('admin_only')),
                         'widgets': widgets_ui})
        nav_pages.append({'id': uid, 'label': pg['label'],
                          'icon': pg.get('icon') or man.get('icon') or 'pkg',
                          'admin_only': bool(pg.get('admin_only'))})

    desc = {'id': pid, 'label': man['label'], 'category': man['category'],
            'blueprint': bp, 'version': str(man.get('version') or '') or None,
            'nav': {'cat': _slug(man['category']) or pid,
                    'cat_order': int(man.get('category_order', 90)),
                    'pages': nav_pages},
            'ui_pages': ui_pages}
    svc = man.get('service')
    if svc:
        # binary must be a usable path for /api/status's Path(...).exists()
        # ('' would be PosixPath('.') -> exists() True); without one declared,
        # presence falls back to the unit-file check alone.
        desc['services'] = {pid: {
            'name': svc.get('name') or man['label'], 'service': svc['unit'],
            'pkg': None, 'binary': svc.get('binary') or '/nonexistent',
            'alert': False}}
        if man.get('dashboard_card'):
            desc['dashboard_card'] = {'type': 'service_status',
                                      'unit': svc['unit']}
    return desc, None
