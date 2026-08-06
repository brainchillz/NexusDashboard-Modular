#!/usr/bin/env python3
"""Nexus Dashboard — entrypoint AND compatibility facade.

Entrypoint: `python app.py` boots the app exactly as the single-file version
did (TLS by default, DASHBOARD_* env vars, CLI subcommands like set-password).

Facade: the test suite (and any user scripts) reference symbols as
`app.<symbol>`. Every public and underscore-prefixed name from the package
modules is re-exported here so `import app` keeps working unchanged.

Monkeypatch forwarding: tests patch collaborators via
``monkeypatch.setattr(app, 'run', fake)``. A plain re-export would only change
the facade's copy while the real code keeps calling the original — so this
module's class is swapped for one whose __setattr__ forwards writes to EVERY
package module that has the attribute (covering both ``from x import run``
bindings and direct module-attribute access). monkeypatch's undo restores
through the same path.
"""
import sys
import types

from nexusdash import create_app
from nexusdash.core import config as _m_config
from nexusdash.core import runcmd as _m_runcmd
from nexusdash.core import validators as _m_validators
from nexusdash.core import services as _m_services
from nexusdash.core import registry as _m_registry
from nexusdash.core import auth as _m_auth
from nexusdash.core import audit as _m_audit
from nexusdash.core import tls as _m_tls
from nexusdash.core import svc_actions as _m_svc_actions
from nexusdash.core import discovery as _m_discovery
from nexusdash.core import summary as _m_summary
from nexusdash.core import history as _m_history
from nexusdash.core import metrics as _m_metrics
from nexusdash.core import tasks as _m_tasks
from nexusdash.core import alerts as _m_alerts
from nexusdash.plugins import plugin_yaml as _m_plugin_yaml
from nexusdash import cli as _m_cli

# Merge order = dependency order; later modules win on (shared-object)
# collisions. The builtin feature segment comes from discovery in
# _LEGACY_FACADE_ORDER — the same sequence as the old hand-written import
# block, so the five order-sensitive winners are unchanged (pinned by
# tests/test_facade_winners.py). Out-of-tree plugins are deliberately NOT
# facade-merged: a plugin global must never shadow a core name in `app.*`;
# tests patch plugins via sys.modules['nexusdash_plugins.<id>'] instead.
_CORE_PRE = [_m_config, _m_runcmd, _m_validators, _m_services, _m_registry,
             _m_auth, _m_audit, _m_tls, _m_svc_actions, _m_discovery]
_CORE_POST = [_m_summary, _m_history, _m_metrics, _m_tasks, _m_alerts,
              _m_plugin_yaml]
_FACADE_MODULES = (_CORE_PRE + _m_discovery.load_builtin_modules() +
                   _CORE_POST + [_m_cli])

_FACADE_SKIP = {'bp', 'MODULE'}   # per-module plumbing; never re-export
_FACADE_OWNERS = {}            # name -> [modules whose namespace holds it]

for _mod in _FACADE_MODULES:
    for _name, _val in vars(_mod).items():
        if _name.startswith('__') or _name in _FACADE_SKIP:
            continue
        globals()[_name] = _val
        _FACADE_OWNERS.setdefault(_name, []).append(_mod)


class _FacadeModule(types.ModuleType):
    def __setattr__(self, name, value):
        for _owner in _FACADE_OWNERS.get(name, ()):
            setattr(_owner, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule

app = create_app()

# Opt-in reverse-proxy support (DASHBOARD_PROXY_FIX=1): trust ONE hop of
# X-Forwarded-For/-Proto so audit logs record the real client IP when a
# local TLS-terminating proxy (caddy) fronts the dashboard. Only safe when
# the backend port is unreachable except via the proxy (firewall-closed) —
# XFF is trivially spoofable on a directly reachable port. Default OFF.
if _m_config.env_bool('DASHBOARD_PROXY_FIX', False):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


if __name__ == '__main__':
    _rc = _m_cli.dispatch(sys.argv)
    if _rc is not None:
        sys.exit(_rc)
    app.secret_key = _m_auth.ensure_bootstrap()['secret_key']
    ssl_context = None
    if _m_config.TLS_ENABLED:
        _m_tls.ensure_tls_cert()
        ssl_context = (_m_config.TLS_CERT, _m_config.TLS_KEY)
    app.run(host=_m_config.DASHBOARD_BIND, port=_m_config.DASHBOARD_PORT,
            ssl_context=ssl_context, debug=False, threaded=True)
