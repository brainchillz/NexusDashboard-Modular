"""Make `import app` work no matter where pytest is invoked from.

The tests import the dashboard module and exercise its pure functions
(validators, parsers, protection guards). Importing `app` is side-effect-free:
it defines the Flask app and helpers but never calls ensure_bootstrap()/app.run()
(those live under `if __name__ == '__main__'`).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point plugin discovery at an empty scratch dir BEFORE anything imports app:
# a dev box's real <APP_DIR>/plugins must never leak into the suite (it would
# break the exact-ordered module-id assertions). Tests that exercise the
# loader monkeypatch this env var at a fresh create_app().
os.environ.setdefault('DASHBOARD_PLUGINS_DIR', tempfile.mkdtemp(prefix='nxd-test-plugins-'))

import pytest


@pytest.fixture
def fresh_registry():
    """Snapshot -> reset -> yield -> restore the module registry.

    create_app() runs ONCE at `import app`, so registry globals are shared
    process state across the whole suite. Tests that build a second app
    (nexusdash.create_app()) must start from an empty registry and put the
    canonical state back afterwards. Restore mutates IN PLACE — the facade and
    every `from .registry import MODULES`-style binding hold references to
    these exact objects, so rebinding would silently desynchronize them.
    """
    from nexusdash.core import registry
    snap = (list(registry.MODULES), set(registry.MODULE_IDS),
            set(registry.DEFAULT_OFF), dict(registry._DESCRIPTORS),
            dict(registry._BP_TO_MODULE), set(registry._LOADED),
            dict(registry._SOURCES), dict(registry._PLUGIN_ERRORS))
    registry.reset()
    yield registry
    registry.reset()
    registry.MODULES.extend(snap[0])
    registry.MODULE_IDS.update(snap[1])
    registry.DEFAULT_OFF.update(snap[2])
    registry._DESCRIPTORS.update(snap[3])
    registry._BP_TO_MODULE.update(snap[4])
    registry._LOADED.update(snap[5])
    registry._SOURCES.update(snap[6])
    registry._PLUGIN_ERRORS.update(snap[7])
