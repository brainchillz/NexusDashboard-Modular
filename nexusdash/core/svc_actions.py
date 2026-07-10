"""Extracted verbatim from NexusStationDashboard app.py (Stage 1 split).
Routes converted @app.route -> @bp.route; logic unchanged."""
import os
import re
import json
import time
import hmac
import socket
import hashlib
import secrets
import shutil
import threading
import subprocess
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, jsonify, request, session, g, Response
from werkzeug.security import generate_password_hash, check_password_hash
from .config import *
from .runcmd import run, run_safe, err, _size_to_bytes, _human_bytes, _num
from .validators import *
from .services import (SYSTEM_SERVICES, SERVICE_OVERRIDES, resolve_service,
                             _unit_present, RE_SERVICE, LLAMA_SERVICE, LLAMA_CONF,
                             LLAMA_MODELS_DIR, LLAMA_DEFAULT_BIN, LLAMA_URL)
from .registry import load_disabled_modules, MODULES, MODULE_IDS
from .auth import _is_admin, _hash_token, RE_USERNAME

bp = Blueprint('svc', __name__)

def systemctl_cmd(action, service):
    return run_safe(['systemctl', action, service])

def _service_action(action, service):
    svc = resolve_service(service)
    if not svc:
        return err('Invalid service')
    return jsonify(systemctl_cmd(action, svc))

@bp.route('/api/service/<service>/start', methods=['POST'])
def service_start(service):
    return _service_action('start', service)

@bp.route('/api/service/<service>/stop', methods=['POST'])
def service_stop(service):
    return _service_action('stop', service)

@bp.route('/api/service/<service>/restart', methods=['POST'])
def service_restart(service):
    return _service_action('restart', service)

@bp.route('/api/service/<service>/enable', methods=['POST'])
def service_enable(service):
    return _service_action('enable', service)

@bp.route('/api/service/<service>/disable', methods=['POST'])
def service_disable(service):
    return _service_action('disable', service)


# ─── Installation Check ───────────────────────────────────────────────
# Reports on EVERY service, so it lives here in the never-gated service core
# (it used to sit in the minidlna module, where disabling that module 403'd
# it and took the whole Services page down with it).

def _pkg_installed(pkg):
    """Whether a system package is installed, using the platform's package
    manager (dpkg on Debian/Ubuntu, rpm on RHEL/Rocky)."""
    if FAMILY == 'rhel':
        return run(['rpm', '-q', pkg], no_sudo=True)[2] == 0
    return 'installed' in run(['dpkg-query', '-W', "-f=${Status}", pkg])[0]


@bp.route('/api/install/status')
def install_status():
    results = {}
    for key, svc in SYSTEM_SERVICES.items():
        pkg = svc.get('pkg')
        if pkg:
            results[key] = {'package': pkg, 'installed': _pkg_installed(pkg)}
        else:
            # Not apt-managed (e.g. llama.cpp): presence = unit file or binary.
            installed = _unit_present(svc['service']) or Path(svc.get('binary') or '').exists()
            results[key] = {'package': svc.get('binary') or '—', 'installed': installed}
    return jsonify(results)

# Package installation is intentionally not exposed over the API. Packages are
# provisioned at install time by install-prerequisites.sh; granting the
# network-facing service passwordless apt-get would be a root-escalation path.


# ─── ZFS Pool Management ─────────────────────────────────────────────

