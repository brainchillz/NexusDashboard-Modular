"""Feature-module registry — descriptor registration, hook dispatch, and HARD
module disable.

Each feature module exposes a MODULE descriptor:

    MODULE = dict(
        id='zfs', label='ZFS Pools', category='Storage MGMT',
        blueprint=bp,              # Flask blueprint holding the routes
        services={...},            # optional: merged into SYSTEM_SERVICES
        summary=fn,                # optional: () -> dict merged into /api/summary
        alerts=fn,                 # optional: () -> [{key, message}]
        metrics=fn,                # optional: () -> [prometheus lines]
        history=fn,                # optional: () -> [(metric, label, value)]
        cli={'name-tick': fn},     # optional: CLI subcommands owned by the module
    )

create_app() registers every descriptor AND every blueprint, disabled or not
(so every node carries all modules and a toggle from the Modules page takes
effect immediately — no restart, no 404). Disable is enforced by the runtime
gate: require_login refuses requests to a disabled module's endpoints with 403.

Carve-out preserved from the single-file design: service management
(/api/service/*) and the Services/status pages live in core blueprints and are
never module-gated, so a disabled module's daemon can still be controlled.

Aggregators call module_hooks(kind) which yields hooks of ENABLED modules only,
so summary/alerts/metrics/history skip disabled modules uniformly.

The legacy per-subsystem aggregation inside core/summary.py etc. is kept inline
(verbatim from the single-file app — it already honors the disabled set and is
covered by the test suite); NEW modules contribute via hooks instead.
"""
import os
import json
from flask import Blueprint, jsonify, request, send_from_directory

from .config import APP_DIR, write_json_atomic
from .runcmd import err

bp = Blueprint('registry', __name__)

MODULES_FILE = os.environ.get('DASHBOARD_MODULES_FILE', os.path.join(APP_DIR, 'modules.json'))

# Live registries, filled by register_module() during create_app(). These are
# the SAME objects the facade and the feature modules import — they fill in
# place, so `app.MODULES` / `app.MODULE_IDS` behave exactly as before.
MODULES = []            # [{'id','label','category'}] in nav order
MODULE_IDS = set()
DEFAULT_OFF = set()     # ids disabled unless the operator explicitly enables them
_DESCRIPTORS = {}       # id -> full descriptor
_BP_TO_MODULE = {}      # blueprint name -> module id
_LOADED = set()         # module ids whose blueprint is actually registered
_SOURCES = {}           # id -> 'builtin' | 'plugin' | 'yaml'
_PLUGIN_ERRORS = {}     # plugin name -> load-failure detail (admin-only)


def register_module(desc, source='builtin'):
    """Declare a feature module (idempotent). Does NOT attach the blueprint —
    create_app registers it separately (always, even when disabled). A
    descriptor with `default_enabled=False` is OFF until explicitly enabled
    from the Modules page (used for the Prometheus endpoint, which can serve
    host telemetry unauthenticated — opt-in, not default-on)."""
    mid = desc['id']
    if mid in _DESCRIPTORS:
        return
    _DESCRIPTORS[mid] = desc
    MODULES.append({'id': mid, 'label': desc['label'], 'category': desc['category']})
    MODULE_IDS.add(mid)
    _SOURCES[mid] = source
    if not desc.get('default_enabled', True):
        DEFAULT_OFF.add(mid)
    blueprint = desc.get('blueprint')
    if blueprint is not None:
        _BP_TO_MODULE[blueprint.name] = mid


def mark_loaded(mid):
    _LOADED.add(mid)


def reset():
    """Clear ALL registry state — in place, never rebinding (the app.py facade
    and every `from .registry import MODULES`-style binding hold references to
    these exact objects). Destructive: a reset MUST be followed by a fresh
    create_app() (or an explicit restore) before anything touches the registry
    again. Test-only in practice; see the fresh_registry fixture."""
    MODULES.clear()
    MODULE_IDS.clear()
    DEFAULT_OFF.clear()
    _DESCRIPTORS.clear()
    _BP_TO_MODULE.clear()
    _LOADED.clear()
    _SOURCES.clear()
    _PLUGIN_ERRORS.clear()


def finalize():
    """Rebuild the merged core tables from registered descriptors. Called once
    by create_app() after every descriptor is registered; idempotent (each
    rebuild starts from its seed and mutates its target in place — the facade
    binds these exact objects).

    Consumes: `services` + `services_overrides` (-> SYSTEM_SERVICES),
    `tasks` (-> MANAGED_TASKS), `history_metrics` (-> HISTORY_METRICS).
    Lazy imports: services/tasks/history import this module at top level."""
    from . import services as _services
    from . import tasks as _tasks
    from . import history as _history
    svc_contribs, svc_overrides, task_contribs = [], [], []
    for m in MODULES:
        desc = _DESCRIPTORS.get(m['id'], {})
        if desc.get('services'):
            svc_contribs.append((m['id'], desc['services']))
        if desc.get('services_overrides'):
            svc_overrides.append(desc['services_overrides'])
        task_contribs.extend(desc.get('tasks') or [])
        _history.HISTORY_METRICS.update(desc.get('history_metrics') or ())
    _services.rebuild_services(svc_contribs, svc_overrides)
    _tasks.rebuild_tasks(task_contribs)


def module_for_endpoint(endpoint):
    """Map a Flask endpoint ('zfs.zfs_pools') to its module id, or None for
    core/system endpoints (auth, audit, services, logs, network, summary, …)."""
    if not endpoint or '.' not in endpoint:
        return None
    return _BP_TO_MODULE.get(endpoint.split('.', 1)[0])


def module_hooks(kind):
    """Yield (module_id, hook) for every ENABLED module providing `kind`
    ('summary' | 'alerts' | 'metrics' | 'history'). The uniform skip point for
    disabled modules across all aggregators."""
    disabled = load_disabled_modules()
    for m in MODULES:
        desc = _DESCRIPTORS.get(m['id'], {})
        fn = desc.get(kind)
        if fn is not None and m['id'] not in disabled:
            yield m['id'], fn


def cli_commands():
    """CLI subcommands contributed by registered modules. First registration
    wins on a duplicate name: CLI names are systemd-facing (timers call
    `python app.py <name>`), so a later-registered module — in particular an
    operator plugin — must never shadow an existing tick command."""
    cmds = {}
    for desc in _DESCRIPTORS.values():
        for name, fn in (desc.get('cli') or {}).items():
            cmds.setdefault(name, fn)
    return cmds


def _load_modules_file():
    try:
        with open(MODULES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_disabled_modules():
    data = _load_modules_file()
    # Keep only ids we still recognize (a removed module shouldn't linger).
    disabled = {m for m in data.get('disabled', []) if m in MODULE_IDS}
    explicitly_enabled = set(data.get('enabled', []))
    # Default-off modules (e.g. the Prometheus endpoint) are disabled unless the
    # operator has explicitly enabled them from the Modules page.
    for mid in DEFAULT_OFF:
        if mid in MODULE_IDS and mid not in explicitly_enabled:
            disabled.add(mid)
    return disabled


def _enabled_module_ids():
    """Enabled module ids — the node's advertised capabilities. Consumed by a
    cluster controller (via /api/me) for per-node capability discovery and
    node-type auto-classification."""
    disabled = load_disabled_modules()
    return [m['id'] for m in MODULES if m['id'] not in disabled]


# ─── Nav manifest (/api/modules/nav) ────────────────────────────────────
# The frontend renders the ENTIRE sidebar from this manifest — built-in
# modules, plugins, and the core System pages alike (nothing is hand-written
# in index.html anymore). Modules declare their nav via the descriptor `nav`
# key; the core pages (which belong to no module) are declared here.
# Item `order` interleaves within a category: the core System pages use
# 10..110 and firewall's descriptor slots itself at 50 (between Network and
# My Account), reproducing the pre-3.0 sidebar exactly.

_CORE_NAV_CAT = {'cat': 'system', 'label': 'System', 'order': 100}
_CORE_NAV_PAGES = [
    {'id': 'services', 'label': 'Services', 'icon': 'toggle', 'order': 10},
    {'id': 'tasks', 'label': 'Scheduled Tasks', 'icon': 'cal', 'order': 20},
    {'id': 'logs', 'label': 'Logs', 'icon': 'log', 'order': 30},
    {'id': 'network', 'label': 'Network', 'icon': 'net',
     'admin_only': True, 'order': 40},
    {'id': 'account', 'label': 'My Account', 'icon': 'user', 'order': 60},
    {'id': 'users', 'label': 'Users & Tokens', 'icon': 'users',
     'admin_only': True, 'order': 70},
    {'id': 'notifications', 'label': 'Notifications', 'icon': 'bell',
     'admin_only': True, 'order': 80},
    {'id': 'certificate', 'label': 'Certificate', 'icon': 'cert',
     'admin_only': True, 'order': 90},
    {'id': 'audit', 'label': 'Audit Log', 'icon': 'list',
     'admin_only': True, 'order': 100},
    {'id': 'modules', 'label': 'Modules', 'icon': 'sli',
     'admin_only': True, 'order': 110},
]


def _nav_manifest():
    """Assemble nav categories from the core pages + every registered
    module's `nav` declaration. First declaration of a category wins its
    label/order; items sort by (order, declaration sequence) — a module
    page's order defaults to its module's own `order` key."""
    cats = {}
    seq = 0

    def add(cat, label, order, item, item_order):
        nonlocal seq
        c = cats.setdefault(cat, {'cat': cat, 'label': label, 'order': order,
                                  '_seq': seq, 'items': []})
        c['items'].append((item_order, seq, item))
        seq += 1

    for p in _CORE_NAV_PAGES:
        add(_CORE_NAV_CAT['cat'], _CORE_NAV_CAT['label'], _CORE_NAV_CAT['order'],
            {'page': p['id'], 'label': p['label'], 'icon': p['icon'],
             'module': None, 'admin_only': bool(p.get('admin_only'))},
            p['order'])
    for m in MODULES:
        desc = _DESCRIPTORS.get(m['id'], {})
        nav = desc.get('nav')
        if not nav:
            continue
        for p in nav.get('pages', []):
            item = {'page': p['id'], 'label': p.get('label', m['label']),
                    'icon': p.get('icon'), 'module': m['id'],
                    'admin_only': bool(p.get('admin_only'))}
            if p.get('icon_paths'):
                item['icon_paths'] = p['icon_paths']
            add(nav['cat'], m['category'], nav.get('cat_order', 1000),
                item, p.get('order', desc.get('order', 1000)))

    out = []
    for c in sorted(cats.values(), key=lambda c: (c['order'], c['_seq'])):
        items = [it for _o, _s, it in sorted(c['items'],
                                             key=lambda t: (t[0], t[1]))]
        out.append({'cat': c['cat'], 'label': c['label'],
                    'order': c['order'], 'items': items})
    return out


@bp.route('/api/modules/nav')
def modules_nav_get():
    """The full UI manifest: per-module state + extras (plugin assets and
    declarative pages arrive with the plugin tiers) and the nav categories.
    /api/modules keeps its exact pre-3.0 shape for the controller; this
    endpoint is the frontend's single fetch."""
    disabled = load_disabled_modules()
    mods = []
    for m in MODULES:
        desc = _DESCRIPTORS.get(m['id'], {})
        mods.append({**m,
                     'enabled': m['id'] not in disabled,
                     'loaded': m['id'] in _LOADED,
                     'installed': m['id'] in _LOADED,
                     'builtin': _SOURCES.get(m['id'], 'builtin') == 'builtin',
                     'version': desc.get('version'),
                     'assets': desc.get('assets'),
                     'dashboard_card': desc.get('dashboard_card'),
                     'ui_pages': desc.get('ui_pages')})
    return jsonify({'modules': mods, 'nav': {'categories': _nav_manifest()}})


# ─── Plugins (out-of-tree modules) ──────────────────────────────────────

@bp.route('/api/plugins')
def plugins_get():
    """Plugin inventory + load status. Failure detail and filesystem paths
    are admin-only; read-only users see id/source/status."""
    from .auth import _is_admin      # lazy: auth imports registry at top
    admin = _is_admin()
    out = []
    for mid, src in _SOURCES.items():
        if src not in ('plugin', 'yaml'):
            continue
        entry = {'id': mid, 'source': src, 'loaded': mid in _LOADED,
                 'status': 'error' if mid in _PLUGIN_ERRORS else 'ok'}
        if admin:
            entry['path'] = _DESCRIPTORS.get(mid, {}).get('_path')
            if mid in _PLUGIN_ERRORS:
                entry['error'] = _PLUGIN_ERRORS[mid]
        out.append(entry)
    return jsonify({'plugins': out})


@bp.route('/plugin-assets/<plugin>/<path:filename>')
def plugin_asset(plugin, filename):
    """Serve a loaded plugin's static/ files. Auth-gated like every non-public
    endpoint (assets inject post-login, same-origin cookies ride along);
    send_from_directory confines traversal."""
    desc = _DESCRIPTORS.get(plugin)
    if (_SOURCES.get(plugin) not in ('plugin', 'yaml')
            or not desc or not desc.get('_path')
            or plugin in _PLUGIN_ERRORS):
        return err('Unknown plugin', 404)
    return send_from_directory(os.path.join(desc['_path'], 'static'), filename)


@bp.route('/api/modules')
def modules_get():
    disabled = load_disabled_modules()
    return jsonify({'modules': [
        {**m, 'enabled': m['id'] not in disabled,
         'loaded': m['id'] in _LOADED} for m in MODULES
    ]})


@bp.route('/api/modules', methods=['POST'])
def modules_save():
    """Enable/disable modules. Accepts a single {id, enabled} toggle or a full
    {modules: {id: bool}} map. Admin-only (enforced centrally by require_login).

    Both directions take effect immediately via the runtime 403 gate (all
    blueprints are always registered). restart_recommended is kept in the
    response for callers built against the old boot-skip behavior."""
    data = request.get_json() or {}
    stored = _load_modules_file()
    disabled = {m for m in stored.get('disabled', []) if m in MODULE_IDS}
    enabled_set = {m for m in stored.get('enabled', []) if m in MODULE_IDS}
    if 'id' in data:
        updates = {data.get('id'): bool(data.get('enabled'))}
    elif isinstance(data.get('modules'), dict):
        updates = data['modules']
    else:
        return err('Nothing to update')
    for mid, enabled in updates.items():
        if mid not in MODULE_IDS:
            continue
        if mid in DEFAULT_OFF:
            # Default-off: track the positive `enabled` opt-in, not `disabled`.
            (enabled_set.add if enabled else enabled_set.discard)(mid)
            disabled.discard(mid)
        elif enabled:
            disabled.discard(mid)
        else:
            disabled.add(mid)
    write_json_atomic(MODULES_FILE,
                      {'disabled': sorted(disabled), 'enabled': sorted(enabled_set)}, 0o644)
    return jsonify({'success': True, 'restart_recommended': False})
