"""Nexus Dashboard (modular) — application factory.

The single-file dashboard's app-level wiring lives here: session-cookie
hardening, the central require_login before_request guard, the single
_audit_request after_request choke point, and blueprint registration for the
core plus every feature module. Stage 2 replaces the static blueprint list
with descriptor-driven registration (and hard module disable).
"""
from flask import Flask, jsonify, send_from_directory

from .core.config import STATIC_DIR, TEMPLATES_DIR, SESSION_COOKIE_CONFIG


def create_app():
    app = Flask(__name__,
                static_folder=STATIC_DIR,
                static_url_path='/static',
                template_folder=TEMPLATES_DIR)
    app.config.update(SESSION_COOKIE_CONFIG)

    from .core import auth, audit, registry, tls, svc_actions
    from .core import summary, history, metrics, tasks, alerts
    from .core import discovery

    app.before_request(auth.require_login)
    app.after_request(audit._audit_request)

    # Core blueprints — never module-gated. Deliberately includes svc_actions
    # (/api/service/*) and the status/summary pages so a disabled module's
    # daemon can still be managed (the single-file app's carve-out). The
    # System pages that live under modules/ but are core in role (logs,
    # network) arrive via discovery's bare-blueprint shape below.
    for mod in (auth, audit, registry, tls, svc_actions,
                summary, history, metrics, tasks, alerts):
        app.register_blueprint(mod.bp)

    # Everything else is DISCOVERED — no module is named here. See
    # core/discovery.py for the shape classification and the plugin loader
    # (out-of-tree dirs under <APP_DIR>/plugins/; error-isolated, forced
    # default-off, never a boot failure).
    builtins = discovery.load_builtin_modules()
    plugins = discovery.load_plugins()

    # Bare blueprints (logs, network, the VGA console page): always-on,
    # never gated (they never enter _BP_TO_MODULE).
    for mod in discovery.bare_blueprint_modules(builtins):
        app.register_blueprint(mod.bp)

    # Feature modules — every descriptor is declared AND every blueprint is
    # registered, disabled or not, so a module enabled from the Modules page
    # works immediately (no restart, no 404). Disable is enforced entirely by
    # the runtime gate: require_login 403s a disabled module's endpoints.
    # metrics rides as `extra`: its blueprint is core (registered above), the
    # descriptor only adds the Modules-page toggle — the not-in-app.blueprints
    # guard below generalizes that case.
    descs = discovery.collect_descriptors(builtins, plugins=plugins,
                                          extra=(metrics.MODULE,))
    for desc, source in descs:
        registry.register_module(desc, source=source)
    for desc, _source in descs:
        bp = desc.get('blueprint')
        if bp is None:
            continue
        if bp.name not in app.blueprints:
            app.register_blueprint(bp)
        registry.mark_loaded(desc['id'])
    registry.finalize()

    # Console websockets (xterm.js / spice-html5 <-> daemon proxies). Not
    # blueprint-scoped, so they can't ride the runtime gate — each handler
    # re-checks its own module toggle on every connection.
    from flask_sock import Sock
    sock = Sock(app)
    for register in discovery.ws_registrars(builtins, [d for d, _ in descs]):
        register(sock)

    @app.route('/')
    def index():
        return send_from_directory(TEMPLATES_DIR, 'index.html')

    @app.route('/manifest.webmanifest')
    def web_manifest():
        """PWA manifest so the dashboard can be installed / added to a home
        screen and open standalone. No service worker (a live control panel
        must not serve stale cached state); install-to-home-screen only."""
        return jsonify({
            'name': 'Nexus Dashboard',
            'short_name': 'Nexus',
            'start_url': '/',
            'display': 'standalone',
            'background_color': '#1c1e22',
            'theme_color': '#1c1e22',
            'icons': [],
        })

    return app
