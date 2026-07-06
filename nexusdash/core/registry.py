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
from flask import Blueprint, jsonify, request

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


def register_module(desc):
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
    if not desc.get('default_enabled', True):
        DEFAULT_OFF.add(mid)
    blueprint = desc.get('blueprint')
    if blueprint is not None:
        _BP_TO_MODULE[blueprint.name] = mid


def mark_loaded(mid):
    _LOADED.add(mid)


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
    """CLI subcommands contributed by registered modules."""
    cmds = {}
    for desc in _DESCRIPTORS.values():
        cmds.update(desc.get('cli') or {})
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
