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
    from .modules import (disks, gpu, logs, zfs, iscsi, nfs, smb, minidlna,
                          replication, maintenance, llama, network, schedules,
                          lvm, mdraid, firewall, docker, docker_compose, caddy,
                          dnsmasq)

    app.before_request(auth.require_login)
    app.after_request(audit._audit_request)

    # Core blueprints — never module-gated. Deliberately includes svc_actions
    # (/api/service/*) and the status/summary pages so a disabled module's
    # daemon can still be managed (the single-file app's carve-out), plus the
    # System pages (logs, network).
    for mod in (auth, audit, registry, tls, svc_actions, logs, network,
                summary, history, metrics, tasks, alerts):
        app.register_blueprint(mod.bp)

    # Feature modules — every descriptor is declared AND every blueprint is
    # registered, disabled or not, so a module enabled from the Modules page
    # works immediately (no restart, no 404). Disable is enforced entirely by
    # the runtime gate: require_login 403s a disabled module's endpoints.
    from .modules.containers import (instances as ct_instances,
                                     images as ct_images,
                                     networks as ct_networks,
                                     portforward as ct_portforward,
                                     console as ct_console)
    feature_modules = (disks, zfs, lvm, mdraid, schedules, replication,
                       maintenance, iscsi, nfs, smb, minidlna, llama, gpu,
                       ct_instances, ct_images, ct_networks, ct_portforward,
                       docker, docker_compose, firewall, caddy, dnsmasq)
    for mod in feature_modules:
        registry.register_module(mod.MODULE)
    for mod in feature_modules:
        app.register_blueprint(mod.MODULE['blueprint'])
        registry.mark_loaded(mod.MODULE['id'])

    # The Prometheus /metrics endpoint is a core blueprint (registered above),
    # but surfaced as a toggle on the Modules page — OFF by default, since it
    # can serve host telemetry unauthenticated. Declare its descriptor only
    # (don't re-register the blueprint).
    registry.register_module(metrics.MODULE)
    registry.mark_loaded('metrics')

    # The graphical (SPICE/VGA) console page — a plain blueprint (not a module
    # descriptor); it re-checks admin + the instances toggle itself, same as its
    # websocket. Serves the spice-html5 host page for VM instances.
    app.register_blueprint(ct_console.bp)

    # Console websockets (xterm.js / spice-html5 <-> daemon proxies). Not
    # blueprint-scoped, so they can't ride the runtime gate — each handler
    # re-checks its own module toggle on every connection.
    from .modules import docker_console
    from flask_sock import Sock
    sock = Sock(app)
    ct_console.register_ws(sock)
    docker_console.register_ws(sock)

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
