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

bp = Blueprint('gpu', __name__)

# Read-only telemetry from nvidia-smi (NVIDIA) or rocm-smi (AMD/ROCm). Both are
# cheap; a short cache still keeps a busy dashboard from polling every refresh.
# No sudo needed (query tools work unprivileged); no config, no state.
_gpu_cache = {'ts': 0.0, 'data': None}


# systemd services don't read /etc/profile.d, so tools that only add
# themselves to login-shell PATH (TheRock ROCm exports /opt/rocm/bin that
# way — amd-halo) are invisible to shutil.which here. Known install dirs
# are checked as a fallback.
_GPU_TOOL_DIRS = ('/opt/rocm/bin',)


def _gpu_tool(name):
    """Absolute path of a GPU query tool: PATH first, then known dirs."""
    p = shutil.which(name)
    if p:
        return p
    for d in _GPU_TOOL_DIRS:
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _gpu_vendor():
    """Which GPU query tool is installed (nvidia wins if somehow both), or None."""
    if _gpu_tool('nvidia-smi'):
        return 'nvidia'
    if _gpu_tool('rocm-smi'):
        return 'amd'
    return None


def _gpu_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_nvidia_smi(csv_text):
    """Parse `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`.
    Columns: index,name,util,mem_used(MiB),mem_total(MiB),temp(C),power(W)."""
    gpus = []
    for line in (csv_text or '').strip().splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 7:
            continue
        used = _gpu_float(parts[3])
        total = _gpu_float(parts[4])
        mem_pct = round(used / total * 100, 1) if (used is not None and total) else None
        gpus.append({
            'index': _num(parts[0]),
            'name': parts[1] or 'GPU',
            'vendor': 'nvidia',
            'util': _gpu_float(parts[2]),
            'mem_used': int(used * 1024 * 1024) if used is not None else None,
            'mem_total': int(total * 1024 * 1024) if total is not None else None,
            'mem_pct': mem_pct,
            'temp': _gpu_float(parts[5]),
            'power': _gpu_float(parts[6]),
        })
    return gpus


def _parse_rocm_smi(json_text):
    """Parse `rocm-smi ... --json` ({"card0": {..metrics..}, ...})."""
    try:
        data = json.loads(json_text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    gpus = []
    for card in sorted(data):
        d = data[card]
        if not isinstance(d, dict):
            continue
        m = re.search(r'(\d+)', card)
        used = _gpu_float(d.get('VRAM Total Used Memory (B)'))
        total = _gpu_float(d.get('VRAM Total Memory (B)'))
        mem_pct = _gpu_float(d.get('GPU Memory Allocated (VRAM%)'))
        if mem_pct is None and used is not None and total:
            mem_pct = round(used / total * 100, 1)
        name = d.get('Card Series')
        if not name or name == 'N/A':
            name = d.get('Card SKU')
        if not name or name == 'N/A':
            name = d.get('Card Model')
        gfx = d.get('GFX Version')
        if not name or name == 'N/A':
            name = gfx or 'AMD GPU'
        elif gfx and gfx != 'N/A':
            name = '%s (%s)' % (name, gfx)
        gpus.append({
            'index': int(m.group(1)) if m else None,
            'name': name,
            'vendor': 'amd',
            'util': _gpu_float(d.get('GPU use (%)')),
            'mem_used': int(used) if used is not None else None,
            'mem_total': int(total) if total is not None else None,
            'mem_pct': mem_pct,
            'temp': _gpu_float(d.get('Temperature (Sensor junction) (C)')
                               or d.get('Temperature (Sensor edge) (C)')),
            'power': _gpu_float(d.get('Average Graphics Package Power (W)')
                               or d.get('Current Socket Graphics Package Power (W)')),
        })
    return gpus


def _gpu_snapshot(force=False):
    """Current GPU telemetry: {available, vendor, gpus:[...]}. Cached ~8s."""
    now = time.time()
    if not force and _gpu_cache['ts'] and now - _gpu_cache['ts'] < 8:
        return _gpu_cache['data']
    vendor = _gpu_vendor()
    gpus = []
    try:
        if vendor == 'nvidia':
            out, _, _ = run([_gpu_tool('nvidia-smi'),
                             '--query-gpu=index,name,utilization.gpu,memory.used,'
                             'memory.total,temperature.gpu,power.draw',
                             '--format=csv,noheader,nounits'], no_sudo=True)
            gpus = _parse_nvidia_smi(out)
        elif vendor == 'amd':
            out, _, _ = run([_gpu_tool('rocm-smi'), '--showproductname', '--showuse',
                             '--showmemuse', '--showtemp', '--showpower',
                             '--showmeminfo', 'vram', '--json'], no_sudo=True)
            gpus = _parse_rocm_smi(out)
    except Exception:
        gpus = []
    data = {'available': bool(gpus), 'vendor': vendor, 'gpus': gpus}
    _gpu_cache['ts'], _gpu_cache['data'] = now, data
    return data


@bp.route('/api/gpu')
def gpu_get():
    return jsonify(_gpu_snapshot())


# ─── Prometheus metrics ───────────────────────────────────────────────
# Public endpoint (a scraper can't use the session cookie). If
# DASHBOARD_METRICS_TOKEN is set it is required (?token= or Bearer); otherwise
# open, as is conventional for node_exporter-style endpoints on a trusted LAN.


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'gpu', 'label': 'GPU', 'category': 'AI Tools',
          'blueprint': bp}
