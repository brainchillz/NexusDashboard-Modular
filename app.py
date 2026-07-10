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
from nexusdash.modules import disks as _m_disks
from nexusdash.modules import gpu as _m_gpu
from nexusdash.modules import logs as _m_logs
from nexusdash.modules import zfs as _m_zfs
from nexusdash.modules import iscsi as _m_iscsi
from nexusdash.modules import nfs as _m_nfs
from nexusdash.modules import smb as _m_smb
from nexusdash.modules import minidlna as _m_minidlna
from nexusdash.modules import replication as _m_replication
from nexusdash.modules import maintenance as _m_maintenance
from nexusdash.modules import llama as _m_llama
from nexusdash.modules import network as _m_network
from nexusdash.modules import schedules as _m_schedules
from nexusdash.modules import lvm as _m_lvm
from nexusdash.modules import mdraid as _m_mdraid
from nexusdash.modules import firewall as _m_firewall
from nexusdash.modules import caddy as _m_caddy
from nexusdash.modules import docker as _m_docker
from nexusdash.modules import docker_console as _m_dk_console
from nexusdash.modules import docker_compose as _m_dk_compose
from nexusdash.modules.containers import client as _m_ct_client
from nexusdash.modules.containers import instances as _m_ct_instances
from nexusdash.modules.containers import images as _m_ct_images
from nexusdash.modules.containers import networks as _m_ct_networks
from nexusdash.modules.containers import portforward as _m_ct_portforward
from nexusdash.modules.containers import console as _m_ct_console
from nexusdash.core import summary as _m_summary
from nexusdash.core import history as _m_history
from nexusdash.core import metrics as _m_metrics
from nexusdash.core import tasks as _m_tasks
from nexusdash.core import alerts as _m_alerts
from nexusdash import cli as _m_cli

# Merge order = dependency order; later modules win on (shared-object) collisions.
_FACADE_MODULES = [_m_config, _m_runcmd, _m_validators, _m_services, _m_registry,
                   _m_auth, _m_audit, _m_tls, _m_svc_actions,
                   _m_disks, _m_gpu, _m_logs, _m_zfs, _m_iscsi, _m_nfs, _m_smb,
                   _m_minidlna, _m_replication, _m_maintenance, _m_llama,
                   _m_network, _m_schedules, _m_lvm, _m_mdraid, _m_firewall,
                   _m_caddy, _m_docker, _m_dk_console, _m_dk_compose,
                   _m_ct_client, _m_ct_instances, _m_ct_images, _m_ct_networks,
                   _m_ct_portforward, _m_ct_console,
                   _m_summary, _m_history, _m_metrics, _m_tasks, _m_alerts,
                   _m_cli]

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


if __name__ == '__main__':
    _rc = _m_cli.dispatch(sys.argv)
    if _rc is not None:
        sys.exit(_rc)
    app.secret_key = _m_auth.ensure_bootstrap()['secret_key']
    ssl_context = None
    if _m_config.TLS_ENABLED:
        _m_tls.ensure_tls_cert()
        ssl_context = (_m_config.TLS_CERT, _m_config.TLS_KEY)
    app.run(host='0.0.0.0', port=_m_config.DASHBOARD_PORT,
            ssl_context=ssl_context, debug=False, threaded=True)
