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


# ─── Tunables (read + set) ────────────────────────────────────────────
# Scope is deliberate: POWER CAP and POWER PROFILE only. Both are supported on
# every AMD card in this fleet, both are instantly reversible, and neither can
# destabilise a running inference job. Clock limits and perf-determinism are
# left out on purpose — they can, and llama-server is normally mid-model.
#
# NOT offered, because the hardware does not have it: memory/compute
# partitioning, soc_pstate, xgmi_plpd, mem_carveout and fan control all report
# N/A on Navi 48 (Radeon AI PRO R9700). Partitioning is an MI300-class
# accelerator feature. Showing a control that is N/A everywhere would be worse
# than not showing it.
#
# Privilege split: READS go through amd-smi, which needs /dev/kfd and so needs
# the service user in the `render` group (rocm-smi reads sysfs unprivileged,
# which is why the telemetry above never needed it). WRITES go through the
# root-owned helper, which re-validates everything against the card's own
# reported limits — the app's validation is a UI convenience, not the boundary.

GPU_TUNE_HELPER = HELPER_PREFIX + '-gpu-tune'
RE_GPU_PROFILE = re.compile(r'^[A-Z0-9_]{1,32}$')
_gpu_tun_cache = {'ts': 0.0, 'data': None}


def _amd_smi_json(args):
    """Run amd-smi with --json and return its per-GPU list, or None.

    amd-smi nests everything under `gpu_data`; older builds returned a bare
    list, so both shapes are accepted."""
    tool = _gpu_tool('amd-smi')
    if not tool:
        return None
    out, _e, rc = run([tool] + list(args) + ['--json'], no_sudo=True)
    if rc != 0 or not out:
        return None
    try:
        d = json.loads(out)
    except (ValueError, TypeError):
        return None
    if isinstance(d, dict):
        d = d.get('gpu_data', d)
    return d if isinstance(d, list) else None


def _amd_val(v):
    """amd-smi reports either {'value': n, 'unit': 'W'} or the string 'N/A'."""
    if isinstance(v, dict):
        return _gpu_float(v.get('value'))
    return None


def _amd_tunables():
    static = _amd_smi_json(['static'])
    if static is None:
        return None
    metric = {}
    for e in (_amd_smi_json(['metric']) or []):
        metric[e.get('gpu')] = e
    out = []
    def _d(v):
        """amd-smi returns an ERROR STRING, not a dict, for a feature the card
        does not implement — e.g. node4's APU answers `profile` with
        "AMDSMI_STATUS_NOT_SUPPORTED - Feature not supported". Every subtree
        here has to survive that."""
        return v if isinstance(v, dict) else {}

    for e in static:
        e = _d(e)
        idx = e.get('gpu')
        lim = _d(e.get('limit'))
        ppt0 = _d(lim.get('ppt0'))
        prof = _d(e.get('profile'))
        m = _d(metric.get(idx))
        power = _d(m.get('power'))
        cur = _amd_val(ppt0.get('socket_power_limit'))
        lo = _amd_val(ppt0.get('min_power_limit'))
        hi = _amd_val(ppt0.get('max_power_limit'))
        avail = prof.get('available_profiles')
        avail = [p for p in avail if RE_GPU_PROFILE.match(str(p))] if isinstance(avail, list) else []
        settable = []
        if cur is not None and lo is not None and hi is not None and hi > lo:
            settable.append('power_cap')
        if avail:
            settable.append('profile')
        asic = _d(e.get('asic'))
        out.append({
            'index': idx,
            'vendor': 'amd',
            'name': asic.get('market_name') or _d(e.get('board')).get('product_name') or 'AMD GPU',
            'power_cap': {'current': cur, 'min': lo, 'max': hi, 'unit': 'W'},
            'power_now': _amd_val(power.get('socket_power')),
            'throttled': power.get('throttle_status') == 'THROTTLED',
            'profile': {'current': prof.get('current'), 'available': avail},
            'slowdown_temp': _amd_val(lim.get('slowdown_hotspot_temperature')),
            'shutdown_temp': _amd_val(lim.get('shutdown_hotspot_temperature')),
            'settable': settable,
        })
    return out


def _nvidia_tunables():
    """nvidia-smi's equivalents. Untested against real hardware — no NVIDIA card
    is in this fleet — so it is written to the same normalized shape and simply
    reports nothing settable if the query fails."""
    tool = _gpu_tool('nvidia-smi')
    if not tool:
        return None
    out, _e, rc = run([tool, '--query-gpu=index,name,power.limit,power.min_limit,'
                       'power.max_limit,power.default_limit,power.draw',
                       '--format=csv,noheader,nounits'], no_sudo=True)
    if rc != 0 or not out:
        return None
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(',')]
        if len(p) < 7:
            continue
        cur, lo, hi = _gpu_float(p[2]), _gpu_float(p[3]), _gpu_float(p[4])
        gpus.append({
            'index': _num(p[0]),
            'vendor': 'nvidia',
            'name': p[1] or 'GPU',
            'power_cap': {'current': cur, 'min': lo, 'max': hi, 'unit': 'W',
                          'default': _gpu_float(p[5])},
            'power_now': _gpu_float(p[6]),
            'throttled': None,
            # NVIDIA has no equivalent of AMD's power profiles; the field stays
            # for shape parity so the UI needs no vendor branch.
            'profile': {'current': None, 'available': []},
            'slowdown_temp': None, 'shutdown_temp': None,
            'settable': (['power_cap'] if cur is not None and lo is not None
                         and hi is not None and hi > lo else []),
        })
    return gpus


def _gpu_tunables(force=False):
    now = time.time()
    if not force and _gpu_tun_cache['ts'] and now - _gpu_tun_cache['ts'] < 8:
        return _gpu_tun_cache['data']
    vendor = _gpu_vendor()
    gpus, problem = [], None
    if vendor == 'nvidia':
        gpus = _nvidia_tunables()
    elif vendor == 'amd':
        gpus = _amd_tunables()
        if gpus is None and _gpu_tool('amd-smi'):
            # The give-away for the render-group problem: rocm-smi telemetry
            # works (sysfs) while amd-smi cannot open /dev/kfd.
            problem = ('amd-smi cannot read the GPU. The service user needs to be '
                       'in the `render` group — run the installer\'s --helpers-only '
                       'mode and restart the dashboard.')
    if gpus is None:
        gpus = []
    if vendor and not gpus and not problem:
        problem = 'No tunable settings are exposed by this GPU.'
    data = {'vendor': vendor, 'gpus': gpus, 'problem': problem,
            'helper': os.path.exists(GPU_TUNE_HELPER)}
    _gpu_tun_cache['ts'], _gpu_tun_cache['data'] = now, data
    return data


@bp.route('/api/gpu/tunables')
def gpu_tunables_get():
    return jsonify(_gpu_tunables())


def _tunable_gpu(index):
    for gpu in _gpu_tunables(force=True)['gpus']:
        if gpu['index'] == index:
            return gpu
    return None


def _run_gpu_helper(*args):
    if not os.path.exists(GPU_TUNE_HELPER):
        return err('The GPU tuning helper is not installed on this node. Run the '
                   'installer\'s --helpers-only mode (fleet-deploy --helpers).', 501)
    out, e, rc = run([GPU_TUNE_HELPER] + [str(a) for a in args])
    if rc != 0:
        return err((e or out or 'the GPU rejected the change').strip()[-300:], 500)
    _gpu_tun_cache['ts'] = 0.0          # next read is fresh
    return jsonify({'success': True, 'detail': (out or '').strip()[-200:]})


@bp.route('/api/gpu/<int:index>/power-cap', methods=['POST'])
def gpu_set_power_cap(index):
    gpu = _tunable_gpu(index)
    if gpu is None:
        return err('No such GPU', 404)
    if 'power_cap' not in gpu['settable']:
        return err('This GPU does not expose a settable power cap')
    try:
        watts = int((request.get_json() or {}).get('watts'))
    except (TypeError, ValueError):
        return err('Power cap must be a whole number of watts')
    lo, hi = gpu['power_cap']['min'], gpu['power_cap']['max']
    if watts < lo or watts > hi:
        return err('Power cap must be between %d and %d W for this card' % (lo, hi))
    return _run_gpu_helper('power-cap', index, watts)


@bp.route('/api/gpu/<int:index>/profile', methods=['POST'])
def gpu_set_profile(index):
    gpu = _tunable_gpu(index)
    if gpu is None:
        return err('No such GPU', 404)
    profile = ((request.get_json() or {}).get('profile') or '').strip().upper()
    if not RE_GPU_PROFILE.match(profile):
        return err('Invalid profile name')
    if profile not in (gpu['profile']['available'] or []):
        return err('This card does not offer the %s profile' % profile)
    return _run_gpu_helper('profile', index, profile)


# ─── Prometheus metrics ───────────────────────────────────────────────
# Public endpoint (a scraper can't use the session cookie). If
# DASHBOARD_METRICS_TOKEN is set it is required (?token= or Bearer); otherwise
# open, as is conventional for node_exporter-style endpoints on a trusted LAN.


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'gpu', 'order': 130, 'label': 'GPU', 'category': 'AI Tools',
          'nav': {'cat': 'ai', 'cat_order': 40, 'pages': [
                  {'id': 'gpu', 'label': 'GPU', 'icon': 'cpu'}]},
          'blueprint': bp}
