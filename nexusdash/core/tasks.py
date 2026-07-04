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

bp = Blueprint('tasks', __name__)

MANAGED_TASKS = [
    {'id': 'autosnap', 'label': 'Auto-Snapshots',
     'timer': UNIT_PREFIX + '-autosnap.timer', 'service': UNIT_PREFIX + '-autosnap.service',
     'desc': 'Take & prune scheduled ZFS snapshots'},
    {'id': 'replicate', 'label': 'Replication',
     'timer': UNIT_PREFIX + '-replicate.timer', 'service': UNIT_PREFIX + '-replicate.service',
     'desc': 'Send ZFS replication jobs to remote hosts'},
    {'id': 'alerts', 'label': 'Health Alerts',
     'timer': UNIT_PREFIX + '-alerts.timer', 'service': UNIT_PREFIX + '-alerts.service',
     'desc': 'Evaluate health and send notifications'},
    {'id': 'maintenance', 'label': 'Maintenance',
     'timer': UNIT_PREFIX + '-maintenance.timer', 'service': UNIT_PREFIX + '-maintenance.service',
     'desc': 'Run due scrubs and SMART self-tests'},
    {'id': 'history', 'label': 'Metrics History',
     'timer': UNIT_PREFIX + '-history.timer', 'service': UNIT_PREFIX + '-history.service',
     'desc': 'Sample metrics into the history store'},
]
TASK_IDS = {t['id'] for t in MANAGED_TASKS}


def _systemctl_show(unit, props):
    """Return {prop: value} from `systemctl show`. Best-effort ({} on error)."""
    args = ['systemctl', 'show', unit, '--no-pager']
    for p in props:
        args += ['-p', p]
    out, _, rc = run(args)
    d = {}
    if rc == 0:
        for line in out.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                d[k] = v
    return d


def _usec_to_epoch(v):
    """systemd *USec property (microseconds) -> unix seconds, or None if 0/empty."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n // 1_000_000 if n > 0 else None


def _task_status(t):
    tinfo = _systemctl_show(t['timer'], ['ActiveState', 'LastTriggerUSec', 'NextElapseUSecRealtime'])
    sinfo = _systemctl_show(t['service'], ['Result', 'ExecMainStatus', 'ActiveState'])
    last = _usec_to_epoch(tinfo.get('LastTriggerUSec'))
    try:
        code = int(sinfo.get('ExecMainStatus') or -1)
    except ValueError:
        code = -1
    result = sinfo.get('Result') or 'unknown'
    running = sinfo.get('ActiveState') == 'active'
    return {
        'id': t['id'], 'label': t['label'], 'desc': t['desc'], 'timer': t['timer'],
        'timer_active': tinfo.get('ActiveState') == 'active',
        'running': running,
        'last_run': last,
        'next_run': _usec_to_epoch(tinfo.get('NextElapseUSecRealtime')),
        'last_result': result,
        'exit_code': code,
        # ok is None until the task has actually run at least once.
        'ok': (result == 'success') if last is not None else None,
    }


def _task_alerts():
    """Alert on an armed timer whose most recent run failed."""
    out = []
    for t in MANAGED_TASKS:
        s = _task_status(t)
        if s['timer_active'] and s['ok'] is False:
            out.append({'key': 'task:' + t['id'],
                        'message': f"Scheduled task '{t['label']}' last run failed ({s['last_result']})"})
    return out


@bp.route('/api/tasks')
def tasks_get():
    return jsonify({'tasks': [_task_status(t) for t in MANAGED_TASKS]})


@bp.route('/api/tasks/<tid>/run', methods=['POST'])
def task_run(tid):
    if tid not in TASK_IDS:
        return err('Unknown task', 404)
    t = next(x for x in MANAGED_TASKS if x['id'] == tid)
    r = run_safe(['systemctl', 'start', t['service']])
    if not r['success']:
        return err(r.get('stderr') or 'Failed to start task', 500)
    return jsonify({'success': True})


# ─── Feature modules (nav visibility) ─────────────────────────────────
# Admins can hide whole feature areas from the left-hand navigation. This is a
# cosmetic/organizational toggle (it does not stop services or block the API) —
# the underlying endpoints keep working, so disabling a module never risks data.
# State is a single global list of disabled module ids in modules.json; a module
# is enabled unless explicitly listed as disabled (so new modules default on).
