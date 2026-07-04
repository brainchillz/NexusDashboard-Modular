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

bp = Blueprint('schedules', __name__)

SCHEDULES_FILE = os.environ.get('DASHBOARD_SCHEDULES_FILE', os.path.join(APP_DIR, 'schedules.json'))
AUTOSNAP_TIMER = UNIT_PREFIX + '-autosnap.timer'
FREQS = ['hourly', 'daily', 'weekly', 'monthly']
FREQ_INTERVAL = {'hourly': timedelta(hours=1), 'daily': timedelta(days=1),
                 'weekly': timedelta(days=7), 'monthly': timedelta(days=30)}


def load_schedules():
    try:
        with open(SCHEDULES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'schedules': []}


def save_schedules(cfg):
    write_json_atomic(SCHEDULES_FILE, cfg, 0o644)


def sync_autosnap_timer():
    """Enable the timer iff at least one enabled schedule needs it; disable
    otherwise. This is the master on/off — driven only by the user's schedules."""
    cfg = load_schedules()
    active = any(s.get('enabled') and any(int(s.get('keep', {}).get(fr, 0)) > 0 for fr in FREQS)
                 for s in cfg.get('schedules', []))
    if active:
        run(['systemctl', 'enable', '--now', AUTOSNAP_TIMER])
    else:
        run(['systemctl', 'disable', '--now', AUTOSNAP_TIMER])
    return active


def autosnap_prune(dataset, freq, keep, recursive):
    """Destroy autosnap_<freq>_* snapshots of this dataset beyond the keep count.
    Only ever removes snapshots created by this feature."""
    out, _, _ = run(['zfs', 'list', '-H', '-d', '1', '-t', 'snapshot', '-o', 'name', '-s', 'creation', dataset])
    prefix = f'@autosnap_{freq}_'
    matching = [l for l in out.strip().split('\n') if prefix in l]
    to_delete = matching[:-keep] if keep > 0 and len(matching) > keep else []
    pruned = 0
    for snap in to_delete:
        cmd = ['zfs', 'destroy'] + (['-r'] if recursive else []) + [snap]
        if run_safe(cmd)['success']:
            pruned += 1
    return pruned


def autosnap_one(sched, freq):
    dataset = sched['dataset']
    keep = int(sched.get('keep', {}).get(freq, 0))
    stamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    name = f'{dataset}@autosnap_{freq}_{stamp}'
    cmd = ['zfs', 'snapshot'] + (['-r'] if sched.get('recursive') else []) + [name]
    r = run_safe(cmd)
    pruned = autosnap_prune(dataset, freq, keep, sched.get('recursive')) if r['success'] else 0
    return {'freq': freq, 'snapshot': name, 'ok': r['success'], 'error': r['stderr'][:160], 'pruned': pruned}


@bp.route('/api/snapshots/schedules')
def snap_schedules_list():
    active = (run(['systemctl', 'is-active', AUTOSNAP_TIMER])[0] or '').strip() == 'active'
    return jsonify({'schedules': load_schedules().get('schedules', []), 'timer_active': active})


@bp.route('/api/snapshots/schedules', methods=['POST'])
def snap_schedule_save():
    data = request.get_json() or {}
    dataset = (data.get('dataset') or '').strip()
    if not RE_DATASET.match(dataset):
        return err('Invalid dataset/pool')
    keep = {}
    for fr in FREQS:
        try:
            keep[fr] = max(0, min(10000, int(data.get('keep', {}).get(fr, 0))))
        except (TypeError, ValueError):
            keep[fr] = 0
    sched = {'dataset': dataset, 'recursive': bool(data.get('recursive')),
             'enabled': bool(data.get('enabled', True)), 'keep': keep, 'last_run': {}}
    cfg = load_schedules()
    prev = next((s for s in cfg['schedules'] if s.get('dataset') == dataset), None)
    if prev:
        sched['last_run'] = prev.get('last_run', {})  # preserve run history on edit
    cfg['schedules'] = [s for s in cfg['schedules'] if s.get('dataset') != dataset]
    cfg['schedules'].append(sched)
    save_schedules(cfg)
    sync_autosnap_timer()
    return jsonify({'success': True})


@bp.route('/api/snapshots/schedules/<path:dataset>', methods=['DELETE'])
def snap_schedule_delete(dataset):
    if not RE_DATASET.match(dataset):
        return err('Invalid dataset')
    cfg = load_schedules()
    cfg['schedules'] = [s for s in cfg['schedules'] if s.get('dataset') != dataset]
    save_schedules(cfg)
    sync_autosnap_timer()
    # Existing autosnap snapshots are intentionally left in place on delete.
    return jsonify({'success': True})


@bp.route('/api/snapshots/schedules/<path:dataset>/run', methods=['POST'])
def snap_schedule_run(dataset):
    if not RE_DATASET.match(dataset):
        return err('Invalid dataset')
    cfg = load_schedules()
    sched = next((s for s in cfg['schedules'] if s.get('dataset') == dataset), None)
    if not sched:
        return err('No such schedule', 404)
    results = [autosnap_one(sched, fr) for fr in FREQS if int(sched.get('keep', {}).get(fr, 0)) > 0]
    now = datetime.now().isoformat()
    sched.setdefault('last_run', {})
    for r in results:
        sched['last_run'][r['freq']] = now
    save_schedules(cfg)
    return jsonify({'success': all(r['ok'] for r in results), 'results': results})


def cli_autosnap_tick():
    """Invoked by the systemd timer. Snapshots+prunes each enabled schedule's
    frequencies that are due (based on last_run)."""
    cfg = load_schedules()
    now = datetime.now()
    changed = False
    for s in cfg.get('schedules', []):
        if not s.get('enabled'):
            continue
        lr = s.setdefault('last_run', {})
        for fr in FREQS:
            if int(s.get('keep', {}).get(fr, 0)) <= 0:
                continue
            last = lr.get(fr)
            due = True
            if last:
                try:
                    due = (now - datetime.fromisoformat(last)) >= FREQ_INTERVAL[fr]
                except ValueError:
                    due = True
            if due:
                autosnap_one(s, fr)
                lr[fr] = now.isoformat()
                changed = True
    if changed:
        save_schedules(cfg)
    return 0


# ─── LVM (PV / VG / LV) management ───────────────────────────────────
# Safety: destructive ops are refused on anything backing a mounted filesystem
# (protects the boot/root LVM). New PVs are only created on free devices.



# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'schedules', 'label': 'Auto-Snapshots', 'category': 'Storage MGMT',
          'blueprint': bp,
          'cli': {'autosnap-tick': cli_autosnap_tick}}
