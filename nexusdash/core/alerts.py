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
from .summary import _compute_alerts
from .tasks import _task_alerts
from ..modules.lvm import _lvm_report
from ..modules.disks import _mdadm_conf_arrays
from ..modules.replication import RE_HOSTNAME

bp = Blueprint('alerts', __name__)

NOTIFICATIONS_FILE = os.environ.get('DASHBOARD_NOTIFICATIONS_FILE',
                                    os.path.join(APP_DIR, 'notifications.json'))
ALERTS_TIMER = UNIT_PREFIX + '-alerts.timer'
PW_MASK = '********'
RE_EMAIL = re.compile(r'^[^@\s,]+@[^@\s,]+\.[^@\s,]+$')


def load_notifications():
    try:
        with open(NOTIFICATIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'email': {}, 'webhook': {}, 'state': {}}


def save_notifications(cfg):
    write_json_atomic(NOTIFICATIONS_FILE, cfg, 0o600)


def _notifications_enabled(cfg):
    return bool(cfg.get('email', {}).get('enabled') or cfg.get('webhook', {}).get('enabled'))


def _send_email(ec, subject, body):
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = ec.get('from') or ec.get('username') or 'storage-dashboard'
    msg['To'] = ec.get('to', '')
    msg.set_content(body)
    host, port = ec.get('host', ''), int(ec.get('port') or 587)
    sec = ec.get('security', 'starttls')
    if sec == 'ssl':
        s = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        s = smtplib.SMTP(host, port, timeout=15)
        if sec == 'starttls':
            s.starttls()
    try:
        if ec.get('username'):
            s.login(ec['username'], ec.get('password', ''))
        s.send_message(msg)
    finally:
        s.quit()


def _send_webhook(url, payload):
    import urllib.request
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'storage-dashboard'})
    urllib.request.urlopen(req, timeout=15).read()


def _notify(cfg, kind, message):
    """Deliver one notification to every enabled channel. Returns list of
    (channel, ok, error)."""
    host = socket.gethostname()
    subject = '[%s] Storage %s: %s' % (host, kind, message[:90])
    body = '%s: %s\n\nHost: %s\nTime: %s\n' % (
        kind, message, socket.getfqdn(), datetime.now().astimezone().isoformat(timespec='seconds'))
    results = []
    ec = cfg.get('email', {})
    if ec.get('enabled'):
        try:
            _send_email(ec, subject, body)
            results.append(('email', True, ''))
        except Exception as e:
            results.append(('email', False, str(e)[:200]))
    wc = cfg.get('webhook', {})
    if wc.get('enabled') and wc.get('url'):
        try:
            # Send only {"text": ...} — Google Chat and Slack both render it, and
            # Google Chat rejects payloads with any unknown fields (400).
            _send_webhook(wc['url'], {'text': '[%s] %s: %s' % (host, kind, message)})
            results.append(('webhook', True, ''))
        except Exception as e:
            results.append(('webhook', False, str(e)[:200]))
    return results


def sync_alerts_timer():
    action = 'enable' if _notifications_enabled(load_notifications()) else 'disable'
    run(['systemctl', '--now', action, ALERTS_TIMER])


def cli_alerts_tick():
    """Invoked by the timer: notify on new/cleared alerts, then persist state."""
    cfg = load_notifications()
    current = {a['key']: a['message'] for a in _compute_alerts()}
    state = cfg.get('state', {})
    if _notifications_enabled(cfg):
        for k, msg in current.items():
            if k not in state:
                _notify(cfg, 'ALERT', msg)
        for k, msg in state.items():
            if k not in current:
                _notify(cfg, 'RESOLVED', msg)
    cfg['state'] = current  # always refresh so enable/disable stays clean
    save_notifications(cfg)
    # Explicit exit code: app.py treats a None from dispatch() as "no command
    # matched" and falls through to starting the SERVER — a missing return here
    # had every alerts-tick since the 2.0.0 cutover die on the bound port.
    return 0


def _validate_notifications(data):
    """Return (clean_config_fragment, error). Does not touch state/password merge."""
    email = data.get('email', {}) or {}
    web = data.get('webhook', {}) or {}
    if email.get('enabled'):
        if not (RE_HOSTNAME.match(email.get('host', '')) or RE_IP.match(email.get('host', ''))):
            return None, 'Invalid SMTP host'
        try:
            if not (1 <= int(email.get('port') or 0) <= 65535):
                return None, 'Invalid SMTP port'
        except (TypeError, ValueError):
            return None, 'Invalid SMTP port'
        if email.get('security', 'starttls') not in ('none', 'starttls', 'ssl'):
            return None, 'Invalid SMTP security'
        if not RE_EMAIL.match(email.get('to', '')):
            return None, 'Invalid recipient address'
        if email.get('from') and not RE_EMAIL.match(email['from']):
            return None, 'Invalid sender address'
    if web.get('enabled') and not re.match(r'^https?://', web.get('url', '')):
        return None, 'Webhook URL must start with http:// or https://'
    return {'email': email, 'webhook': web}, None


@bp.route('/api/notifications')
def notifications_get():
    cfg = load_notifications()
    email = dict(cfg.get('email', {}))
    if email.get('password'):
        email['password'] = PW_MASK   # never expose the stored password
    return jsonify({'email': email, 'webhook': cfg.get('webhook', {}),
                    'active_alerts': cfg.get('state', {}),
                    'timer_active': (run(['systemctl', 'is-active', ALERTS_TIMER])[0] or '').strip() == 'active'})


@bp.route('/api/notifications', methods=['POST'])
def notifications_save():
    data = request.get_json() or {}
    clean, errmsg = _validate_notifications(data)
    if errmsg:
        return err(errmsg)
    cfg = load_notifications()
    # Preserve the stored SMTP password when the client sends the mask or blank.
    newpw = clean['email'].get('password', '')
    if newpw in (PW_MASK, ''):
        clean['email']['password'] = cfg.get('email', {}).get('password', '')
    cfg['email'] = clean['email']
    cfg['webhook'] = clean['webhook']
    save_notifications(cfg)
    sync_alerts_timer()
    return jsonify({'success': True})


@bp.route('/api/notifications/test', methods=['POST'])
def notifications_test():
    cfg = load_notifications()
    if not _notifications_enabled(cfg):
        return err('Enable and save email and/or webhook first')
    results = _notify(cfg, 'TEST', 'This is a test notification from the storage dashboard.')
    ok = all(r[1] for r in results) and bool(results)
    return jsonify({'success': ok,
                    'results': [{'channel': c, 'ok': o, 'error': e} for c, o, e in results]})


# ─── Scheduled maintenance (scrubs + SMART self-tests) ────────────────
# Opt-in, same timer pattern as auto-snapshots: a tick runs due scrubs and SMART
# self-tests. Uses already-granted binaries (zpool, smartctl) — no new sudoers.
