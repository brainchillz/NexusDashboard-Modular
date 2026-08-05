"""Module discovery — builtin scan + out-of-tree plugin loader.

Replaces create_app()'s hand-maintained import tuple and app.py's
hand-written facade import block.

Every python module under the scan packages is imported EAGERLY (lazy imports
would silently break the facade's monkeypatch forwarding — see app.py) and
classified by attribute shape; a module can match several shapes:

    MODULE attr           feature module: descriptor registered, blueprint
                          attached (containers/instances, zfs, caddy, ...)
    bp attr, no MODULE    always-on blueprint, registered ungated — files that
                          live under modules/ but are core in role (logs,
                          network) plus the VGA console page (containers/
                          console, which self-gates per request)
    register_ws attr      websocket registrar fn(sock) (containers/console,
                          docker_console) — websockets are not blueprint-
                          scoped, so each handler re-checks its own module
                          toggle per connection
    none of the above     imported only (containers/client; the facade still
                          needs the module object)

Ordering:
- Facade order (load_builtin_modules) follows _LEGACY_FACADE_ORDER — the old
  hand-written app.py import order — because "later modules win" on shared
  facade names and exactly five bindings are order-sensitive (pinned by
  tests/test_facade_winners.py). New files sort after the legacy set,
  alphabetically, which keeps them ahead of the core aggregators app.py
  appends last.
- Registration/nav order (collect_descriptors) is by the descriptor's int
  `order` key — nothing here depends on file names, which cannot infer ids
  anyway (llama.py -> 'llamacpp', docker_compose.py -> 'compose').
"""
import importlib
import importlib.util
import logging
import os
import re
import sys

import pkgutil

_log = logging.getLogger('nexusdash.plugins')

_SCAN_PACKAGES = ('nexusdash.modules', 'nexusdash.modules.containers')

# The exact module segment of app.py's pre-3.0 _FACADE_MODULES.
_LEGACY_FACADE_ORDER = (
    'disks', 'gpu', 'logs', 'zfs', 'iscsi', 'nfs', 'smb', 'minidlna',
    'replication', 'maintenance', 'llama', 'network', 'schedules', 'lvm',
    'mdraid', 'firewall', 'caddy', 'dnsmasq', 'docker', 'docker_console',
    'docker_compose', 'containers.client', 'containers.instances',
    'containers.images', 'containers.networks', 'containers.portforward',
    'containers.console')

_builtins_cache = None


def load_builtin_modules():
    """Import every module under the scan packages (eager, cached) and return
    them in legacy facade order."""
    global _builtins_cache
    if _builtins_cache is not None:
        return _builtins_cache
    found = {}
    for pkg_name in _SCAN_PACKAGES:
        pkg = importlib.import_module(pkg_name)
        prefix = 'containers.' if pkg_name.endswith('.containers') else ''
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.ispkg:      # the containers package is scanned explicitly
                continue
            found[prefix + info.name] = importlib.import_module(
                pkg_name + '.' + info.name)

    def sort_key(name):
        try:
            return (_LEGACY_FACADE_ORDER.index(name), name)
        except ValueError:
            return (len(_LEGACY_FACADE_ORDER), name)

    _builtins_cache = [found[n] for n in sorted(found, key=sort_key)]
    return _builtins_cache


def collect_descriptors(builtins, plugins=(), extra=()):
    """[(descriptor, source)] in registration (== nav) order.

    Sorted by (descriptor 'order', discovery sequence): builtin orders are the
    unique ints 10..230, so today's exact nav sequence is reproduced;
    plugins default to 1000 and land after every builtin, tie-broken by their
    (alphabetical) discovery sequence. Deduped by id — builtins/extra first,
    so a plugin can never displace a builtin id.

    `plugins` is [(descriptor, source)] from the plugin loader; `extra` is for
    core-resident descriptors (metrics: blueprint registered as core, the
    descriptor only adds the Modules-page toggle).
    """
    entries = []
    seen = set()
    seq = 0
    for mod in builtins:
        desc = getattr(mod, 'MODULE', None)
        if desc is None or desc['id'] in seen:
            continue
        seen.add(desc['id'])
        entries.append((desc.get('order', 1000), seq, desc, 'builtin'))
        seq += 1
    for desc in extra:
        if desc['id'] in seen:
            continue
        seen.add(desc['id'])
        entries.append((desc.get('order', 1000), seq, desc, 'builtin'))
        seq += 1
    for desc, source in plugins:
        if desc['id'] in seen:
            continue
        seen.add(desc['id'])
        entries.append((desc.get('order', 1000), seq, desc, source))
        seq += 1
    entries.sort(key=lambda e: (e[0], e[1]))
    return [(desc, source) for _order, _seq, desc, source in entries]


# ─── Out-of-tree plugins ────────────────────────────────────────────────
# One directory per plugin under <APP_DIR>/plugins/ (env DASHBOARD_PLUGINS_DIR;
# read at call time so tests can point fresh apps at tmp dirs):
#
#     plugins/<id>/plugin.py     Python tier — exposes a MODULE descriptor
#     plugins/<id>/plugin.yaml   declarative tier — compiled by
#                                nexusdash.plugins.plugin_yaml (XOR plugin.py;
#                                both present is a loud rejection)
#     plugins/<id>/static/       optional assets, served via /plugin-assets/
#     plugins/<id>/README.md     optional
#
# Trust model (documented in PLUGINS.md): a Python plugin is arbitrary code
# running in-process as the dashboard user — installing one is an explicit
# root/operator action, never something the web UI can do. The loader adds
# guard rails, not a sandbox: forced default-off (the Modules-page enable is
# the consent moment), world-writable refusal, error isolation (a broken
# plugin becomes a not-installed stub — boot NEVER fails), id rules
# (id == dirname, blueprint name == id, builtin ids can't be taken), and no
# facade merge (a plugin global must never shadow a core name in `app.*`).

RE_PLUGIN_ID = re.compile(r'^[a-z][a-z0-9-]{1,31}$')


def plugins_dir():
    from .config import APP_DIR
    return os.environ.get('DASHBOARD_PLUGINS_DIR',
                          os.path.join(APP_DIR, 'plugins'))


def _world_writable(*paths):
    for p in paths:
        try:
            if os.stat(p).st_mode & 0o002:
                return p
        except OSError:
            continue
    return None


def _stub(name, error):
    """Descriptor for a plugin that failed to load: visible on the Modules
    page as not-installed (never mark_loaded), never enabled, no routes."""
    _log.error('plugin %r REJECTED: %s', name, error)
    return {'id': name, 'label': name + ' (load failed)', 'category': 'Plugins',
            'blueprint': None, 'default_enabled': False, '_error': error}


def _load_python_plugin(name, path):
    """Import plugins/<name>/plugin.py as nexusdash_plugins.<name> and
    validate its MODULE descriptor. Returns (descriptor, error|None)."""
    modname = 'nexusdash_plugins.' + name
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    # replace any earlier load (tests build fresh apps against fresh dirs)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    desc = getattr(mod, 'MODULE', None)
    if not isinstance(desc, dict):
        return None, 'plugin.py does not define a MODULE descriptor dict'
    for key in ('id', 'label', 'category'):
        if not desc.get(key):
            return None, f'MODULE descriptor missing required key {key!r}'
    if desc['id'] != name:
        return None, f"MODULE id {desc['id']!r} must equal the directory name"
    bp = desc.get('blueprint')
    if bp is not None and bp.name != desc['id']:
        return None, (f'blueprint name {bp.name!r} must equal the plugin id '
                      '(the disable gate maps endpoints by blueprint name)')
    desc['_module'] = mod
    return desc, None


def load_plugins():
    """Scan the plugin dir; return [(descriptor, source)] ready for
    collect_descriptors. Fills registry._PLUGIN_ERRORS. Error-isolated: a
    broken plugin yields a not-installed stub, never a boot failure — but no
    sandbox is claimed (import-time side effects before a late error still
    happened)."""
    from . import registry
    from . import metrics as _metrics
    base = plugins_dir()
    out = []
    if not os.path.isdir(base):
        return out
    # ids a plugin may never take: every builtin descriptor's (registration
    # happens after this scan, so registry.MODULE_IDS may still be empty)
    taken = {getattr(m, 'MODULE', {}).get('id')
             for m in load_builtin_modules()}
    taken.discard(None)
    taken.add(_metrics.MODULE['id'])
    for name in sorted(os.listdir(base)):
        pdir = os.path.join(base, name)
        if not os.path.isdir(pdir):
            continue
        py = os.path.join(pdir, 'plugin.py')
        yml = os.path.join(pdir, 'plugin.yaml')
        has_py, has_yml = os.path.isfile(py), os.path.isfile(yml)
        try:
            if not RE_PLUGIN_ID.match(name):
                desc, err = None, ('invalid plugin directory name (want '
                                   '^[a-z][a-z0-9-]{1,31}$)')
            elif name in taken or any(d['id'] == name for d, _ in out):
                desc, err = None, 'id collides with an existing module'
            elif has_py and has_yml:
                desc, err = None, ('plugin.py AND plugin.yaml both present — '
                                   'a plugin is one tier or the other')
            elif not has_py and not has_yml:
                desc, err = None, 'no plugin.py or plugin.yaml found'
            elif _world_writable(pdir, py if has_py else yml):
                desc, err = None, 'plugin dir/file is world-writable — refused'
            elif has_py:
                desc, err = _load_python_plugin(name, py)
            else:
                from ..plugins import plugin_yaml
                desc, err = plugin_yaml.compile_manifest(pdir)
        except Exception as e:                      # noqa: BLE001
            _log.exception('plugin %r failed to load', name)
            desc, err = None, f'{type(e).__name__}: {e}'
        if err or desc is None:
            registry._PLUGIN_ERRORS[name] = err or 'unknown error'
            out.append((_stub(name, err or 'unknown error'), 'plugin'))
            continue
        # Guard rails applied to every successfully loaded plugin:
        desc['default_enabled'] = False    # operator opt-in on the Modules page
        desc['_path'] = pdir
        assets = desc.get('assets') or {}
        if assets:
            ver = str(desc.get('version') or '0')
            desc['assets'] = {
                kind: ['/plugin-assets/%s/%s?v=%s' % (name, f, ver)
                       for f in files if _asset_name_ok(f)]
                for kind, files in assets.items() if kind in ('js', 'css')}
        out.append((desc, 'plugin' if has_py else 'yaml'))
        _log.info('plugin %r loaded (%s tier)', name,
                  'python' if has_py else 'declarative')
    return out


_RE_ASSET = re.compile(r'^[A-Za-z0-9._/-]+$')


def _asset_name_ok(f):
    return bool(_RE_ASSET.match(f)) and '..' not in f and not f.startswith('/')


def bare_blueprint_modules(builtins):
    """Modules exposing a blueprint but no descriptor — always-on, never
    module-gated (they never enter _BP_TO_MODULE)."""
    return [m for m in builtins
            if getattr(m, 'bp', None) is not None
            and getattr(m, 'MODULE', None) is None]


def ws_registrars(builtins, descriptors=()):
    """Websocket registrar callables, deduped by identity: the grandfathered
    module-attr spelling (`register_ws`) plus the descriptor-key spelling
    (`websockets`, the v2 contract for plugins)."""
    regs, seen = [], set()
    for mod in builtins:
        fn = getattr(mod, 'register_ws', None)
        if callable(fn) and id(fn) not in seen:
            seen.add(id(fn))
            regs.append(fn)
    for desc in descriptors:
        fn = desc.get('websockets')
        if callable(fn) and id(fn) not in seen:
            seen.add(id(fn))
            regs.append(fn)
    return regs
