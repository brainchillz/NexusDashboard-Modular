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

bp = Blueprint('llama', __name__)

RE_LLAMA_FLAG = re.compile(r'^-{1,2}[A-Za-z0-9][A-Za-z0-9-]*$')
RE_LLAMA_VALUE = re.compile(r'^[A-Za-z0-9_./:,@=+-]*$')  # no spaces/quotes/newlines

# llama-server flags that take no value (presence-only) — used only to split an
# existing LLAMA_OPTS string into flag/value pairs for the editor.
LLAMA_BOOL_FLAGS = frozenset({
    '--verbose', '-v', '--log-disable', '--log-colors', '--log-verbose', '--offline',
    '--escape', '--no-escape', '--ignore-eos', '--perf', '--no-perf', '--flash-attn', '-fa',
    '--mlock', '--no-mmap', '--mmap', '--no-host', '--repack', '--no-repack',
    '--kv-offload', '-kvo', '--no-kv-offload', '-nkvo', '--direct-io', '-dio', '--no-direct-io', '-ndio',
    '--op-offload', '--no-op-offload', '--cpu-moe', '-cmoe',
    '--reuse-port', '--metrics', '--props', '--slots', '--no-slots',
    '--embedding', '--embeddings', '--rerank', '--reranking', '--jinja', '--no-jinja',
    '--cont-batching', '-cb', '--no-cont-batching', '-nocb', '--cache-prompt', '--no-cache-prompt',
    '--context-shift', '--no-context-shift', '--warmup', '--no-warmup', '--spm-infill',
    '--no-mmproj', '--mmproj-offload', '--no-mmproj-offload', '--kv-unified', '-kvu',
    '--no-webui', '--webui', '--check-tensors',
})


def _llama_read_conf():
    """Parse /etc/llama.conf into {bin, model, opts}; -m stripped from opts."""
    conf = {'bin': LLAMA_DEFAULT_BIN, 'model': '', 'opts': ''}
    try:
        with open(LLAMA_CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                val = val.strip().strip('"').strip("'")
                if key == 'LLAMA_BIN':
                    conf['bin'] = val
                elif key == 'LLAMA_MODEL':
                    conf['model'] = val
                elif key == 'LLAMA_OPTS':
                    conf['opts'] = val
    except OSError:
        pass
    conf['opts'] = re.sub(r'(^|\s)-m\s+\S+', ' ', conf['opts']).strip()
    return conf


def _llama_write_conf(conf):
    """Render and write /etc/llama.conf via the pinned tee grant.

    Returns (out, err, rc) — use run() (tuple), not run_safe() (dict)."""
    content = (f'LLAMA_BIN={conf["bin"]}\n'
               f'LLAMA_MODEL={conf["model"]}\n'
               f'LLAMA_OPTS="{conf["opts"]}"\n')
    return run(['tee', LLAMA_CONF], input_data=content)


def _llama_models():
    """All *.gguf under the models dir (excluding mmproj-* projector files)."""
    models = []
    try:
        for root, _dirs, files in os.walk(LLAMA_MODELS_DIR):
            for f in files:
                if f.endswith('.gguf') and not f.startswith('mmproj-'):
                    full = os.path.join(root, f)
                    models.append({'path': full, 'name': os.path.relpath(full, LLAMA_MODELS_DIR)})
    except OSError:
        pass
    return sorted(models, key=lambda m: m['name'])


def _llama_valid_model(path):
    """A model must be a .gguf that resolves inside the models dir and exists."""
    if not path or not RE_PATH.match(path) or not path.endswith('.gguf'):
        return False
    real = os.path.realpath(path)
    root = os.path.realpath(LLAMA_MODELS_DIR)
    return (real == root or real.startswith(root + os.sep)) and os.path.isfile(real)


def _llama_parse_opts(opts):
    """Split an opts string into [{flag, value}] pairs (mirrors the editor)."""
    tokens = opts.split()
    args, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith('-'):
            if '=' in tok:
                f, v = tok.split('=', 1)
                args.append({'flag': f, 'value': v}); i += 1; continue
            if tok in LLAMA_BOOL_FLAGS:
                args.append({'flag': tok, 'value': ''}); i += 1; continue
            if i + 1 < len(tokens) and not tokens[i + 1].startswith('-'):
                args.append({'flag': tok, 'value': tokens[i + 1]}); i += 2
            else:
                args.append({'flag': tok, 'value': ''}); i += 1
        else:
            i += 1  # stray bare token (shouldn't happen) — skip
    return args


def _llama_format_opts(args):
    parts = []
    for a in args:
        flag = (a.get('flag') or '').strip()
        val = (a.get('value') or '').strip()
        if not flag:
            continue
        parts.append(f'{flag} {val}' if val else flag)
    return ' '.join(parts)


def _llama_configured():
    return os.path.exists(LLAMA_CONF) or _unit_present(LLAMA_SERVICE)


def _llama_apply_restart():
    """Restart llama-server only if it is currently running (apply in place)."""
    if (run(['systemctl', 'is-active', LLAMA_SERVICE])[0] or '').strip() == 'active':
        run(['systemctl', 'restart', LLAMA_SERVICE])
        return True
    return False


@bp.route('/api/llama')
def llama_get():
    conf = _llama_read_conf()
    active = (run(['systemctl', 'is-active', LLAMA_SERVICE])[0] or '').strip() or 'inactive'
    enabled = (run(['systemctl', 'is-enabled', LLAMA_SERVICE])[0] or '').strip() or 'disabled'
    return jsonify({
        'configured': _llama_configured(),
        'service': {'active': active, 'enabled': enabled},
        'bin': conf['bin'],
        'model': conf['model'],
        'models_dir': LLAMA_MODELS_DIR,
        'models': _llama_models(),
        'args': _llama_parse_opts(conf['opts']),
    })


@bp.route('/api/llama/model', methods=['PUT'])
def llama_set_model():
    data = request.get_json() or {}
    model = (data.get('model') or '').strip()
    if not _llama_valid_model(model):
        return err('Invalid or unknown model path')
    conf = _llama_read_conf()
    conf['model'] = model
    _, e, rc = _llama_write_conf(conf)
    if rc != 0:
        return err(e or 'Failed to write llama config', 500)
    return jsonify({'success': True, 'restarted': _llama_apply_restart()})


def _llama_clean_args(raw):
    """Validate a raw [{flag, value}] list (shared by the live config and
    presets). Returns (clean_list, error_message_or_None). Drops empty flags and
    the -m/--model flag (managed separately by the Model card)."""
    if not isinstance(raw, list):
        return None, 'args must be a list'
    clean = []
    for a in raw:
        if not isinstance(a, dict):
            return None, 'Each arg must be an object'
        flag = (a.get('flag') or '').strip()
        val = (a.get('value') or '').strip()
        if not flag:
            continue
        if flag in ('-m', '--model'):
            continue
        if not RE_LLAMA_FLAG.match(flag):
            return None, f'Invalid flag: {flag}'
        if val and not RE_LLAMA_VALUE.match(val):
            return None, f'Invalid value for {flag}'
        clean.append({'flag': flag, 'value': val})
    return clean, None


@bp.route('/api/llama/args', methods=['PUT'])
def llama_set_args():
    data = request.get_json() or {}
    clean, e = _llama_clean_args(data.get('args'))
    if e:
        return err(e)
    conf = _llama_read_conf()
    conf['opts'] = _llama_format_opts(clean)
    _, we, rc = _llama_write_conf(conf)
    if rc != 0:
        return err(we or 'Failed to write llama config', 500)
    return jsonify({'success': True, 'restarted': _llama_apply_restart(), 'args': clean})


# Named profiles — save a model + a set of CLI args under a name and apply the
# pair to the live server in one click. State in llama_presets.json (atomic,
# gitignored). Back-compat: early presets stored args only (a bare list); those
# normalize to {model:'', args:[...]}.
RE_LLAMA_PRESET = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$')
LLAMA_PRESETS_FILE = os.environ.get('DASHBOARD_LLAMA_PRESETS_FILE',
                                    os.path.join(APP_DIR, 'llama_presets.json'))


def _norm_preset(v):
    """Normalize a stored preset to {model, args}. Accepts the legacy bare-list
    (args-only) shape and the current {model, args} dict shape."""
    if isinstance(v, list):
        return {'model': '', 'args': v}
    if isinstance(v, dict):
        args = v.get('args')
        return {'model': v.get('model') or '', 'args': args if isinstance(args, list) else []}
    return {'model': '', 'args': []}


def _load_llama_presets():
    try:
        with open(LLAMA_PRESETS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: _norm_preset(v) for k, v in data.items()}


@bp.route('/api/llama/presets')
def llama_presets_get():
    presets = _load_llama_presets()
    return jsonify({'presets': [{'name': k, 'model': v['model'], 'args': v['args']}
                                for k, v in sorted(presets.items())]})


@bp.route('/api/llama/presets', methods=['POST'])
def llama_presets_save():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not RE_LLAMA_PRESET.match(name):
        return err('Invalid preset name (letters, numbers, space, _ . - ; max 64)')
    # A profile may pin a model (optional). Validate it the same way the Model
    # card does — must resolve inside the models dir and exist.
    model = (data.get('model') or '').strip()
    if model and not _llama_valid_model(model):
        return err('Invalid or unknown model path')
    clean, e = _llama_clean_args(data.get('args'))
    if e:
        return err(e)
    presets = _load_llama_presets()
    presets[name] = {'model': model, 'args': clean}
    write_json_atomic(LLAMA_PRESETS_FILE, presets, 0o600)
    return jsonify({'success': True, 'name': name})


@bp.route('/api/llama/presets/<name>/apply', methods=['POST'])
def llama_presets_apply(name):
    """Apply a saved profile to the live config: write its model (if any) AND its
    args in one /etc/llama.conf rewrite, then restart if running."""
    presets = _load_llama_presets()
    if name not in presets:
        return err('No such preset', 404)
    p = presets[name]
    conf = _llama_read_conf()
    if p['model']:
        if not _llama_valid_model(p['model']):
            return err('Preset model no longer exists: ' + p['model'], 409)
        conf['model'] = p['model']
    conf['opts'] = _llama_format_opts(p['args'])
    _, we, rc = _llama_write_conf(conf)
    if rc != 0:
        return err(we or 'Failed to write llama config', 500)
    return jsonify({'success': True, 'restarted': _llama_apply_restart(),
                    'model': conf['model'], 'args': p['args']})


@bp.route('/api/llama/presets/<name>', methods=['DELETE'])
def llama_presets_delete(name):
    presets = _load_llama_presets()
    if name not in presets:
        return err('No such preset', 404)
    del presets[name]
    write_json_atomic(LLAMA_PRESETS_FILE, presets, 0o600)
    return jsonify({'success': True})


# ─── Model download (Hugging Face GGUF pull) ──────────────────────────
# Writing into the root-owned models dir is done by the root-owned wrapper
# storage-dashboard-model-fetch (the trust boundary — it re-validates repo +
# filename, confines output to the models dir, and atomically renames the
# finished file into place). The pull runs in a background thread (one at a time)
# because a multi-GB fetch never fits run()'s 120s window; live progress is read
# by statting the .partial file the wrapper writes.
MODEL_FETCH_HELPER = HELPER_PREFIX + '-model-fetch'
RE_HF_REPO = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$')
RE_HF_FILE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*\.gguf$')

# Job state lives in a file, not memory, so it's consistent across gunicorn
# workers (a status poll may land on a different worker than the one that ran the
# POST / owns the download thread). The .partial byte progress is read straight
# from the filesystem, which is worker-agnostic anyway.
MODEL_JOB_FILE = os.environ.get('DASHBOARD_MODEL_JOB_FILE',
                                os.path.join(APP_DIR, 'model_job.json'))
_model_job_lock = threading.Lock()


def _load_model_job():
    try:
        with open(MODEL_JOB_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'state': 'idle'}


def _save_model_job(job):
    write_json_atomic(MODEL_JOB_FILE, job, 0o600)


def _hf_resolve_url(repo, filename):
    return 'https://huggingface.co/%s/resolve/main/%s' % (repo, filename)


def _model_fetch_run(repo, filename, token):
    """Thread body: run the wrapper (blocking) and record the outcome to file."""
    out, e, rc = run([MODEL_FETCH_HELPER, repo, filename],
                     input_data=(token + '\n') if token else None)
    with _model_job_lock:
        job = _load_model_job()
        if job.get('filename') != filename:
            return  # superseded by a newer job
        if rc == 0:
            job.update(state='done', finished=time.time())
        else:
            job.update(state='error', finished=time.time(),
                       error=(e or out or 'download failed').strip()[-300:])
        _save_model_job(job)


@bp.route('/api/llama/models/pull', methods=['POST'])
def llama_model_pull():
    data = request.get_json() or {}
    repo = (data.get('repo') or '').strip()
    filename = (data.get('filename') or '').strip()
    token = (data.get('token') or '').strip()
    if not RE_HF_REPO.match(repo):
        return err('Invalid repo id (expected e.g. TheBloke/Model-GGUF)')
    if not RE_HF_FILE.match(filename):
        return err('Invalid filename (must be a .gguf, no path separators)')
    dest = os.path.join(LLAMA_MODELS_DIR, filename)
    if os.path.exists(dest):
        return err('A model with that filename already exists', 409)
    with _model_job_lock:
        if _load_model_job().get('state') == 'downloading':
            return err('A download is already in progress', 409)
    # Best-effort total size + existence check via HEAD (unknown -> 0 = no % bar).
    total = 0
    try:
        import urllib.request
        req = urllib.request.Request(_hf_resolve_url(repo, filename), method='HEAD')
        if token:
            req.add_header('Authorization', 'Bearer ' + token)
        with urllib.request.urlopen(req, timeout=10) as r:
            total = int(r.headers.get('Content-Length') or 0)
    except Exception as ex:
        return err('Cannot reach that model on Hugging Face: ' + str(ex)[-200:], 502)
    with _model_job_lock:
        _save_model_job({'state': 'downloading', 'repo': repo, 'filename': filename,
                         'total': total, 'started': time.time()})
    threading.Thread(target=_model_fetch_run, args=(repo, filename, token),
                     daemon=True).start()
    return jsonify({'success': True, 'total': total})


@bp.route('/api/llama/models/pull/status')
def llama_model_pull_status():
    job = _load_model_job()
    # Live byte progress: stat the .partial (created by the wrapper as root; the
    # models dir is world-traversable so we can read its size).
    if job.get('state') == 'downloading' and job.get('filename'):
        part = os.path.join(LLAMA_MODELS_DIR, job['filename'] + '.partial')
        try:
            job['downloaded'] = os.path.getsize(part)
        except OSError:
            job['downloaded'] = 0
    return jsonify(job)


# Lightweight in-memory tokens/sec: derived from the tokens_predicted_total
# counter between successive /health polls. No persistence — a real trend lands
# with the history store (plan 01). A counter that decreases means llama-server
# restarted (model switch), so that interval is skipped.
_llama_rate = {'ts': 0.0, 'tokens': None}


def _llama_derive_rate(result):
    tot = (result.get('metrics') or {}).get('tokens_predicted_total')
    if not isinstance(tot, (int, float)):
        return
    now = time.time()
    prev_t, prev_n = _llama_rate['ts'], _llama_rate['tokens']
    if prev_n is not None and prev_t and now > prev_t and tot >= prev_n:
        result['tokens_per_sec'] = round((tot - prev_n) / (now - prev_t), 1)
    _llama_rate['ts'], _llama_rate['tokens'] = now, tot


@bp.route('/api/llama/health')
def llama_health():
    """Proxy llama-server's /health + /metrics (no sudo) for the dashboard card."""
    import urllib.request
    base = LLAMA_URL.rstrip('/')
    result = {'ok': False, 'status': 'unknown', 'metrics': {}}
    try:
        with urllib.request.urlopen(base + '/health', timeout=3) as r:
            data = json.loads(r.read().decode())
            result['ok'] = True
            result['status'] = data.get('status', 'ok')
    except Exception as ex:
        result['error'] = str(ex)
    try:
        with urllib.request.urlopen(base + '/metrics', timeout=3) as r:
            text = r.read().decode()
            metrics = {}
            for m in re.finditer(r'^(\w[\w:]*)\s+([0-9.eE+-]+)\s*$', text, re.M):
                name, val = m.group(1), m.group(2)
                short = name.split(':', 1)[-1] if ':' in name else name
                try:
                    metrics[short] = float(val) if ('.' in val or 'e' in val.lower()) else int(val)
                except ValueError:
                    pass
            result['metrics'] = metrics
    except Exception:
        pass
    _llama_derive_rate(result)
    return jsonify(result)


# ─── Network configuration (netplan) ──────────────────────────────────
# The dashboard owns a single netplan file (90-storage-dashboard.yaml), rendered
# from an app-owned JSON config (the source of truth). Changing an interface IP
# is the one operation that can sever the admin's own connection, so it uses a
# **dual-IP, two-step** flow instead of replace-and-race:
#
#   1. Apply  — the new address is ADDED alongside the old one (networkd holds
#      both), keeping the old gateway/DNS active. The admin's current session is
#      never touched, so lockout is impossible during verification. A janitor
#      timer removes the new address after PENDING_WINDOW if nothing is finalized.
#   2. Finalize — once the admin reaches the dashboard on the new address (a
#      handoff token logs them straight in there), the old address is dropped and
#      the gateway/DNS switched. A short FINALIZE_WINDOW timer rolls all the way
#      back to the previous config unless the new-address page heartbeat-confirms,
#      covering the only residual risk (a bad gateway at the final commit).
#
# The privileged write + `netplan generate` + `netplan apply` happen in a
# root-owned helper.


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'llamacpp', 'label': 'LLama.cpp', 'category': 'AI Tools',
          'blueprint': bp}
