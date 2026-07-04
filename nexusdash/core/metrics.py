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
from .summary import _system_resources
from ..modules.disks import _smart_health_ok
from ..modules.nfs import parse_exports
from ..modules.smb import smbconf_parse

bp = Blueprint('metrics', __name__)

METRICS_TOKEN = os.environ.get('DASHBOARD_METRICS_TOKEN', '')


def _prom_escape(v):
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _prom_num(v):
    return ('%g' % v) if isinstance(v, float) else str(int(v))


def _render_metrics(families):
    """families: list of (name, help, type, [(labels_str, value), ...]) -> text."""
    out = []
    for name, htext, mtype, samples in families:
        if not samples:
            continue
        out.append('# HELP %s %s' % (name, htext))
        out.append('# TYPE %s %s' % (name, mtype))
        for labels, value in samples:
            out.append('%s%s %s' % (name, labels, _prom_num(value)))
    return '\n'.join(out) + '\n'


def _metrics_families():
    res = _system_resources()
    mem, sw = res['memory'], res['swap']
    fams = [
        ('storagedash_up', 'Dashboard is up', 'gauge', [('', 1)]),
        ('storagedash_uptime_seconds', 'Host uptime in seconds', 'gauge', [('', res['uptime_seconds'])]),
        ('storagedash_load1', '1-minute load average', 'gauge', [('', res['load']['1'])]),
        ('storagedash_load5', '5-minute load average', 'gauge', [('', res['load']['5'])]),
        ('storagedash_load15', '15-minute load average', 'gauge', [('', res['load']['15'])]),
        ('storagedash_cpu_count', 'Logical CPU count', 'gauge', [('', res['cpus'])]),
        ('storagedash_cpu_usage_percent', 'CPU busy percent', 'gauge', [('', res['cpu_pct'])]),
        ('storagedash_memory_total_bytes', 'Total RAM', 'gauge', [('', mem['total'])]),
        ('storagedash_memory_available_bytes', 'Available RAM', 'gauge', [('', mem['available'])]),
        ('storagedash_memory_used_bytes', 'Used RAM', 'gauge', [('', mem['used'])]),
        ('storagedash_swap_total_bytes', 'Total swap', 'gauge', [('', sw['total'])]),
        ('storagedash_swap_used_bytes', 'Used swap', 'gauge', [('', sw['used'])]),
    ]

    # ZFS pools (cheap: one zpool list -Hp).
    size_s, alloc_s, free_s, health_s = [], [], [], []
    zout, _, zrc = run(['zpool', 'list', '-Hp', '-o', 'name,size,alloc,free,health'])
    if zrc == 0:
        for line in zout.strip().split('\n'):
            p = line.split('\t')
            if len(p) >= 5 and p[0]:
                lbl = '{pool="%s"}' % _prom_escape(p[0])
                size_s.append((lbl, int(p[1])))
                alloc_s.append((lbl, int(p[2])))
                free_s.append((lbl, int(p[3])))
                health_s.append((lbl, 1 if p[4] == 'ONLINE' else 0))
    fams += [
        ('storagedash_zfs_pool_size_bytes', 'ZFS pool total size', 'gauge', size_s),
        ('storagedash_zfs_pool_alloc_bytes', 'ZFS pool allocated', 'gauge', alloc_s),
        ('storagedash_zfs_pool_free_bytes', 'ZFS pool free', 'gauge', free_s),
        ('storagedash_zfs_pool_healthy', 'ZFS pool ONLINE (1) or not (0)', 'gauge', health_s),
    ]

    # Service up/down (cheap systemctl is-active per service).
    svc_s = []
    for key, svc in SYSTEM_SERVICES.items():
        active = (run(['systemctl', 'is-active', svc['service']])[0] or '').strip()
        svc_s.append(('{service="%s"}' % _prom_escape(key), 1 if active == 'active' else 0))
    fams.append(('storagedash_service_up', 'systemd service active (1) or not (0)', 'gauge', svc_s))

    # Share/export counts (file reads, cheap) and SMART (cached).
    fams.append(('storagedash_nfs_exports', 'Configured NFS exports', 'gauge',
                 [('', len(parse_exports()))]))
    fams.append(('storagedash_smb_shares', 'Configured SMB shares', 'gauge',
                 [('', len([n for n in smbconf_parse() if n.lower() not in ('global', 'homes')]))]))
    smart_ok = _smart_health_ok()
    if smart_ok is not None:
        fams.append(('storagedash_disk_smart_ok', 'All disks pass SMART (1) or a failure (0)',
                     'gauge', [('', 1 if smart_ok else 0)]))
    return fams


@bp.route('/metrics')
def metrics():
    if METRICS_TOKEN:
        tok = request.args.get('token', '')
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            tok = auth[7:]
        if tok != METRICS_TOKEN:
            return Response('unauthorized\n', status=401, mimetype='text/plain')
    return Response(_render_metrics(_metrics_families()),
                    mimetype='text/plain; version=0.0.4; charset=utf-8')


