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
from ..core.config import *
from ..core.runcmd import run, run_safe, err, _size_to_bytes, _human_bytes, _num
from ..core.validators import *
from ..core.services import (SYSTEM_SERVICES, SERVICE_OVERRIDES, resolve_service,
                             _unit_present, RE_SERVICE, LLAMA_SERVICE, LLAMA_CONF,
                             LLAMA_MODELS_DIR, LLAMA_DEFAULT_BIN, LLAMA_URL)
from ..core.registry import load_disabled_modules, MODULES, MODULE_IDS
from ..core.auth import _is_admin, _hash_token, RE_USERNAME
from ..core.tasks import MANAGED_TASKS

bp = Blueprint('logs', __name__)

LOG_PRIORITIES = {'0', '1', '2', '3', '4', '5', '6', '7'}
RE_LOG_GREP = re.compile(r'^[\w .:@/=,+-]{0,120}$')


def _own_unit():
    """This process's own systemd unit (the unit name varies by deployment, e.g.
    storage-dashboard vs a custom name). Falls back sanely when not run by systemd."""
    try:
        with open('/proc/self/cgroup') as f:
            m = re.search(r'/([A-Za-z0-9@._-]+\.service)', f.read())
        if m:
            return m.group(1)
    except OSError:
        pass
    return UNIT_PREFIX + '.service'


def _log_sources():
    srcs = [{'id': 'dashboard', 'label': 'Dashboard (this app)', 'unit': _own_unit()}]
    for key, svc in SYSTEM_SERVICES.items():
        srcs.append({'id': 'svc:' + key, 'label': svc['name'], 'unit': svc['service']})
    for t in MANAGED_TASKS:
        srcs.append({'id': 'task:' + t['id'], 'label': t['label'] + ' (task)', 'unit': t['service']})
    return srcs


def _log_unit_for(source):
    return next((s['unit'] for s in _log_sources() if s['id'] == source), None)


@bp.route('/api/logs/sources')
def logs_sources():
    return jsonify({'sources': _log_sources()})


@bp.route('/api/logs/query')
def logs_query():
    """Filtered journald tail for one curated source. Read-only."""
    unit = _log_unit_for(request.args.get('source', ''))
    if not unit:
        return err('Unknown log source', 404)
    try:
        lines = max(10, min(int(request.args.get('lines', 200)), 2000))
    except (TypeError, ValueError):
        lines = 200
    args = ['journalctl', '-u', unit, '--no-pager', '-n', str(lines), '--output=short-iso']
    pri = request.args.get('priority', '')
    if pri:
        if pri not in LOG_PRIORITIES:
            return err('Invalid priority')
        args.append('--priority=' + pri)
    grep = (request.args.get('grep') or '').strip()
    if grep:
        if not RE_LOG_GREP.match(grep):
            return err('Invalid filter (allowed: letters, digits, space . : @ / = , + -)')
        # '=' form + a separate flag keeps a leading '-' in the pattern from being
        # read as an option; case-insensitive for convenience.
        args += ['--grep=' + grep, '--case-sensitive=no']
    out, _, rc = run(args)
    if rc != 0 and not out.strip():
        out = out or 'No logs available'
    return jsonify({'unit': unit, 'logs': out or 'No matching log entries'})

# ─── Service Control ──────────────────────────────────────────────────

