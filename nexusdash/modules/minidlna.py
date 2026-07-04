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
from .smb import _yn

bp = Blueprint('minidlna', __name__)

MINIDLNA_CONF = os.environ.get('DASHBOARD_MINIDLNA_CONF', '/etc/minidlna.conf')
# The db/cache dir the rebuild wrapper operates on (holds files.db + art_cache).
# Distro default on both Debian and EPEL/Rocky; the wrapper hard-codes the same
# constant (see install.sh) — keep them in sync if a family ever differs.
MINIDLNA_CACHE = '/var/cache/minidlna'
DLNA_RESCAN_HELPER = HELPER_PREFIX + '-dlna-rescan'
# Read-only helper: reads media-library counts out of minidlna's SQLite files.db.
# Root-owned because the db/cache dir is minidlna-only (mode 0750) on some distros,
# so the dashboard user can't read it directly. The helper opens the db read-only
# and runs fixed COUNT queries — no writes, no arbitrary SQL.
DLNA_STATS_HELPER = HELPER_PREFIX + '-dlna-stats'

# Managed scalar keys surfaced in the UI. Every OTHER key present in the file
# (log_dir, db_dir, album_art_names, serial, model_number, …) is preserved
# verbatim on rewrite, so saving the form never clobbers unmanaged settings.
MINIDLNA_MANAGED = ('friendly_name', 'port', 'network_interface', 'inotify',
                    'root_container')
MINIDLNA_MEDIA_TYPES = {'A', 'V', 'P', ''}   # audio / video / picture / all
RE_DLNA_NAME = re.compile(r'^[^\n\r]{0,64}$')             # friendly_name: no newline
RE_DLNA_IFACES = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._, -]*$')  # one or more iface names
RE_DLNA_CONTAINER = re.compile(r'^[A-Za-z0-9.,$_/-]{1,64}$')    # root_container tokens


def _split_media_dir(v):
    """Split a media_dir value into (type_prefix, path). "V,/srv/video" →
    ('V', '/srv/video'); "/srv/media" → ('', '/srv/media'). A leading token is
    only treated as a type prefix when it is a combination of A/V/P letters."""
    if ',' in v:
        pre, path = v.split(',', 1)
        pre = pre.strip().upper()
        if pre and all(c in 'AVP' for c in pre):
            return pre, path.strip()
    return '', v.strip()


def minidlna_parse():
    """Round-trip parse of /etc/minidlna.conf into an ordered list of (key, value)
    pairs (media_dir may repeat). The file is world-readable, so a plain read (no
    sudo); only writes need root. Comments/blank lines are dropped on rewrite."""
    pairs = []
    try:
        with open(MINIDLNA_CONF) as f:
            for line in f:
                s = line.strip()
                if not s or s[0] == '#' or '=' not in s:
                    continue
                k, v = s.split('=', 1)
                pairs.append((k.strip().lower(), v.strip()))
    except FileNotFoundError:
        pass
    return pairs


def _minidlna_view(pairs):
    """Shape the raw pairs into the API/UI view (scalars + media_dir list)."""
    view = {k: '' for k in MINIDLNA_MANAGED}
    view['inotify'] = 'yes'          # minidlna's own default
    view['media_dirs'] = []
    view['db_dir'] = MINIDLNA_CACHE
    for k, v in pairs:
        if k == 'media_dir':
            t, p = _split_media_dir(v)
            view['media_dirs'].append({'type': t, 'path': p})
        elif k == 'db_dir':
            view['db_dir'] = v
        elif k in MINIDLNA_MANAGED:
            view[k] = v
    return view


def minidlna_render(pairs):
    return '\n'.join(f'{k}={v}' for k, v in pairs) + '\n'


def _minidlna_build(data):
    """Validate the posted settings and merge them into the existing file,
    preserving order and every unmanaged key. Returns (pairs, error_or_None)."""
    scalars = {}

    name = (data.get('friendly_name') or '').strip()
    if name:
        if not RE_DLNA_NAME.match(name):
            return None, 'Invalid friendly name'
        scalars['friendly_name'] = name

    port = str(data.get('port') or '').strip()
    if port:
        if not RE_NUM.match(port) or not (1 <= int(port) <= 65535):
            return None, 'Port must be between 1 and 65535'
        scalars['port'] = port

    iface = (data.get('network_interface') or '').strip()
    if iface:
        if not RE_DLNA_IFACES.match(iface):
            return None, 'Invalid network interface'
        scalars['network_interface'] = iface

    container = (data.get('root_container') or '').strip()
    if container:
        if not RE_DLNA_CONTAINER.match(container):
            return None, 'Invalid root container'
        scalars['root_container'] = container

    # inotify is always written (a plain yes/no boolean).
    scalars['inotify'] = _yn(data.get('inotify'), 'yes')

    media_lines = []
    for entry in (data.get('media_dirs') or []):
        if isinstance(entry, str):
            entry = {'path': entry}
        path = (entry.get('path') or '').strip()
        mtype = (entry.get('type') or '').strip().upper()
        if not path:
            continue
        if not RE_PATH.match(path):
            return None, f'Invalid media directory: {path}'
        if mtype not in MINIDLNA_MEDIA_TYPES:
            return None, f'Invalid media type prefix: {mtype}'
        if not os.path.isdir(path):
            return None, f'Media directory does not exist: {path}'
        media_lines.append(f'{mtype},{path}' if mtype else path)
    if not media_lines:
        return None, 'At least one media directory is required'

    existing = minidlna_parse()
    managed = set(MINIDLNA_MANAGED)
    out, placed, media_emitted = [], set(), False
    for k, v in existing:
        if k == 'media_dir':
            if not media_emitted:
                out.extend(('media_dir', ml) for ml in media_lines)
                media_emitted = True
            continue                       # drop the old media_dir lines
        if k in managed:
            if k in scalars and k not in placed:
                out.append((k, scalars[k]))
                placed.add(k)
            continue                       # replaced above, or cleared → drop
        out.append((k, v))                 # preserve every unmanaged key
    for k in MINIDLNA_MANAGED:
        if k in scalars and k not in placed:
            out.append((k, scalars[k]))
    if not media_emitted:
        out.extend(('media_dir', ml) for ml in media_lines)
    return out, None


def minidlna_apply(data):
    """Validate + write /etc/minidlna.conf (pinned tee grant) + reload the daemon."""
    pairs, error = _minidlna_build(data)
    if error:
        return {'success': False, 'error': error}
    r = run_safe(['tee', MINIDLNA_CONF], input_data=minidlna_render(pairs))
    if not r['success']:
        return r
    return run_safe(['systemctl', 'reload-or-restart',
                     SYSTEM_SERVICES['minidlna']['service']])


def _minidlna_configured():
    return _unit_present('minidlna') or os.path.exists(MINIDLNA_CONF)


def _minidlna_db_stats():
    """Media-library counts from minidlna's SQLite files.db, via the root-owned
    read helper. Returns a dict ({available, path, size, objects, audio, video,
    image}) or None if the helper is missing / the db is unreadable. Best-effort —
    never raises into a request."""
    try:
        out, _e, rc = run([DLNA_STATS_HELPER])
        if rc != 0:
            return None
        data = json.loads(out)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


@bp.route('/api/minidlna')
def minidlna_get():
    view = _minidlna_view(minidlna_parse())
    unit = SYSTEM_SERVICES['minidlna']['service']
    active = (run(['systemctl', 'is-active', unit])[0] or '').strip() or 'inactive'
    enabled = (run(['systemctl', 'is-enabled', unit])[0] or '').strip() or 'disabled'
    return jsonify({
        'configured': _minidlna_configured(),
        'service': {'active': active, 'enabled': enabled},
        'conf_path': MINIDLNA_CONF,
        'cache_dir': MINIDLNA_CACHE,
        'library': _minidlna_db_stats(),
        **view,
    })


@bp.route('/api/minidlna/stats')
def minidlna_stats():
    """Just the media-library counts — a lightweight endpoint the Dashboard card
    polls without parsing the config or hitting systemctl."""
    return jsonify({'library': _minidlna_db_stats()})


@bp.route('/api/minidlna', methods=['POST'])
def minidlna_set():
    result = minidlna_apply(request.get_json() or {})
    if not result.get('success'):
        return err(result.get('error') or result.get('stderr') or 'Failed to apply config', 500)
    return jsonify({'success': True})


@bp.route('/api/minidlna/rescan', methods=['POST'])
def minidlna_rescan():
    """Incremental rescan: restart the service so it re-reads the config and picks
    up new files (inotify normally handles new files while running)."""
    r = run_safe(['systemctl', 'restart', SYSTEM_SERVICES['minidlna']['service']])
    if not r['success']:
        return err(r.get('stderr') or 'Restart failed', 500)
    return jsonify({'success': True})


@bp.route('/api/minidlna/rebuild', methods=['POST'])
def minidlna_rebuild():
    """Force a full database rebuild via the root-owned wrapper: stop the service,
    delete files.db (confined to the cache dir), `minidlnad -R`, start. Discards
    the media index and re-scans from scratch."""
    out, e, rc = run([DLNA_RESCAN_HELPER])
    if rc != 0:
        return err((e or out or 'Rebuild failed').strip()[-300:], 500)
    return jsonify({'success': True})


# ─── Installation Check ───────────────────────────────────────────────

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


# ─── TLS certificate management ───────────────────────────────────────



# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'minidlna', 'label': 'DLNA Media', 'category': 'Sharing',
          'blueprint': bp}
