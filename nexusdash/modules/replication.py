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

bp = Blueprint('replication', __name__)

REPLICATION_FILE = os.environ.get('DASHBOARD_REPLICATION_FILE', os.path.join(APP_DIR, 'replication.json'))
REPL_KEY = os.environ.get('DASHBOARD_REPL_KEY', os.path.join(APP_DIR, 'replication_key'))
REPL_KNOWN_HOSTS = os.path.join(APP_DIR, 'replication_known_hosts')
REPL_TIMER = UNIT_PREFIX + '-replicate.timer'
RE_HOSTNAME = re.compile(r'^[a-zA-Z0-9_.-]+$')


def load_replication():
    try:
        with open(REPLICATION_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'jobs': []}


def save_replication(cfg):
    write_json_atomic(REPLICATION_FILE, cfg, 0o600)


def ensure_repl_key():
    """Generate the dedicated replication keypair on first use. Returns the
    public key text (to install on the remote), or '' on failure."""
    if not os.path.exists(REPL_KEY):
        run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-q', '-f', REPL_KEY,
             '-C', 'storage-dashboard-replication'], no_sudo=True)
    try:
        with open(REPL_KEY + '.pub') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ''


def _ssh_base(host, user, port):
    return ['ssh', '-i', REPL_KEY, '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'UserKnownHostsFile=' + REPL_KNOWN_HOSTS,
            '-o', 'ConnectTimeout=10', '-p', str(port),
            '%s@%s' % (user, host)]


def _valid_endpoint(host, user, port):
    if not (RE_HOSTNAME.match(host or '') or RE_IP.match(host or '')):
        return 'Invalid host'
    if not RE_USERNAME.match(user or ''):
        return 'Invalid remote user'
    try:
        p = int(port)
        if not (1 <= p <= 65535):
            return 'Invalid port'
    except (TypeError, ValueError):
        return 'Invalid port'
    return None


def _local_snaps(dataset):
    out, _, _ = run(['zfs', 'list', '-H', '-o', 'name', '-t', 'snapshot',
                     '-s', 'creation', '-d', '1', dataset])
    return [l.split('@', 1)[1] for l in out.split('\n') if '@' in l]


def _remote_snaps(job):
    """Snapshot short-names of the target on the remote, or None if the target
    dataset does not exist there yet (i.e. an initial replication is needed)."""
    cmd = _ssh_base(job['host'], job['user'], job.get('port', 22)) + \
        ['sudo', '-n', 'zfs', 'list', '-H', '-o', 'name', '-t', 'snapshot', '-d', '1', job['target']]
    out, _, rc = run(cmd, no_sudo=True)
    if rc != 0:
        return None
    return [l.split('@', 1)[1] for l in out.split('\n') if '@' in l]


def _pipe_send_recv(send_cmd, recv_cmd):
    """Run `send_cmd | recv_cmd` connecting pipes in Python (no shell). Returns
    (ok, error_text)."""
    try:
        sp = subprocess.Popen(send_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rp = subprocess.Popen(recv_cmd, stdin=sp.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sp.stdout.close()  # let `send` get SIGPIPE if `recv` dies
        _, rerr = rp.communicate()
        sp.wait()
        serr = sp.stderr.read()
        sp.stderr.close()
    except OSError as e:
        return False, str(e)
    if sp.returncode == 0 and rp.returncode == 0:
        return True, ''
    msg = (serr.decode(errors='replace') + ' ' + rerr.decode(errors='replace')).strip()
    return False, msg[:300] or 'replication failed'


def replicate_job(job):
    """Replicate one job's source dataset to its remote target. Returns a result
    dict with ok/message/error and the snapshot transferred."""
    source, target = job['source'], job['target']
    local = _local_snaps(source)
    if not local:
        return {'ok': False, 'error': 'No snapshots on %s — create or schedule one first' % source}
    latest = local[-1]
    remote = _remote_snaps(job)
    send = ['sudo', '-n', 'zfs', 'send']
    if remote is None:
        # Initial replication: full stream up to the latest snapshot.
        if job.get('recursive'):
            send.append('-R')
        send.append('%s@%s' % (source, latest))
        kind = 'full'
    else:
        common = [s for s in local if s in remote]
        if not common:
            return {'ok': False, 'error': 'Target exists but shares no snapshot with the source. '
                    'Destroy the remote target to re-seed, or pick a fresh target.'}
        base = common[-1]
        if base == latest:
            return {'ok': True, 'nochange': True, 'snapshot': latest,
                    'message': 'Already up to date at @%s' % latest}
        if job.get('recursive'):
            send.append('-R')
        send += ['-I', '%s@%s' % (source, base), '%s@%s' % (source, latest)]
        kind = 'incremental'
    recv = _ssh_base(job['host'], job['user'], job.get('port', 22)) + \
        ['sudo', '-n', 'zfs', 'recv', '-F', target]
    ok, errtxt = _pipe_send_recv(send, recv)
    return {'ok': ok, 'snapshot': latest, 'kind': kind, 'error': errtxt}


def sync_replicate_timer():
    """Enable the replication timer iff at least one enabled job exists."""
    active = any(j.get('enabled') for j in load_replication().get('jobs', []))
    action = 'enable' if active else 'disable'
    run(['systemctl', '--now', action, REPL_TIMER])


@bp.route('/api/zfs/replication')
def replication_list():
    cfg = load_replication()
    active = (run(['systemctl', 'is-active', REPL_TIMER])[0] or '').strip() == 'active'
    return jsonify({'jobs': cfg.get('jobs', []), 'pubkey': ensure_repl_key(),
                    'timer_active': active})


@bp.route('/api/zfs/replication', methods=['POST'])
def replication_save():
    data = request.get_json() or {}
    source = (data.get('source') or '').strip()
    target = (data.get('target') or '').strip()
    host = (data.get('host') or '').strip()
    user = (data.get('user') or '').strip()
    port = data.get('port', 22)
    if not RE_DATASET.match(source):
        return err('Invalid source dataset')
    if not RE_DATASET.match(target):
        return err('Invalid target dataset')
    bad = _valid_endpoint(host, user, port)
    if bad:
        return err(bad)
    job = {
        'id': (data.get('id') or 'repl-%d' % int(time.time() * 1000)),
        'source': source, 'target': target, 'host': host, 'user': user,
        'port': int(port), 'recursive': bool(data.get('recursive')),
        'enabled': bool(data.get('enabled', True)),
    }
    cfg = load_replication()
    prev = next((j for j in cfg['jobs'] if j.get('id') == job['id']), None)
    if prev:  # preserve run history on edit
        for k in ('last_run', 'last_status', 'last_error', 'last_snapshot'):
            if k in prev:
                job[k] = prev[k]
    cfg['jobs'] = [j for j in cfg['jobs'] if j.get('id') != job['id']]
    cfg['jobs'].append(job)
    save_replication(cfg)
    sync_replicate_timer()
    return jsonify({'success': True, 'id': job['id']})


@bp.route('/api/zfs/replication/<job_id>', methods=['DELETE'])
def replication_delete(job_id):
    cfg = load_replication()
    cfg['jobs'] = [j for j in cfg['jobs'] if j.get('id') != job_id]
    save_replication(cfg)
    sync_replicate_timer()
    return jsonify({'success': True})


@bp.route('/api/zfs/replication/test', methods=['POST'])
def replication_test():
    data = request.get_json() or {}
    host = (data.get('host') or '').strip()
    user = (data.get('user') or '').strip()
    port = data.get('port', 22)
    bad = _valid_endpoint(host, user, port)
    if bad:
        return err(bad)
    ensure_repl_key()
    cmd = _ssh_base(host, user, port) + ['sudo', '-n', 'zfs', 'version']
    out, errtxt, rc = run(cmd, no_sudo=True)
    if rc != 0:
        return jsonify({'success': False,
                        'error': (errtxt or 'SSH/zfs check failed').strip()[:300]})
    return jsonify({'success': True, 'remote_zfs': out.strip().split('\n')[0]})


@bp.route('/api/zfs/replication/<job_id>/run', methods=['POST'])
def replication_run(job_id):
    cfg = load_replication()
    job = next((j for j in cfg['jobs'] if j.get('id') == job_id), None)
    if not job:
        return err('No such replication job', 404)
    res = replicate_job(job)
    job['last_run'] = datetime.now().isoformat(timespec='seconds')
    job['last_status'] = 'ok' if res['ok'] else 'error'
    job['last_error'] = '' if res['ok'] else res.get('error', '')
    if res.get('snapshot'):
        job['last_snapshot'] = res['snapshot']
    save_replication(cfg)
    return jsonify({'success': res['ok'], **res})


@bp.route('/api/zfs/replication/key/regenerate', methods=['POST'])
def replication_key_regenerate():
    for p in (REPL_KEY, REPL_KEY + '.pub'):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return jsonify({'success': True, 'pubkey': ensure_repl_key()})


def cli_replicate_tick():
    """Invoked by the systemd timer: run every enabled replication job."""
    cfg = load_replication()
    changed = False
    for job in cfg.get('jobs', []):
        if not job.get('enabled'):
            continue
        res = replicate_job(job)
        job['last_run'] = datetime.now().isoformat(timespec='seconds')
        job['last_status'] = 'ok' if res['ok'] else 'error'
        job['last_error'] = '' if res['ok'] else res.get('error', '')
        if res.get('snapshot'):
            job['last_snapshot'] = res['snapshot']
        changed = True
        print('replicate %s -> %s@%s:%s : %s' % (
            job['source'], job['user'], job['host'], job['target'],
            job['last_status']), flush=True)
    if changed:
        save_replication(cfg)


# ─── Alerting / notifications (email + webhook) ───────────────────────
# A background tick computes the current health alerts (the single source,
# _compute_alerts) and notifies on NEW conditions only (de-duplicated against
# saved state), plus a RESOLVED notice when one clears. Email via smtplib and
# webhook via urllib — both stdlib, no new dependencies or sudo.


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'replication', 'label': 'Replication', 'category': 'Storage MGMT',
          'blueprint': bp,
          'cli': {'replicate-tick': cli_replicate_tick}}
