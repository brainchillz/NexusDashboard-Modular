"""Extracted verbatim from NexusStationDashboard app.py (Stage 1 split).
Routes converted @app.route -> @bp.route; logic unchanged."""
import os
import re
import sys
import signal
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
#
# Only list a flag here if it truly takes NO value: a value-taking flag listed
# as boolean has its value parsed as a stray token and silently DROPPED on save
# (see _llama_parse_opts). upstream has been migrating former booleans to
# 'on|off|auto' enums — as of b10333 those are --flash-attn/-fa, --log-colors,
# --color, --fit, --fit-print and --reasoning, none of which belong here.
LLAMA_BOOL_FLAGS = frozenset({
    '--verbose', '-v', '--log-disable', '--log-verbose', '--offline',
    '--escape', '--no-escape', '--ignore-eos', '--perf', '--no-perf',
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
    """All *.gguf under the models dir (excluding mmproj-* projector files).

    A split model contributes only its FIRST part: llama.cpp is handed
    `-00001-of-000NN.gguf` and opens the siblings itself, so listing every part
    would fill the picker with entries that cannot be loaded on their own."""
    models = []
    try:
        for root, _dirs, files in os.walk(LLAMA_MODELS_DIR):
            for f in files:
                # `._name` files are macOS AppleDouble resource forks, left
                # behind when a model is copied over SMB/AFP from a Mac. They
                # are ~4 KB of metadata carrying a .gguf suffix, so they reach
                # the model picker looking like real models and fail to load.
                # They also defeat the mmproj- check, which is why this tests
                # the prefixes together.
                if not f.endswith('.gguf') or f.startswith(('mmproj-', '._')):
                    continue
                split = RE_GGUF_SPLIT.match(f)
                if split and split.group('idx') != '00001':
                    continue
                full = os.path.join(root, f)
                entry = {'path': full, 'name': os.path.relpath(full, LLAMA_MODELS_DIR)}
                try:
                    entry['size'] = os.path.getsize(full)
                except OSError:
                    entry['size'] = 0
                if split:
                    entry['parts'] = int(split.group('total'))
                    # Report the SET's size, not part 1's — the library view is
                    # answering "how much disk is this model costing me".
                    for sib in files:
                        m2 = RE_GGUF_SPLIT.match(sib)
                        if m2 and m2.group('stem') == split.group('stem') \
                                and sib != f:
                            try:
                                entry['size'] += os.path.getsize(os.path.join(root, sib))
                            except OSError:
                                pass
                models.append(entry)
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


def _llama_web_ui(args):
    """Where llama-server's own web UI is, from the configured flags.

    Only the port and the bind address are decided here — the browser builds the
    link against the host it is already talking to, which is what makes it work
    whether the node was reached by name or by IP. A loopback bind means the UI
    is unreachable from anywhere but the node itself, so say so rather than
    offering a link that cannot work. (`-p` is deliberately NOT accepted: in
    llama.cpp that is --prompt, not --port.)"""
    port, host = 8080, '127.0.0.1'
    for a in args or ():
        flag, val = a.get('flag'), (a.get('value') or '').strip()
        if flag == '--port' and val.isdigit():
            port = int(val)
        elif flag == '--host' and val:
            host = val
    return {'port': port, 'host': host,
            'reachable': host not in ('127.0.0.1', 'localhost', '::1', '')}


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
        'models_free_bytes': _models_free_bytes(),
        'args': _llama_parse_opts(conf['opts']),
        'web_ui': _llama_web_ui(_llama_parse_opts(conf['opts'])),
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


# ─── Hugging Face model download ──────────────────────────────────────
# llama.cpp loads GGUF and nothing else, so a repo without .gguf files is
# refused with that stated plainly rather than a generic 404.
#
# Layout: <models>/<group>/. A "group" is one downloadable unit — a single
# .gguf, or the set of `-00001-of-00003.gguf` parts a large quant is split
# into. Parts must land side by side because llama.cpp opens part 1 and expects
# its siblings in the same directory, which is why the per-model directory
# matters rather than being cosmetic.
#
# Privileges: none. The models dir is group-writable (group `models`, setgid)
# and the service user is a member, so the transfer runs unprivileged. This
# replaced a root-owned sudo helper: fetching hundreds of GB of remote content
# as root was the worse trade, and helper updates could not be pushed by
# `fleet-deploy --helpers` to the legacy-prefixed nodes that most need this.
#
# Durability: a several-hundred-GB pull outlives the dashboard process — a
# deploy restarts the unit mid-transfer. So the download runs in a DETACHED
# child (start_new_session, reparented to init), records its pid in the job
# file, and resumes with an HTTP Range request. Restarting the dashboard, or
# the box, costs only the bytes since the last flush.

HF_API = 'https://huggingface.co/api/models/'

RE_HF_REPO = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$')
# An rfilename may sit in a subdirectory (that is how big split quants ship), so
# slashes are allowed — but no traversal, no leading slash, no empty segment.
RE_HF_RFILE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*\.gguf$')
RE_HF_GROUP = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
RE_GGUF_SPLIT = re.compile(r'^(?P<stem>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$')

LLAMA_HF_FILE = os.environ.get('DASHBOARD_LLAMA_HF_FILE',
                               os.path.join(APP_DIR, 'llama_hf.json'))
MODEL_JOB_FILE = os.environ.get('DASHBOARD_MODEL_JOB_FILE',
                                os.path.join(APP_DIR, 'model_job.json'))
_model_job_lock = threading.Lock()

_CHUNK = 1 << 20
# Link rates are decimal: 1 Mbps = 1e6 bits, so the byte budget is mbps*1e6/8.
# 600 Mbps ~= 75 MB/s.
_MBPS_TO_BPS = 1e6 / 8
MAX_RATE_MBPS = 100000


# ─── token + settings (never leaves the node) ─────────────────────────

def _load_hf_settings():
    try:
        with open(LLAMA_HF_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _hf_token():
    return (_load_hf_settings().get('token') or '').strip()


def _hf_rate_mbps():
    try:
        return max(0, min(MAX_RATE_MBPS, int(_load_hf_settings().get('rate_mbps') or 0)))
    except (TypeError, ValueError):
        return 0


@bp.route('/api/llama/hf')
def llama_hf_get():
    """Token PRESENCE and the saved rate cap. The token itself is never
    returned — it is write-only from the browser's point of view."""
    return jsonify({'token_set': bool(_hf_token()), 'rate_mbps': _hf_rate_mbps(),
                    'max_rate_mbps': MAX_RATE_MBPS})


@bp.route('/api/llama/hf', methods=['PUT'])
def llama_hf_set():
    """Paste-and-add a token, and/or set the default rate cap. Omitting a field
    leaves it alone, so saving a rate cannot wipe the token."""
    data = request.get_json() or {}
    cur = _load_hf_settings()
    if 'token' in data:
        tok = (data.get('token') or '').strip()
        # Deliberately not pattern-matched beyond the obvious: HF has changed
        # its token format before, and a wrong guess here would lock out a
        # valid credential. A bad token simply fails the next API call.
        if tok and (len(tok) > 512 or any(c.isspace() for c in tok)):
            return err('That does not look like an access token')
        cur['token'] = tok
    if 'rate_mbps' in data:
        try:
            rate = int(data.get('rate_mbps') or 0)
        except (TypeError, ValueError):
            return err('Rate limit must be a whole number of Mbps')
        if rate < 0 or rate > MAX_RATE_MBPS:
            return err('Rate limit must be between 0 (unlimited) and %d Mbps' % MAX_RATE_MBPS)
        cur['rate_mbps'] = rate
    write_json_atomic(LLAMA_HF_FILE, cur, 0o600)
    return jsonify({'success': True, 'token_set': bool((cur.get('token') or '').strip()),
                    'rate_mbps': _hf_rate_mbps()})


@bp.route('/api/llama/hf/token', methods=['DELETE'])
def llama_hf_token_delete():
    cur = _load_hf_settings()
    cur.pop('token', None)
    write_json_atomic(LLAMA_HF_FILE, cur, 0o600)
    return jsonify({'success': True, 'token_set': False})


# ─── repo inspection ──────────────────────────────────────────────────

def _hf_get_json(url, token, timeout=25):
    req = urllib.request.Request(url, headers={'User-Agent': 'nexus-dashboard'})
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def _gguf_group(rfilename):
    """The download unit a GGUF belongs to.

    Two layouts in the wild: a directory per quant
    (`Meta-Llama-3.1-70B-Instruct-Q8_0/....-00001-of-00002.gguf`) and flat files
    (`Llama-3.2-3B-Instruct-IQ3_M.gguf`). Both must be fetched as a unit, so the
    key is the subdirectory when there is one, else the filename with any split
    suffix stripped."""
    if '/' in rfilename:
        return rfilename.split('/', 1)[0]
    m = RE_GGUF_SPLIT.match(rfilename)
    return m.group('stem') if m else rfilename[:-len('.gguf')]


def _hf_gguf_groups(repo, token):
    """(groups, error). Each group is one selectable download."""
    try:
        info = _hf_get_json(HF_API + repo + '?blobs=true', token)
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403):
            return None, ('That repository is gated or private. Add a Hugging Face '
                          'access token that has access to it, then try again.')
        if ex.code == 404:
            return None, 'No such model on Hugging Face: ' + repo
        return None, 'Hugging Face returned HTTP %d' % ex.code
    except Exception as ex:
        return None, 'Cannot reach Hugging Face: ' + str(ex)[-200:]
    sibs = info.get('siblings') or []
    names = [(s.get('rfilename') or '') for s in sibs]
    sizes = {(s.get('rfilename') or ''): int(s.get('size') or 0) for s in sibs}
    ggufs = [n for n in names if n.lower().endswith('.gguf') and RE_HF_RFILE.match(n)]
    if not ggufs:
        return None, ('llama.cpp needs a GGUF, and %s does not publish one. '
                      'Look for a GGUF conversion of this model — usually a '
                      'separate repository whose name ends in "-GGUF".' % repo)
    groups = {}
    for n in ggufs:
        gname = _gguf_group(n)
        if not RE_HF_GROUP.match(gname):
            continue          # unusable as a directory name; skip rather than guess
        entry = groups.setdefault(gname, {'name': gname, 'files': [],
                                          'bytes': 0, 'parts': 0})
        entry['files'].append(n)
        entry['bytes'] += sizes.get(n, 0)
        entry['parts'] += 1
    if not groups:
        return None, 'This repository has GGUF files, but none with a usable name.'
    out = []
    for gname in sorted(groups):
        entry = groups[gname]
        entry['files'].sort()
        entry['installed'] = os.path.isdir(os.path.join(LLAMA_MODELS_DIR, gname))
        out.append(entry)
    return out, None


@bp.route('/api/llama/hf/repo')
def llama_hf_repo():
    repo = (request.args.get('repo') or '').strip()
    if not RE_HF_REPO.match(repo):
        return err('Invalid repo id (expected e.g. bartowski/Llama-3.2-3B-Instruct-GGUF)')
    groups, e = _hf_gguf_groups(repo, _hf_token())
    if e:
        return err(e)
    return jsonify({'repo': repo, 'groups': groups,
                    'models_dir': LLAMA_MODELS_DIR,
                    'free_bytes': _models_free_bytes()})


# ─── job state ────────────────────────────────────────────────────────

def _load_model_job():
    try:
        with open(MODEL_JOB_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {'state': 'idle'}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'state': 'idle'}


def _save_model_job(job):
    job['updated'] = time.time()
    write_json_atomic(MODEL_JOB_FILE, job, 0o600)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _models_free_bytes():
    try:
        st = os.statvfs(LLAMA_MODELS_DIR)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def _job_view(job):
    """The job as the UI should see it. A job whose worker is gone is reported
    as `interrupted` — the partial files are intact and resumable, so this is a
    resumable pause, not a failure."""
    job = dict(job)
    if job.get('state') == 'downloading' and not _pid_alive(job.get('pid')):
        job['state'] = 'interrupted'
        job['error'] = job.get('error') or 'The download was interrupted (service restart or reboot). It can be resumed.'
    job.pop('token', None)
    return job


@bp.route('/api/llama/models/pull/status')
def llama_model_pull_status():
    return jsonify(_job_view(_load_model_job()))


# ─── starting / cancelling / resuming ─────────────────────────────────

# Refuse to start unless the destination has room for what is left, plus a
# margin — better a clear refusal than a dead 300 GB transfer that fills the
# root filesystem at 3am.
SPACE_MARGIN = 0.02


def _group_dest_dir(group):
    """Confined destination directory for a group. Returns None if the name
    would escape the models dir (it is API-supplied, so it is never trusted)."""
    if not RE_HF_GROUP.match(group or ''):
        return None
    d = os.path.join(LLAMA_MODELS_DIR, group)
    root = os.path.realpath(LLAMA_MODELS_DIR)
    real = os.path.realpath(d)
    if real != root and not real.startswith(root + os.sep):
        return None
    if os.path.dirname(real) != root:
        return None
    return d


def _spawn_fetch_worker():
    """Run the transfer in a DETACHED child so it survives this process.

    start_new_session detaches it from the dashboard's process group, so a
    `systemctl restart` (every deploy does one) leaves the download running and
    it is reparented to init. The child re-reads the job file for its inputs,
    which keeps the token out of its argv."""
    argv = [sys.executable, os.path.join(APP_DIR, 'app.py'), 'llama-fetch']
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, cwd=APP_DIR)
    return p.pid


@bp.route('/api/llama/models/pull', methods=['POST'])
def llama_model_pull():
    data = request.get_json() or {}
    repo = (data.get('repo') or '').strip()
    group = (data.get('group') or '').strip()
    if not RE_HF_REPO.match(repo):
        return err('Invalid repo id (expected e.g. bartowski/Llama-3.2-3B-Instruct-GGUF)')
    dest_dir = _group_dest_dir(group)
    if dest_dir is None:
        return err('Invalid model selection')
    rate = _hf_rate_mbps()
    if data.get('rate_mbps') is not None:
        try:
            rate = int(data.get('rate_mbps'))
        except (TypeError, ValueError):
            return err('Rate limit must be a whole number of Mbps')
        if rate < 0 or rate > MAX_RATE_MBPS:
            return err('Rate limit must be between 0 (unlimited) and %d Mbps' % MAX_RATE_MBPS)

    with _model_job_lock:
        cur = _job_view(_load_model_job())
        if cur.get('state') == 'downloading':
            return err('A download is already in progress', 409)

    token = _hf_token()
    groups, e = _hf_gguf_groups(repo, token)
    if e:
        return err(e, 502)
    sel = next((grp for grp in groups if grp['name'] == group), None)
    if sel is None:
        return err('That model is not in %s' % repo, 404)

    files = []
    have = 0
    for rf in sel['files']:
        dest = os.path.join(dest_dir, os.path.basename(rf))
        files.append({'rfilename': rf, 'dest': dest})
        for p in (dest, dest + '.partial'):
            try:
                have += os.path.getsize(p)
                break
            except OSError:
                pass
    need = int(max(0, sel['bytes'] - have) * (1 + SPACE_MARGIN))
    free = _models_free_bytes()
    if free and need and free < need:
        return err('Not enough space in %s: needs ~%.1f GB free, has %.1f GB'
                   % (LLAMA_MODELS_DIR, need / 1e9, free / 1e9), 507)

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as ex:
        return err('Cannot create %s: %s. The models directory must be writable '
                   'by this service (group `models`).' % (dest_dir, ex), 500)

    with _model_job_lock:
        job = {'state': 'downloading', 'repo': repo, 'group': group,
               'dir': dest_dir, 'files': files, 'total': sel['bytes'],
               'downloaded': have, 'parts': sel['parts'], 'current': '',
               'rate_mbps': rate, 'started': time.time(), 'pid': 0,
               'error': None, 'finished': None}
        _save_model_job(job)
        job['pid'] = _spawn_fetch_worker()
        _save_model_job(job)
    return jsonify({'success': True, 'total': sel['bytes'], 'resumed_bytes': have,
                    'rate_mbps': rate, 'dir': dest_dir})


@bp.route('/api/llama/models/pull/resume', methods=['POST'])
def llama_model_pull_resume():
    """Restart the worker for an interrupted job. The partial files decide where
    it picks up, so this is safe to call more than once."""
    with _model_job_lock:
        job = _load_model_job()
        view = _job_view(job)
        if view.get('state') not in ('interrupted', 'error', 'cancelled'):
            return err('Nothing to resume', 409)
        if not job.get('files'):
            return err('That job has no files to resume', 409)
        job['state'] = 'downloading'
        job['error'] = None
        job['finished'] = None
        job['cancel'] = False
        _save_model_job(job)
        job['pid'] = _spawn_fetch_worker()
        _save_model_job(job)
    return jsonify({'success': True})


@bp.route('/api/llama/models/pull/cancel', methods=['POST'])
def llama_model_pull_cancel():
    """Ask the worker to stop. Partial files are LEFT in place so the transfer
    can resume; deleting hundreds of GB because someone hit cancel would be its
    own kind of bug."""
    with _model_job_lock:
        job = _load_model_job()
        if _job_view(job).get('state') != 'downloading':
            return err('No download is running', 409)
        job['cancel'] = True
        _save_model_job(job)
        pid = job.get('pid')
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    return jsonify({'success': True})


# ─── the detached worker (CLI: `python app.py llama-fetch`) ───────────

def _hf_resolve(repo, rfilename):
    """Download URL for one file in a repo. A named seam so the transfer can be
    pointed at a local server under test without mocking urllib itself."""
    return 'https://huggingface.co/%s/resolve/main/%s' % (repo, rfilename)


def _fetch_file(url, dest, token, rate_bps, on_progress, should_stop):
    """Fetch one file with resume and rate limiting. Returns bytes now on disk.

    Resume is an HTTP Range request against the .partial. A server that ignores
    Range answers 200 with the whole body, so the partial is discarded rather
    than corrupted by appending to it."""
    if os.path.exists(dest):
        return os.path.getsize(dest)
    part = dest + '.partial'
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={'User-Agent': 'nexus-dashboard'})
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    if have:
        req.add_header('Range', 'bytes=%d-' % have)
    with urllib.request.urlopen(req, timeout=60) as r:
        resumed = (getattr(r, 'status', r.getcode()) == 206)
        if have and not resumed:
            have = 0
        mode = 'ab' if resumed and have else 'wb'
        started = time.monotonic()
        session_bytes = 0
        with open(part, mode) as f:
            while True:
                if should_stop():
                    return have
                chunk = r.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                session_bytes += len(chunk)
                if rate_bps:
                    # Average-rate pacing: sleep until this session's bytes are
                    # "due" at the configured rate.
                    due = session_bytes / rate_bps
                    slept = time.monotonic() - started
                    if due > slept:
                        time.sleep(due - slept)
                on_progress(len(chunk))
    os.replace(part, dest)
    return have


def cli_llama_fetch(argv=None):
    """Detached download worker. Reads the job file, fetches every file in the
    group, and records progress back to the same file. Never raises out — a
    crash here must leave a readable `error` state, not a job stuck at
    `downloading` forever."""
    job = _load_model_job()
    if job.get('state') != 'downloading' or not job.get('files'):
        return 0
    job['pid'] = os.getpid()
    _save_model_job(job)

    stopping = {'v': False}

    def _sigterm(_s, _f):
        stopping['v'] = True

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    token = _hf_token()
    rate_bps = int((job.get('rate_mbps') or 0) * _MBPS_TO_BPS)
    done_bytes = 0
    last_write = [0.0]

    def should_stop():
        if stopping['v']:
            return True
        # Also honour a cancel flag written by the API, in case SIGTERM was lost.
        if time.time() - last_write[0] > 2.0:
            if _load_model_job().get('cancel'):
                stopping['v'] = True
        return stopping['v']

    def flush(extra=0, current=None, force=False):
        now = time.time()
        if not force and now - last_write[0] < 2.0:
            return
        last_write[0] = now
        j = _load_model_job()
        if j.get('group') != job.get('group'):
            stopping['v'] = True     # superseded by a newer job
            return
        j['downloaded'] = done_bytes + extra
        if current:
            j['current'] = current
        j['pid'] = os.getpid()
        _save_model_job(j)

    try:
        for spec in job['files']:
            if should_stop():
                break
            url = _hf_resolve(job['repo'], spec['rfilename'])
            name = os.path.basename(spec['rfilename'])
            flush(current=name, force=True)
            counter = {'n': 0}

            def on_progress(n, _c=counter, _name=name):
                _c['n'] += n
                flush(extra=_c['n'], current=_name)

            written = _fetch_file(url, spec['dest'], token, rate_bps,
                                  on_progress, should_stop)
            done_bytes += written
        j = _load_model_job()
        if j.get('group') != job.get('group'):
            return 0
        if stopping['v']:
            j.update(state='cancelled', finished=time.time(), cancel=False,
                     error='Cancelled. Partial files were kept — resume to continue.')
        else:
            j.update(state='done', finished=time.time(), downloaded=done_bytes,
                     current='', error=None)
        _save_model_job(j)
    except Exception as ex:
        j = _load_model_job()
        if j.get('group') == job.get('group'):
            j.update(state='error', finished=time.time(),
                     error=('%s: %s' % (type(ex).__name__, ex))[-300:])
            _save_model_job(j)
        return 1
    return 0


# ─── Model backup / restore to a shared location ──────────────────────
# Adapted from SparkDash's backup.py. Same shape — copy a model to a share,
# leave a self-describing manifest beside it, one job at a time — but the
# durability model is this dashboard's: a detached worker and a job file, not
# an in-memory asyncio task, because a 200 GB copy outlives the process.
#
# Layout: <base>/NexusDashboard/Models/<group>/ plus nexus-backup.json.
# The directory name is the model group, so a restore is a copy back into the
# models dir under the name llama.cpp already expects.
#
# rsync does the copying: --partial makes an interrupted copy resumable, and a
# --checksum dry run is what makes "verify" mean something. It is NOT assumed
# present (node4/node5 ship without it), so its absence degrades to a
# stated refusal rather than a traceback.

BACKUP_SUBDIR = os.path.join('NexusDashboard', 'Models')
BACKUP_MANIFEST = 'nexus-backup.json'
BACKUP_JOB_FILE = os.environ.get('DASHBOARD_MODEL_BACKUP_JOB_FILE',
                                 os.path.join(APP_DIR, 'model_backup_job.json'))
BACKUP_DEFAULT_BASE = os.environ.get('DASHBOARD_BACKUP_BASE', '/mnt/llm')
_backup_lock = threading.Lock()

# rsync: recurse, copy symlinks as symlinks, preserve perms+times, drop
# owner/group so it works onto a root-squashed NFS export, and --partial so an
# interrupted copy resumes instead of restarting.
RSYNC_BASE = ['rsync', '-rlt', '--partial']


def _rsync_path():
    return shutil.which('rsync')


def _backup_base():
    return (_load_hf_settings().get('backup_base') or BACKUP_DEFAULT_BASE).rstrip('/') or '/'


def _backup_root(base=None):
    return os.path.join(base or _backup_base(), BACKUP_SUBDIR)


def _is_mountpoint(path):
    try:
        return os.path.ismount(path)
    except OSError:
        return False


def _fs_source(path):
    """Which device/export backs `path`, for the UI to show. Best effort."""
    out, _e, rc = run(['findmnt', '-n', '-o', 'SOURCE', '--target', path])
    return (out or '').strip() if rc == 0 else ''


def _free_bytes(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def _dir_bytes(path):
    """Bytes on disk under `path`. Walks rather than shelling to du so it works
    the same on both distro families."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _backup_status(base=None):
    """Everything the UI needs to decide whether a backup can run — and to show
    the operator WHERE it would go.

    `mounted` is the load-bearing field. An unmounted mountpoint is an empty
    directory on the root filesystem, so backing up to it silently fills the
    system disk while looking like it went to the share. That has already
    happened in the wild here: /mnt/llm is a real NFS mount on nexus and a bare
    empty directory on node4."""
    base = base or _backup_base()
    exists = os.path.isdir(base)
    return {
        'base': base,
        'root': _backup_root(base),
        'exists': exists,
        'writable': exists and os.access(base, os.W_OK),
        'mounted': _is_mountpoint(base),
        'source': _fs_source(base) if exists else '',
        'free_bytes': _free_bytes(base) if exists else 0,
        'allow_local': bool(_load_hf_settings().get('backup_allow_local')),
        'rsync': bool(_rsync_path()),
    }


def _backup_precondition(st):
    """Why a backup cannot start, or None. Ordered most-fundamental first."""
    if not st['rsync']:
        return ('rsync is not installed on this node, so model backup is '
                'unavailable. Install it (apt install rsync / dnf install rsync).')
    if not st['exists']:
        return 'The backup location %s does not exist.' % st['base']
    if not st['writable']:
        return 'The backup location %s is not writable by this service.' % st['base']
    if not st['mounted'] and not st['allow_local']:
        return ('%s is not a mount point — it looks like a share that failed to '
                'mount, and copying there would fill the local disk instead. '
                'Mount it, or tick "allow a non-mounted location" if that path '
                'really is local storage.' % st['base'])
    return None


def _backup_dir(group, base=None):
    """Confined backup directory for a group, or None. The group is validated
    the same way the download path validates it — it must be one path segment
    and land directly under the backup root."""
    if not RE_HF_GROUP.match(group or ''):
        return None
    root = _backup_root(base)
    d = os.path.join(root, group)
    if os.path.dirname(os.path.normpath(d)) != os.path.normpath(root):
        return None
    return d


def _read_manifest(d):
    try:
        with open(os.path.join(d, BACKUP_MANIFEST)) as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _local_groups():
    """Model groups in the models dir, as {group: [paths]}.

    A directory of .gguf files is one group. Loose top-level .gguf files are
    grouped by _gguf_group, so the shards of a split model collapse into ONE
    entry instead of appearing as separate models — backing up shard 1 alone
    would otherwise produce a 13 MB "backup" of a 63 GB model that looks
    perfectly fine in the list. (Real case: gpt-oss-120b on node4, whose
    first shard genuinely is that small.) Only .gguf is collected; llama.cpp's
    .etag sidecars are download bookkeeping, not part of the model."""
    out = {}
    try:
        for name in sorted(os.listdir(LLAMA_MODELS_DIR)):
            p = os.path.join(LLAMA_MODELS_DIR, name)
            if os.path.isdir(p):
                try:
                    if any(f.endswith('.gguf') for f in os.listdir(p)):
                        out[name] = [p]
                except OSError:
                    pass
            elif name.endswith('.gguf') and not name.startswith(('mmproj-', '._')):
                out.setdefault(_gguf_group(name), []).append(p)
    except OSError:
        pass
    return out


def _paths_bytes(paths):
    total = 0
    for p in paths:
        if os.path.isdir(p):
            total += _dir_bytes(p)
        else:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
    return total


@bp.route('/api/llama/backups')
def llama_backups_get():
    st = _backup_status()
    local = _local_groups()
    items = []
    root = st['root']
    if os.path.isdir(root):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            names = []
        for name in names:
            d = os.path.join(root, name)
            if not os.path.isdir(d) or not RE_HF_GROUP.match(name):
                continue
            man = _read_manifest(d)
            items.append({'group': name,
                          'size': man.get('size_bytes') or _dir_bytes(d),
                          'saved_at': man.get('saved_at'),
                          'source_node': man.get('source_node'),
                          'repo': man.get('repo'),
                          'present_locally': name in local})
    return jsonify({'status': st, 'problem': _backup_precondition(st),
                    'backups': items,
                    'local_groups': [{'group': gname, 'size': _paths_bytes(gpaths),
                                      'files': len(gpaths)}
                                     for gname, gpaths in sorted(local.items())]})


def _load_backup_job():
    try:
        with open(BACKUP_JOB_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {'state': 'idle'}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'state': 'idle'}


def _save_backup_job(job):
    job['updated'] = time.time()
    write_json_atomic(BACKUP_JOB_FILE, job, 0o600)


def _backup_job_view(job):
    job = dict(job)
    if job.get('state') == 'running' and not _pid_alive(job.get('pid')):
        job['state'] = 'interrupted'
        job['error'] = job.get('error') or ('The copy was interrupted (service restart '
                                            'or reboot). Re-running it resumes.')
    return job


@bp.route('/api/llama/backups/job')
def llama_backup_job_get():
    return jsonify(_backup_job_view(_load_backup_job()))


@bp.route('/api/llama/backups/job/cancel', methods=['POST'])
def llama_backup_job_cancel():
    with _backup_lock:
        job = _load_backup_job()
        if _backup_job_view(job).get('state') != 'running':
            return err('No backup or restore is running', 409)
        job['cancel'] = True
        _save_backup_job(job)
        pid = job.get('pid')
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    return jsonify({'success': True})


def _start_backup_job(op, group, srcs, dest, total):
    with _backup_lock:
        if _backup_job_view(_load_backup_job()).get('state') == 'running':
            return err('A backup or restore is already running', 409)
        job = {'state': 'running', 'op': op, 'group': group, 'srcs': srcs,
               'dest': dest, 'total': total, 'done': _dir_bytes(dest)
               if os.path.isdir(dest) else 0, 'started': time.time(),
               'pid': 0, 'error': None, 'cancel': False, 'finished': None}
        _save_backup_job(job)
        argv = [sys.executable, os.path.join(APP_DIR, 'app.py'), 'llama-backup']
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd=APP_DIR)
        job['pid'] = p.pid
        _save_backup_job(job)
    return jsonify({'success': True, 'total': total, 'dest': dest})


@bp.route('/api/llama/backups/<group>', methods=['POST'])
def llama_backup_create(group):
    st = _backup_status()
    problem = _backup_precondition(st)
    if problem:
        return err(problem, 409)
    local = _local_groups()
    if group not in local:
        return err('No such model on this node: %s' % group, 404)
    dest = _backup_dir(group)
    if dest is None:
        return err('Invalid model name')
    srcs = local[group]
    total = _paths_bytes(srcs)
    have = _dir_bytes(dest) if os.path.isdir(dest) else 0
    need = int(max(0, total - have) * (1 + SPACE_MARGIN))
    if need and st['free_bytes'] and st['free_bytes'] < need:
        return err('Not enough space at %s: needs ~%.1f GB free, has %.1f GB'
                   % (st['base'], need / 1e9, st['free_bytes'] / 1e9), 507)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as ex:
        return err('Cannot create %s: %s' % (dest, ex), 500)
    return _start_backup_job('backup', group, srcs, dest, total)


@bp.route('/api/llama/backups/<group>/restore', methods=['POST'])
def llama_backup_restore(group):
    st = _backup_status()
    if not st['rsync']:
        return err('rsync is not installed on this node.', 409)
    src = _backup_dir(group)
    if src is None or not os.path.isdir(src):
        return err('No backup found for %s' % group, 404)
    dest = _group_dest_dir(group)
    if dest is None:
        return err('Invalid model name')
    total = _dir_bytes(src)
    have = _dir_bytes(dest) if os.path.isdir(dest) else 0
    need = int(max(0, total - have) * (1 + SPACE_MARGIN))
    free = _models_free_bytes()
    if need and free and free < need:
        return err('Not enough space in %s: needs ~%.1f GB free, has %.1f GB'
                   % (LLAMA_MODELS_DIR, need / 1e9, free / 1e9), 507)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as ex:
        return err('Cannot create %s: %s. The models directory must be writable '
                   'by this service (group `models`).' % (dest, ex), 500)
    return _start_backup_job('restore', group, [src], dest, total)


@bp.route('/api/llama/backups/<group>', methods=['DELETE'])
def llama_backup_delete(group):
    d = _backup_dir(group)
    if d is None:
        return err('Invalid model name')
    if not os.path.isdir(d):
        return err('No backup found for %s' % group, 404)
    if _backup_job_view(_load_backup_job()).get('state') == 'running':
        return err('A backup or restore is running; wait for it to finish', 409)
    try:
        shutil.rmtree(d)
    except OSError as ex:
        return err('Could not delete the backup: %s' % ex, 500)
    return jsonify({'success': True})


def cli_llama_backup(argv=None):
    """Detached backup/restore worker: one rsync, progress from the destination
    size. Never raises out — a crash must leave a readable error, not a job
    stuck on `running` with no worker behind it."""
    job = _load_backup_job()
    if job.get('state') != 'running' or not job.get('srcs'):
        return 0
    job['pid'] = os.getpid()
    _save_backup_job(job)

    rsync = _rsync_path()
    if not rsync:
        job.update(state='error', finished=time.time(), error='rsync is not installed')
        _save_backup_job(job)
        return 1

    srcs, dest = job.get('srcs') or [], job['dest']
    # Trailing slashes matter to rsync: a DIRECTORY source needs one so its
    # CONTENTS land in dest rather than a nested copy. Plain files take none.
    # A split model arrives here as several file sources, which is why this
    # takes a list: all its shards must land in the one directory together.
    src_args = [(p + os.sep if os.path.isdir(p) else p) for p in srcs]
    # The manifest describes the BACKUP, not the model, so it must not be
    # restored into the models directory as if it were part of the model.
    excl = ['--exclude', BACKUP_MANIFEST] if job.get('op') == 'restore' else []
    argv_rs = RSYNC_BASE + excl + src_args + [dest + os.sep]
    try:
        proc = subprocess.Popen(argv_rs, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as ex:
        job.update(state='error', finished=time.time(), error=str(ex))
        _save_backup_job(job)
        return 1

    stopping = {'v': False}

    def _sigterm(_s, _f):
        stopping['v'] = True
        try:
            proc.terminate()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    while proc.poll() is None:
        j = _load_backup_job()
        if j.get('group') != job.get('group') or j.get('cancel'):
            stopping['v'] = True
            try:
                proc.terminate()
            except OSError:
                pass
            break
        j['done'] = _dir_bytes(dest)
        j['pid'] = os.getpid()
        _save_backup_job(j)
        time.sleep(2)
    try:
        _out, errout = proc.communicate(timeout=30)
    except Exception:
        errout = b''
    rc = proc.returncode

    j = _load_backup_job()
    if j.get('group') != job.get('group'):
        return 0
    if stopping['v']:
        j.update(state='cancelled', finished=time.time(), cancel=False,
                 done=_dir_bytes(dest),
                 error='Cancelled. Copied data was kept — running it again resumes.')
    elif rc != 0:
        j.update(state='error', finished=time.time(), done=_dir_bytes(dest),
                 error=(errout.decode('utf-8', 'replace').strip()[-300:]
                        or 'rsync exited %d' % rc))
    else:
        done = _dir_bytes(dest)
        if job['op'] == 'backup':
            # The manifest makes a backup self-describing: what it is, how big,
            # and which node it came from.
            try:
                write_json_atomic(os.path.join(dest, BACKUP_MANIFEST),
                                  {'group': job['group'], 'size_bytes': done,
                                   'source_node': socket.gethostname(),
                                   'saved_at': time.time()}, 0o644)
            except OSError:
                pass
        j.update(state='done', finished=time.time(), done=done, error=None)
    _save_backup_job(j)
    return 0 if rc == 0 or stopping['v'] else 1


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
MODULE = {'id': 'llamacpp', 'order': 120, 'label': 'LLama.cpp', 'category': 'AI Tools',
          # Two pages, ONE toggle (the dnsmasq precedent): the model library is
          # llama.cpp's — GGUF-only, LLAMA_MODELS_DIR — so a node with AI Tools
          # off should not show it, and this gets that gating for free on every
          # existing node. registry gives each page data-module=llamacpp.
          'nav': {'cat': 'ai', 'cat_order': 40, 'pages': [
                  {'id': 'llamacpp', 'label': 'LLama.cpp', 'icon': 'flame'},
                  {'id': 'models', 'label': 'Models', 'icon': 'dl', 'order': 121}]},
          'blueprint': bp,
          # The detached download worker re-enters through this name.
          'cli': {'llama-fetch': cli_llama_fetch,
                  'llama-backup': cli_llama_backup},
          # No apt package (pkg=None) and never raises health alerts
          # (alert=False) — llama-server is frequently stopped on purpose /
          # absent on storage hosts.
          'services': {'llamacpp': {'name': 'llama.cpp', 'service': LLAMA_SERVICE,
                                    'pkg': None, 'binary': LLAMA_DEFAULT_BIN,
                                    'alert': False}}}
