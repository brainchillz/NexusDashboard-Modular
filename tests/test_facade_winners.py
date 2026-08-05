"""Facade characterization for the discovery refactor.

The app.py facade merges every module's names into `app.*` with "later modules
win" on shared-name collisions. Exactly five names have differing objects
across owners, so their winning binding depends on _FACADE_MODULES order.
These tests pin the current winners BY IDENTITY so any reordering introduced
by discovery-driven registration fails loudly instead of silently flipping
what `monkeypatch.setattr(app, ...)` reaches.
"""
import pkgutil
import importlib

import app
import nexusdash.modules
import nexusdash.modules.containers
from nexusdash.modules import dnsmasq
from nexusdash.modules.containers import console as ct_console
from nexusdash.core import alerts as core_alerts


def test_order_sensitive_facade_winners():
    assert app.RE_DOMAIN is dnsmasq.RE_DOMAIN
    assert app.RE_IFACE is dnsmasq.RE_IFACE
    assert app.register_ws is ct_console.register_ws
    assert app.RE_COMMENT is core_alerts.RE_COMMENT
    assert app.RE_HOSTNAME is core_alerts.RE_HOSTNAME


def test_every_builtin_module_is_facade_merged():
    """Every python module under nexusdash.modules (and .containers) must be in
    _FACADE_MODULES — a module missing from the merge silently breaks
    `monkeypatch.setattr(app, ...)` for its names (no error, tests just stop
    testing). Discovery must keep feeding all of them, eagerly."""
    facade = set(id(m) for m in app._FACADE_MODULES)
    for pkg in (nexusdash.modules, nexusdash.modules.containers):
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.ispkg:      # the containers package itself; its submodules
                continue        # are checked via the second loop iteration
            mod = importlib.import_module(pkg.__name__ + '.' + info.name)
            assert id(mod) in facade, (
                f'{mod.__name__} is not merged into the app facade')
