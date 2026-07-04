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

bp = Blueprint('maintenance', __name__)

MAINTENANCE_FILE = os.environ.get('DASHBOARD_MAINTENANCE_FILE',
                                  os.path.join(APP_DIR, 'maintenance.json'))
MAINT_TIMER = UNIT_PREFIX + '-maintenance.timer'
MAINT_INTERVALS = {'daily': timedelta(days=1), 'weekly': timedelta(days=7),
                   'monthly': timedelta(days=30)}


def load_maintenance():
    try:
        with open(MAINTENANCE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'scrubs': [], 'smart': []}


def save_maintenance(cfg):
    write_json_atomic(MAINTENANCE_FILE, cfg, 0o644)


def _maint_due(last_run, freq):
    iv = MAINT_INTERVALS.get(freq)
    if not iv:
        return False
    if not last_run:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last_run) >= iv
    except ValueError:
        return True


def sync_maintenance_timer():
    cfg = load_maintenance()
    active = bool(cfg.get('scrubs') or cfg.get('smart'))
    run(['systemctl', '--now', 'enable' if active else 'disable', MAINT_TIMER])


def cli_maintenance_tick():
    """Invoked by the timer: start due scrubs and SMART self-tests."""
    cfg = load_maintenance()
    now = datetime.now().isoformat(timespec='seconds')
    changed = False
    for s in cfg.get('scrubs', []):
        if _maint_due(s.get('last_run'), s.get('freq', 'monthly')):
            run(['zpool', 'scrub', s['pool']])
            s['last_run'] = now
            changed = True
            print('maintenance: scrub %s' % s['pool'], flush=True)
    for s in cfg.get('smart', []):
        if _maint_due(s.get('last_run'), s.get('freq', 'weekly')):
            run(['smartctl', '-t', s.get('type', 'short'), '/dev/' + s['device']])
            s['last_run'] = now
            changed = True
            print('maintenance: smart %s %s' % (s.get('type', 'short'), s['device']), flush=True)
    if changed:
        save_maintenance(cfg)


@bp.route('/api/maintenance')
def maintenance_get():
    cfg = load_maintenance()
    return jsonify({'scrubs': cfg.get('scrubs', []), 'smart': cfg.get('smart', []),
                    'timer_active': (run(['systemctl', 'is-active', MAINT_TIMER])[0] or '').strip() == 'active'})


@bp.route('/api/maintenance', methods=['POST'])
def maintenance_save():
    data = request.get_json() or {}
    scrubs, smart = [], []
    for s in data.get('scrubs', []):
        pool = (s.get('pool') or '').strip()
        freq = s.get('freq', 'monthly')
        if not RE_POOL.match(pool):
            return err(f'Invalid pool: {pool}')
        if freq not in ('weekly', 'monthly'):
            return err('Scrub frequency must be weekly or monthly')
        scrubs.append({'pool': pool, 'freq': freq, 'last_run': s.get('last_run', '')})
    for s in data.get('smart', []):
        dev = (s.get('device') or '').strip()
        ttype = s.get('type', 'short')
        freq = s.get('freq', 'weekly')
        if not RE_DEVNAME.match(dev):
            return err(f'Invalid device: {dev}')
        if ttype not in ('short', 'long'):
            return err('SMART test type must be short or long')
        if freq not in ('daily', 'weekly', 'monthly'):
            return err('SMART frequency must be daily, weekly or monthly')
        smart.append({'device': dev, 'type': ttype, 'freq': freq, 'last_run': s.get('last_run', '')})
    save_maintenance({'scrubs': scrubs, 'smart': smart})
    sync_maintenance_timer()
    return jsonify({'success': True})


@bp.route('/api/maintenance/smart-test', methods=['POST'])
def maintenance_smart_test():
    """Kick off a SMART self-test now (independent of any schedule)."""
    data = request.get_json() or {}
    dev = (data.get('device') or '').strip()
    ttype = data.get('type', 'short')
    if not RE_DEVNAME.match(dev):
        return err('Invalid device')
    if ttype not in ('short', 'long'):
        return err('Invalid test type')
    return jsonify(run_safe(['smartctl', '-t', ttype, '/dev/' + dev]))


# ─── Scheduled tasks (feature 04) ─────────────────────────────────────
# A read-only console over the systemd timers the dashboard manages, plus a
# "run now" trigger. Status/last-run/next-run/last-result come straight from
# systemctl (no new state file). A failed last run of an armed timer raises an
# alert through the normal _compute_alerts path.


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'maintenance', 'label': 'Maintenance', 'category': 'Storage MGMT',
          'blueprint': bp,
          'cli': {'maintenance-tick': cli_maintenance_tick}}
