"""Append-only audit log — one after_request choke point covers every route.

Extracted verbatim from the single-file dashboard. Every state-changing request
(and all login attempts, including denied 401/403/429) is recorded; GETs are
reads and intentionally not logged. Do NOT add per-route audit calls.
"""
import os
import json
import threading
from datetime import datetime
from flask import Blueprint, jsonify, request, session, g

from .config import APP_DIR
from .runcmd import err
from .auth import _is_admin

bp = Blueprint('audit', __name__)

AUDIT_FILE = os.environ.get('DASHBOARD_AUDIT_FILE', os.path.join(APP_DIR, 'audit.log'))
AUDIT_METHODS = {'POST', 'PUT', 'DELETE', 'PATCH'}
_audit_lock = threading.Lock()


def audit(user, ip, method, path, target, status):
    """Append one JSON line to the audit log. Best-effort: auditing must never
    break a request, so all errors are swallowed."""
    try:
        entry = {
            'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
            'user': user or '-',
            'ip': ip or '-',
            'method': method,
            'path': path,
            'target': target or {},
            'status': status,
            'result': 'ok' if 200 <= status < 300 else
                      ('denied' if status in (401, 403, 429) else 'error'),
        }
        line = json.dumps(entry, separators=(',', ':'), default=str)
        with _audit_lock:
            fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as f:
                f.write(line + '\n')
    except Exception:
        pass


def _audit_request(response):
    """Single choke point (registered app-wide by create_app): record every
    state-changing request regardless of which endpoint handled it. Runs even
    when require_login short-circuits, so denied (401/403/429) attempts are
    logged too."""
    try:
        if request.method in AUDIT_METHODS and request.endpoint != 'static':
            # Prefer the resolved identity (session user or 'token:<name>'); on a
            # failed login fall back to the attempted username stashed by api_login.
            user = (session.get('user') or getattr(g, 'identity_name', None)
                    or getattr(g, 'audit_user', None))
            audit(user, request.remote_addr, request.method,
                  request.path, request.view_args, response.status_code)
    except Exception:
        pass
    return response


@bp.route('/api/audit')
def audit_list():
    """Recent audit entries (admin only), newest first."""
    if not _is_admin():
        return err('Administrator access required', 403)
    try:
        limit = max(1, min(int(request.args.get('limit', 200)), 2000))
    except (TypeError, ValueError):
        limit = 200
    entries = []
    try:
        with open(AUDIT_FILE) as f:
            for line in f.readlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    entries.reverse()
    return jsonify({'entries': entries, 'count': len(entries)})
