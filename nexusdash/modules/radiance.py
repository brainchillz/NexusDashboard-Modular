"""Radiance — the vLLM Radiance inference server (AI Tools > Radiance).

What the fleet calls "Radiance" is `stilldeadcode/vllm-radiance` — a vLLM
fork for RDNA4 cards — run by ONE systemd unit (`vllm-qwen.service` by
default, `DASHBOARD_RADIANCE_UNIT` overrides) whose ExecStart is the repo's
`serve-tpN.sh` → `serve-mxfp4.sh` → `docker run --network=host` in the
foreground. Every boot parameter the launcher understands is an
`Environment=` line on that unit (TP, MAXLEN, MAXSEQS, KV_MEM, SPEC_METHOD,
the checkpoints, …), so "change a boot parameter" means "change the unit's
environment and restart it".

This module does three things and needs no new privilege on a node whose
dashboard runs as root (node7/node6 — the legacy AI units):

- **Control:** start / stop / restart / enable / disable the unit through
  the blanket `systemctl` sudoers grant every node has. The unit declares
  `Conflicts=llama-server.service`, so a start STOPS llama-server — the
  page says so, and an alert fires when both are enabled at boot.
- **Boot parameters:** edited through a typed, range-checked allowlist and
  written to a systemd DROP-IN beside the unit
  (`/etc/systemd/system/<unit>.service.d/50-nexus-dashboard.conf`), never
  into the unit file: a later `Environment=` wins, so the drop-in holds
  only the overrides and "Reset" is `rm` + daemon-reload. The free-text
  `EXTRA` knob (raw vllm args) is deliberately NOT offered (the NOTIFYCMD
  rule: no free text that reaches a root-run command line). A node whose
  dashboard is NOT root writes through the root-owned `<prefix>-radiance`
  helper, which re-validates the drop-in's shape independently.
- **Telemetry:** the server's own `/metrics` (vllm:* gauges: running /
  waiting requests, KV cache %, draft acceptance, token counters) for the
  card, summary block, alerts and history — a local HTTP GET, safe for the
  30 s poll. The page additionally reads `/version`, `/v1/models` and the
  engine's own startup lines from the container log (KV cache size,
  max concurrency, load times — the halogen "budget lines" precedent: the
  module never recomputes what the engine already printed).

Default-off (halogen precedent): dead weight on a node without the unit,
and the page says "not installed" rather than erroring.
"""
import os
import re
import json
import shlex
import time
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request

from ..core.config import APP_DIR, HELPER_PREFIX, write_json_atomic
from ..core.runcmd import run, run_safe, err, _num
from ..core.validators import RE_SERVICE
from .docker import DockerError, docker_raw, _dk_demux_logs, RE_DK_NAME

bp = Blueprint('radiance', __name__)

RADIANCE_UNIT = os.environ.get('DASHBOARD_RADIANCE_UNIT', 'vllm-qwen')
RADIANCE_URL = os.environ.get('DASHBOARD_RADIANCE_URL', '')          # '' = http://127.0.0.1:<PORT>
SYSTEMD_DIR = os.environ.get('DASHBOARD_RADIANCE_SYSTEMD_DIR', '/etc/systemd/system')
DROPIN_NAME = '50-nexus-dashboard.conf'
RADIANCE_HELPER = HELPER_PREFIX + '-radiance'
RADIANCE_PREV_FILE = os.path.join(APP_DIR, 'radiance_prev.json')
CONFLICT_UNIT = 'llama-server'
DEFAULT_PORT = 8080
DEFAULT_NAME = 'vllmmxfp4074'           # serve-mxfp4.sh's NAME default
WARM_START_S = 180                      # ~3 min warm (compile cache hit); the UI says so

SHOW_PROPS = ('Id,LoadState,ActiveState,SubState,UnitFileState,FragmentPath,DropInPaths,'
              'NRestarts,ActiveEnterTimestamp,ActiveEnterTimestampMonotonic,MainPID,Environment,'
              'ExecStart,ExecStop,Conflicts')
UNIT_ACTIONS = ('start', 'stop', 'restart', 'enable', 'disable')

# ─── The knob allowlist ───────────────────────────────────────────────
# (key, group, type, spec, launcher default, help). Values are strings in a
# systemd Environment= line; every regex below excludes `"`, `\`, `$` and
# whitespace (SERVED_NAMES excepted — single spaces between names, rendered
# quoted) so a value can never break out of the drop-in line it is written
# to. Defaults are serve-mxfp4.sh's (README "Configuration"), shown when the
# unit sets nothing.
RE_KNOB_PATH = re.compile(r'^/[A-Za-z0-9._@+/-]{1,255}\Z')
RE_GPUS = re.compile(r'^\d{1,2}(,\d{1,2}){0,7}\Z')
RE_KV_MEM = re.compile(r'^(auto|\d{1,15})\Z')
RE_SERVED_NAMES = re.compile(r'^[A-Za-z0-9._-]{1,64}( [A-Za-z0-9._-]{1,64}){0,15}\Z')
RE_IMAGE = re.compile(r'^[a-z0-9][a-z0-9._/-]{0,200}(:[A-Za-z0-9._-]{1,64})?\Z')
RE_INT = re.compile(r'^\d{1,10}\Z')
RE_FLOAT = re.compile(r'^\d{1,3}(\.\d{1,4})?\Z')
RE_ADV_KEY = re.compile(r'^RADIANCE_[A-Z0-9_]{1,48}\Z')
RE_ADV_VAL = re.compile(r'^[A-Za-z0-9._,:/-]{1,64}\Z')
RE_ENV_KEY = re.compile(r'^[A-Z][A-Z0-9_]{0,63}\Z')
RE_DIRNAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')

KNOBS = [
    ('TP', 'shape', 'int', (1, 8), 'auto',
     'Tensor-parallel size = how many GPUs serve one model. Must divide the model\'s head counts '
     '(1, 2, 4 natively; 3 by dummy-head padding). When the unit starts through a serve-tpN.sh '
     'wrapper (which pins TP), changing this switches the wrapper — the sibling serve-tpN.sh '
     'must exist.'),
    ('GPUS', 'shape', 'gpus', None, 'auto',
     'HIP indices to serve on, comma-separated (e.g. 0,1). Default: every card with enough VRAM.'),
    ('MAXLEN', 'shape', 'int', (1024, 1048576), '262144',
     'Context length per request. The KV reservation and the GDN fallback scale with it.'),
    ('MAXSEQS', 'shape', 'int', (1, 256), '8',
     'Max concurrent sequences. Changes the cudagraph capture set — a KV_MEM byte pin measured '
     'at another value no longer applies.'),
    ('CHUNK', 'shape', 'int', (256, 65536), '8192',
     'Prefill chunk (tokens per prefill step). Changes the prefill memory transient.'),
    ('SPEC_METHOD', 'shape', 'enum', ('dflash', 'mtp'), 'dflash',
     'Speculative drafter: dflash (block-diffusion drafter, needs the DRAFTER checkpoint) or '
     'mtp (the head inside the target, no second checkpoint).'),
    ('SPEC', 'shape', 'int', (1, 16), '7 (dflash) / 4 (mtp)',
     'Speculative depth. 7 wins on code/JSON, 5 on prose or batch throughput.'),
    ('GPU_UTIL', 'shape', 'float', (0.3, 0.99), '0.98',
     'Fraction of VRAM vLLM may claim. 0.75 for perplexity work.'),
    ('KV_MEM', 'shape', 'kvmem', None, 'auto',
     'KV cache size: auto = the measured pin for this hardware + batch shape (kv-profiles.tsv), '
     'falling back to vLLM\'s profiling; 0 = always profile; <bytes> = pin explicitly. A pin is '
     'only valid for the shape it was measured at.'),
    ('SINGLE_GPU_PROFILE', 'shape', 'enum', ('auto', '0', '1'), 'auto (on iff TP=1)',
     'One-card serving profile (fp16 ssm cache, MAXLEN 65536, CHUNK 4096, narrow-state kernels).'),
    ('ASYNC', 'shape', 'enum', ('0', '1'), '0',
     'Async scheduling. vLLM refuses it together with the unpadded drafter batch.'),
    ('MIN_GPU_MIB', 'shape', 'int', (256, 1048576), '8192',
     'VRAM floor for a card to count as usable (skips iGPUs).'),
    ('SNAP', 'paths', 'path', None, '$MODELS/Qwen3.8-27B-MXFP4-mtpfp8',
     'The target checkpoint directory (must live under MODELS — it is the only mount). '
     'serve-mxfp4.sh launchers.'),
    ('MODEL_DIR', 'paths', 'dirname', None, 'Qwen3.8-27B-PARO',
     'PARO launcher (run_paroquant.sh): the checkpoint directory NAME under MODELS.'),
    ('MODE', 'shape', 'enum', ('prod', 'eval'), 'eval',
     'PARO launcher (run_paroquant.sh): prod serving profile or eval.'),
    ('DRAFTER', 'paths', 'path', None, '$MODELS/Qwen3.8-27B-DFlash2-FP8',
     'The dflash drafter checkpoint (ignored under SPEC_METHOD=mtp).'),
    ('MODELS', 'paths', 'path', None, '~/models',
     'Checkpoint directory, bind-mounted at /models.'),
    ('CHAT_TEMPLATE', 'paths', 'path', None, '<repo>/qwen-fixed-v22.3.jinja',
     'Chat template file on the host (mounted by path).'),
    ('CACHE', 'paths', 'path', None, '~/.radiance-cache-w4a8-093 + suffixes',
     'Compile cache. Keyed on the image AND every graph-changing knob — never share one across '
     'configurations; a fresh path costs one cold compile.'),
    ('SERVED_NAMES', 'serving', 'names', None, 'Qwen3.8 Qwen3.6 Qwen3.8-MXFP4',
     'Model ids the OpenAI API answers to, space-separated.'),
    ('PORT', 'serving', 'int', (1024, 65535), '8080',
     'Listen port (host network).'),
    ('NAME', 'serving', 'name', None, DEFAULT_NAME,
     'Container name (what ExecStop stops and the log viewer reads).'),
    ('IMAGE', 'serving', 'image', None, 'stilldeadcode/vllm-radiance:0.9.3',
     'Container image. Moves with CACHE.'),
    ('RUNTIME', 'serving', 'enum', ('docker', 'podman'), 'auto',
     'Container runtime.'),
]
KNOB_INDEX = {k[0]: k for k in KNOBS}
# Changing any of these invalidates a KV_MEM byte pin (kv-profiles.tsv rows
# are per batch shape): the write refuses without confirm.
SHAPE_KEYS = ('TP', 'GPUS', 'MAXLEN', 'MAXSEQS', 'CHUNK', 'SPEC_METHOD', 'SPEC', 'GPU_UTIL',
              'SINGLE_GPU_PROFILE', 'SNAP', 'MODEL_DIR', 'MODE', 'IMAGE')
# Unit environment that is neither a knob nor a RADIANCE_* flag is shown
# read-only (the page must not hide what the unit sets); these are noise.
HIDDEN_ENV = ('HOME', 'PATH', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL', 'INVOCATION_ID', 'JOURNAL_STREAM')
NEVER_OFFERED = ('EXTRA',)       # free-text vllm args — refused outright


def validate_knob(key, value):
    """(normalized value, None) or (None, error). Pure."""
    if not isinstance(key, str) or not isinstance(value, str):
        return None, 'key and value must be strings'
    value = value.strip()
    if key in NEVER_OFFERED:
        return None, '%s is free-text vllm arguments and is not offered here — edit the unit by hand' % key
    spec = KNOB_INDEX.get(key)
    if spec is None:
        if RE_ADV_KEY.match(key):
            if not RE_ADV_VAL.match(value):
                return None, '%s: value must be a short flag/number (letters, digits . _ , : / -)' % key
            return value, None
        return None, 'Unknown parameter %r' % key
    _k, _g, typ, arg, _d, _h = spec
    if not value:
        return None, '%s: empty value (remove the override to fall back to the unit / default)' % key
    if typ == 'int':
        if not RE_INT.match(value) or not (arg[0] <= int(value) <= arg[1]):
            return None, '%s must be a whole number between %d and %d' % (key, arg[0], arg[1])
        return str(int(value)), None
    if typ == 'float':
        if not RE_FLOAT.match(value) or not (arg[0] <= float(value) <= arg[1]):
            return None, '%s must be a number between %s and %s' % (key, arg[0], arg[1])
        return value, None
    if typ == 'enum':
        if value not in arg:
            return None, '%s must be one of: %s' % (key, ', '.join(arg))
        return value, None
    if typ == 'gpus':
        if not RE_GPUS.match(value):
            return None, '%s must be comma-separated GPU indices, e.g. 0,1' % key
        return value, None
    if typ == 'kvmem':
        if not RE_KV_MEM.match(value):
            return None, '%s must be auto, 0, or a byte count' % key
        return value, None
    if typ == 'path':
        if not RE_KNOB_PATH.match(value) or '/../' in value or value.endswith('/..'):
            return None, '%s must be an absolute path (letters, digits . _ @ + - /)' % key
        return value, None
    if typ == 'dirname':
        if not RE_DIRNAME.match(value) or value in ('.', '..'):
            return None, '%s must be a directory name (letters, digits . _ -), not a path' % key
        return value, None
    if typ == 'names':
        if not RE_SERVED_NAMES.match(value):
            return None, '%s: space-separated model names (letters, digits . _ -)' % key
        return value, None
    if typ == 'name':
        if not RE_DK_NAME.match(value):
            return None, '%s must be a valid container name' % key
        return value, None
    if typ == 'image':
        if not RE_IMAGE.match(value):
            return None, '%s must be an image reference like repo/name:tag' % key
        return value, None
    return None, 'Unknown parameter type'


# ─── systemd side ─────────────────────────────────────────────────────

RE_EXEC_PATH = re.compile(r'path=([^\s;]+)')
RE_TP_LAUNCHER = re.compile(r'^serve-tp(\d{1,2})\.sh\Z')


def parse_exec_start(show_value):
    """`systemctl show`'s ExecStart (`{ path=/x/serve-tp1.sh ; argv[]=… }`) →
    the launcher path, or '' . Pure."""
    m = RE_EXEC_PATH.search(show_value or '')
    return m.group(1) if m else ''


RE_STOP_NAME = re.compile(r'argv\[\]=(?:\S+/)?(?:docker|podman) stop(?: -t \d+)? ([A-Za-z0-9][A-Za-z0-9_.-]*)')


def parse_exec_stop_name(show_value):
    """The container ExecStop stops (`docker stop -t 30 vllmparo`) — the
    launcher's NAME when the unit does not set it in Environment. Pure."""
    m = RE_STOP_NAME.search(show_value or '')
    return m.group(1) if m else ''


def launcher_tp(path):
    """The TP a serve-tpN.sh wrapper pins, as a string, else None. Pure."""
    m = RE_TP_LAUNCHER.match(os.path.basename(path or ''))
    return m.group(1) if m else None


def parse_unit_exec(text):
    """The last non-empty ExecStart= of a unit / drop-in file (an empty one
    clears, as systemd does). Pure."""
    exe = ''
    for raw in (text or '').splitlines():
        line = raw.strip()
        if line.startswith('ExecStart='):
            v = line[len('ExecStart='):].strip()
            exe = v.split()[0].lstrip('-@:+!') if v else ''
    return exe


def tp_launcher_path(unit_launcher, tp):
    """The sibling wrapper that pins `tp`, beside the unit's own wrapper."""
    return os.path.join(os.path.dirname(unit_launcher), 'serve-tp%s.sh' % tp)


def _unit():
    u = RADIANCE_UNIT[:-8] if RADIANCE_UNIT.endswith('.service') else RADIANCE_UNIT
    return u if RE_SERVICE.match(u) else 'vllm-qwen'


def _dropin_dir(unit=None):
    return os.path.join(SYSTEMD_DIR, (unit or _unit()) + '.service.d')


def _dropin_path(unit=None):
    return os.path.join(_dropin_dir(unit), DROPIN_NAME)


def parse_show(text):
    """`systemctl show -p …` output → dict. Pure."""
    out = {}
    for line in (text or '').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def parse_env_string(s):
    """systemd's Environment= value (one or more K=V words, double-quoted when
    they hold spaces) → dict, later wins. Pure."""
    env = {}
    try:
        words = shlex.split(s or '')
    except ValueError:
        return env
    for w in words:
        if '=' in w:
            k, v = w.split('=', 1)
            if RE_ENV_KEY.match(k):
                env[k] = v
    return env


def parse_unit_env(text):
    """The Environment= lines of a unit or drop-in file → dict (later line
    wins; an empty `Environment=` clears, as systemd does). Pure."""
    env = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line.startswith('Environment='):
            continue
        rest = line[len('Environment='):].strip()
        if not rest:
            env = {}
            continue
        env.update(parse_env_string(rest))
    return env


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _show(unit):
    out, _e, rc = run(['systemctl', 'show', unit + '.service', '-p', SHOW_PROPS])
    if rc != 0:
        return {}
    return parse_show(out)


def _conflict_state():
    """llama-server's state — it owns the same port and card."""
    s = _show(CONFLICT_UNIT)
    present = bool(s) and s.get('LoadState') == 'loaded'
    return {'unit': CONFLICT_UNIT, 'present': present,
            'active': s.get('ActiveState') == 'active' if present else False,
            'enabled': s.get('UnitFileState') == 'enabled' if present else False}


def _editable():
    return os.geteuid() == 0 or (os.path.isfile(RADIANCE_HELPER) and os.access(RADIANCE_HELPER, os.X_OK))


def render_dropin(overrides, unit_launcher=''):
    """The drop-in text for a {key: value} map (sorted, deterministic). When
    the unit starts through a serve-tpN.sh wrapper, a TP override is an
    ExecStart swap to the sibling wrapper (the wrapper pins TP in the
    environment it execs, so Environment=TP= would be ignored). Pure."""
    lines = ['# Written by the Nexus Dashboard radiance module — boot-parameter overrides.',
             '# Edit on the Radiance page; "Reset" removes this file. The unit file is never touched.',
             '[Service]']
    env = dict(overrides)
    if 'TP' in env and launcher_tp(unit_launcher):
        lines += ['ExecStart=', 'ExecStart=' + tp_launcher_path(unit_launcher, env.pop('TP'))]
    for k in sorted(env):
        v = env[k]
        lines.append('Environment="%s=%s"' % (k, v) if ' ' in v else 'Environment=%s=%s' % (k, v))
    return '\n'.join(lines) + '\n'


def _write_dropin(text):
    """Root: write beside the unit + daemon-reload. Otherwise the helper does
    both (it re-validates the shape). Returns an error string or None."""
    unit = _unit()
    if os.geteuid() == 0:
        d = _dropin_dir(unit)
        try:
            os.makedirs(d, 0o755, exist_ok=True)
            tmp = os.path.join(d, '.' + DROPIN_NAME + '.tmp')
            with open(tmp, 'w') as f:
                f.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, _dropin_path(unit))
        except OSError as e:
            return 'cannot write %s: %s' % (_dropin_path(unit), e)
        _out, e, rc = run(['systemctl', 'daemon-reload'])
        return None if rc == 0 else ('daemon-reload failed: ' + (e or '').strip())
    out, e, rc = run([RADIANCE_HELPER, 'write-dropin', unit], input_data=text)
    return None if rc == 0 else ((e or out or 'helper failed').strip()[-500:])


def _delete_dropin():
    unit = _unit()
    if os.geteuid() == 0:
        p = _dropin_path(unit)
        try:
            if os.path.exists(p):
                os.remove(p)
            try:
                os.rmdir(_dropin_dir(unit))     # only when nothing else lives there
            except OSError:
                pass
        except OSError as e:
            return 'cannot remove %s: %s' % (p, e)
        _out, e, rc = run(['systemctl', 'daemon-reload'])
        return None if rc == 0 else ('daemon-reload failed: ' + (e or '').strip())
    out, e, rc = run([RADIANCE_HELPER, 'reset', unit])
    return None if rc == 0 else ((e or out or 'helper failed').strip()[-500:])


# ─── HTTP side (the vLLM server) ──────────────────────────────────────

def _http_get(base, path, timeout):
    """(text, error) — never raises."""
    if not base:
        return None, 'no URL'
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.read().decode('utf-8', 'replace'), None
    except urllib.error.HTTPError as e:
        return None, 'HTTP %d' % e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(getattr(e, 'reason', e))


RE_METRIC = re.compile(r'^(vllm:[A-Za-z0-9_]+)(?:\{([^}]*)\})?\s+(\S+)')
RE_LABEL_MODEL = re.compile(r'model_name="([^"]*)"')


def parse_metrics(text):
    """Prometheus exposition → ({name: value summed over label sets}, model
    name from the first labelled sample). Histogram buckets are skipped. Pure."""
    sums, model = {}, None
    for line in (text or '').splitlines():
        if not line or line.startswith('#'):
            continue
        m = RE_METRIC.match(line)
        if not m:
            continue
        name, labels, val = m.group(1), m.group(2) or '', m.group(3)
        if name.endswith('_bucket'):
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        sums[name] = sums.get(name, 0.0) + v
        if model is None:
            lm = RE_LABEL_MODEL.search(labels)
            if lm:
                model = lm.group(1)
    return sums, model


def metrics_summary(sums, model=None):
    """The figures the card / summary / history use. None when empty. Pure."""
    if not sums:
        return None
    drafts = sums.get('vllm:spec_decode_num_draft_tokens_total') or 0
    accepted = sums.get('vllm:spec_decode_num_accepted_tokens_total') or 0
    pq = sums.get('vllm:prefix_cache_queries_total') or 0
    ph = sums.get('vllm:prefix_cache_hits_total') or 0
    kv = sums.get('vllm:kv_cache_usage_perc', 0.0)
    return {
        'model': model,
        'requests_running': int(sums.get('vllm:num_requests_running', 0)),
        'requests_waiting': int(sums.get('vllm:num_requests_waiting', 0)),
        'kv_cache_pct': round(kv * 100, 1),
        'generation_tokens_total': int(sums.get('vllm:generation_tokens_total', 0)),
        'prompt_tokens_total': int(sums.get('vllm:prompt_tokens_total', 0)),
        'prompt_tokens_cached_total': int(sums.get('vllm:prompt_tokens_cached_total', 0)),
        'requests_total': int(sums.get('vllm:request_success_total', 0)),
        'preemptions_total': int(sums.get('vllm:num_preemptions_total', 0)),
        'draft_tokens_total': int(drafts),
        'draft_accept_ratio': round(accepted / drafts, 3) if drafts else None,
        'prefix_cache_hit_ratio': round(ph / pq, 3) if pq else None,
    }


def parse_models(text):
    """/v1/models → [{id, root, max_model_len}]. Pure."""
    try:
        d = json.loads(text or '')
    except ValueError:
        return []
    out = []
    for m in (d.get('data') or []) if isinstance(d, dict) else []:
        if isinstance(m, dict) and m.get('id'):
            out.append({'id': str(m['id']), 'root': m.get('root'), 'max_model_len': m.get('max_model_len')})
    return out


# ─── Container log: the engine's own startup figures ──────────────────

RE_LOG_PREFIX = re.compile(r'^\((?:APIServer|EngineCore)[^)]*\)\s+(?:INFO|WARNING|ERROR)(?:\s+\d\d-\d\d \d\d:\d\d:\d\d)?\s+(?:\[[^\]]+\]\s+)?')
RE_STARTUP_MARK = re.compile(r'\] non-default args:')
RE_STARTUP_LINE = re.compile(
    r'\] (Initializing a V1 LLM engine|Loading weights took|Model loading took|Initial free memory|'
    r'GPU KV cache size|Maximum concurrency for|Graph capturing finished|init engine|'
    r'Starting vLLM server|torch\.compile took)|Application startup complete|Traceback \(most recent|ERROR ')
RE_KV_TOKENS = re.compile(r'GPU KV cache size: ([\d,]+) tokens')
RE_CONCURRENCY = re.compile(r'Maximum concurrency for ([\d,]+) tokens per request: ([\d.]+)x')
RE_WEIGHTS = re.compile(r'Model loading took ([\d.]+) GiB and ([\d.]+) seconds')
RE_INIT = re.compile(r'init engine .* took ([\d.]+) s')


def startup_summary(log_text):
    """The engine's own figures from the LAST start in the container log
    (anchored on the APIServer's `non-default args` line), verbatim lines
    with the pid/timestamp prefix stripped. Pure."""
    lines = (log_text or '').splitlines()
    starts = [i for i, l in enumerate(lines) if RE_STARTUP_MARK.search(l)]
    tail = lines[starts[-1]:] if starts else lines
    picked = [RE_LOG_PREFIX.sub('', l.rstrip()) for l in tail if RE_STARTUP_LINE.search(l)]
    picked = [p for p in picked if p][:40]
    text = '\n'.join(picked)
    out = {'lines': picked, 'ready': 'Application startup complete' in text,
           'kv_tokens': None, 'max_concurrency': None, 'concurrency_at': None,
           'weights_gib': None, 'load_s': None, 'init_s': None,
           'errors': sum(1 for p in picked if p.startswith('Traceback') or p.startswith('ERROR'))}
    m = RE_KV_TOKENS.search(text)
    if m:
        out['kv_tokens'] = int(m.group(1).replace(',', ''))
    m = RE_CONCURRENCY.search(text)
    if m:
        out['concurrency_at'] = int(m.group(1).replace(',', ''))
        out['max_concurrency'] = float(m.group(2))
    m = RE_WEIGHTS.search(text)
    if m:
        out['weights_gib'], out['load_s'] = float(m.group(1)), float(m.group(2))
    m = RE_INIT.search(text)
    if m:
        out['init_s'] = float(m.group(1))
    return out


def _container_logs(name, tail):
    if not RE_DK_NAME.match(name or ''):
        return ''
    try:
        status, raw = docker_raw('GET', '/containers/%s/logs?stdout=1&stderr=1&tail=%d' % (name, tail), timeout=20)
    except DockerError:
        return ''
    if status >= 400:
        return ''
    return _dk_demux_logs(raw).decode('utf-8', 'replace')


# ─── Status assembly ──────────────────────────────────────────────────

def _knob_rows(env, base, overrides, st):
    rows = []
    for key, group, typ, arg, default, help_ in KNOBS:
        value, unit_value = env.get(key), base.get(key)
        source = 'override' if key in overrides else ('unit' if key in base else 'default')
        if key == 'TP' and 'TP' not in env and st.get('launcher_tp'):
            value = st['launcher_tp']                 # pinned by the serve-tpN.sh wrapper
            unit_value = st.get('unit_launcher_tp')
            source = 'override' if 'TP' in overrides else 'launcher'
        elif key == 'TP' and 'TP' not in env and st.get('tp'):
            value = st['tp']                          # implied by the GPUS list
            source = 'gpus'
        rows.append({'key': key, 'group': group, 'type': typ,
                     'range': list(arg) if typ in ('int', 'float') else None,
                     'choices': list(arg) if typ == 'enum' else None,
                     'value': value, 'unit_value': unit_value,
                     'override': overrides.get(key), 'source': source,
                     'default': default, 'help': help_})
    for key in sorted(set(env) | set(overrides)):
        if RE_ADV_KEY.match(key):
            rows.append({'key': key, 'group': 'advanced', 'type': 'flag', 'range': None, 'choices': None,
                         'value': env.get(key), 'unit_value': base.get(key), 'override': overrides.get(key),
                         'source': 'override' if key in overrides else 'unit',
                         'default': '', 'help': 'Kernel / engine flag read by serve-mxfp4.sh (see DOCKERHUB.md).'})
    for key in sorted(env):
        if key in KNOB_INDEX or RE_ADV_KEY.match(key) or key in HIDDEN_ENV:
            continue
        rows.append({'key': key, 'group': 'other', 'type': 'readonly', 'range': None, 'choices': None,
                     'value': env.get(key), 'unit_value': base.get(key), 'override': None, 'source': 'unit',
                     'default': '', 'help': 'Set by the unit; not a parameter this page edits.'})
    return rows


def _models_available(env):
    """Checkpoint directories under MODELS (for the SNAP/DRAFTER pickers).
    Empty when unreadable — the field stays free-form."""
    d = env.get('MODELS') or os.path.join(env.get('HOME') or '', 'models')
    try:
        return sorted(os.path.join(d, n) for n in os.listdir(d)
                      if os.path.isdir(os.path.join(d, n)) and not n.startswith('.'))
    except OSError:
        return []


def _base_status(full=False):
    unit = _unit()
    st = {'unit': unit, 'installed': False, 'active': 'unknown', 'sub': '', 'enabled': None,
          'running': False, 'nrestarts': 0, 'since': '', 'pid': 0,
          'fragment': '', 'dropin': _dropin_path(unit), 'dropin_present': False,
          'editable': _editable(), 'helper': RADIANCE_HELPER if os.geteuid() != 0 else None,
          'env': {}, 'unit_env': {}, 'overrides': {}, 'port': DEFAULT_PORT,
          'container': DEFAULT_NAME, 'url': RADIANCE_URL or None, 'metrics': None}
    s = _show(unit)
    if not s or s.get('LoadState') != 'loaded' or not s.get('FragmentPath'):
        st['error'] = 'unit %s.service not found on this node' % unit
        return st
    st.update({'installed': True, 'active': s.get('ActiveState', ''), 'sub': s.get('SubState', ''),
               'enabled': s.get('UnitFileState') == 'enabled', 'running': s.get('ActiveState') == 'active',
               'nrestarts': int(_num(s.get('NRestarts')) or 0), 'since': s.get('ActiveEnterTimestamp', ''),
               'pid': int(_num(s.get('MainPID')) or 0), 'fragment': s.get('FragmentPath', ''),
               'exec_start': s.get('ExecStart', ''), 'conflicts': s.get('Conflicts', '')})
    env = parse_env_string(s.get('Environment', ''))       # effective: unit + drop-ins
    st['env'] = env
    st['launcher'] = parse_exec_start(s.get('ExecStart', ''))     # effective ExecStart
    st['launcher_tp'] = launcher_tp(st['launcher'])
    frag = _read_text(st['fragment'])
    st['unit_env'] = parse_unit_env(frag) if frag is not None else {}
    st['unit_launcher'] = parse_unit_exec(frag) if frag is not None else st['launcher']
    st['unit_launcher_tp'] = launcher_tp(st['unit_launcher'])
    dtext = _read_text(st['dropin'])
    if dtext is not None:
        st['dropin_present'] = True
        st['overrides'] = parse_unit_env(dtext)
        swapped = launcher_tp(parse_unit_exec(dtext))
        if swapped:
            st['overrides']['TP'] = swapped
    st['other_dropins'] = [p for p in (s.get('DropInPaths') or '').split() if p and p != st['dropin']]
    st['port'] = int(_num(env.get('PORT')) or DEFAULT_PORT)
    st['container'] = env.get('NAME') or parse_exec_stop_name(s.get('ExecStop', '')) or DEFAULT_NAME
    st['url'] = RADIANCE_URL or 'http://127.0.0.1:%d' % st['port']
    st['checkpoint'] = (os.path.basename((env.get('SNAP') or '').rstrip('/')) or env.get('MODEL_DIR') or None)
    st['image'] = env.get('IMAGE') or KNOB_INDEX['IMAGE'][4]
    gpus = [g for g in (env.get('GPUS') or '').split(',') if g.strip()]
    st['tp'] = env.get('TP') or st['launcher_tp'] or (str(len(gpus)) if gpus else None)
    st['models'] = []
    if st['running']:
        text, e = _http_get(st['url'], '/metrics', 3)
        sums, model = parse_metrics(text) if text else ({}, None)
        st['metrics'] = metrics_summary(sums, model)
        st['metrics_error'] = e
        # /v1/models is local and instant; its `root` names the loaded
        # checkpoint when the unit's environment does not (PARO launcher).
        text, _ = _http_get(st['url'], '/v1/models', 3)
        st['models'] = parse_models(text) if text else []
        if not st['checkpoint'] and st['models'] and st['models'][0].get('root'):
            st['checkpoint'] = os.path.basename(str(st['models'][0]['root']).rstrip('/')) or None
    if full:
        st['knobs'] = _knob_rows(env, st['unit_env'], st['overrides'], st)
        st['dropin_text'] = dtext
        st['models_available'] = _models_available(env)
        st['conflict'] = _conflict_state()
        st['warm_start_s'] = WARM_START_S
        if st['running']:
            text, _ = _http_get(st['url'], '/version', 3)
            try:
                st['version'] = (json.loads(text) or {}).get('version') if text else None
            except ValueError:
                st['version'] = None
            st['startup'] = startup_summary(_container_logs(st['container'], 3000))
    return st


# ─── Routes ───────────────────────────────────────────────────────────

@bp.route('/api/radiance')
def radiance_status():
    return jsonify(_base_status(full=True))


@bp.route('/api/radiance/action', methods=['POST'])
def radiance_action():
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    if action not in UNIT_ACTIONS:
        return err('Unknown action')
    unit = _unit()
    s = _show(unit)
    if not s or s.get('LoadState') != 'loaded':
        return err('unit %s.service not found on this node' % unit, 404)
    conflict = _conflict_state() if action in ('start', 'restart') else None
    res = dict(run_safe(['systemctl', action, unit + '.service']))
    res['action'] = action
    if conflict and conflict['active'] and res.get('success'):
        res['note'] = '%s was stopped (Conflicts=) — the card and the port are now the vLLM server\'s' % CONFLICT_UNIT
    if action in ('start', 'restart') and res.get('success'):
        res['warm_start_s'] = WARM_START_S
    return jsonify(res)


def _apply_overrides(overrides, restart, unit_launcher=''):
    e = _write_dropin(render_dropin(overrides, unit_launcher)) if overrides else _delete_dropin()
    if e:
        return None, e
    out = {'success': True, 'overrides': overrides, 'dropin': _dropin_path(),
           'dropin_present': bool(overrides), 'restarted': False}
    if restart:
        r = run_safe(['systemctl', 'restart', _unit() + '.service'])
        out['restarted'] = bool(r.get('success'))
        if not r.get('success'):
            out['restart_error'] = r.get('error') or r.get('stderr') or 'restart failed'
        out['warm_start_s'] = WARM_START_S
    return out, None


@bp.route('/api/radiance/params', methods=['POST'])
def radiance_params():
    """Replace the drop-in with exactly {overrides}. A key whose value equals
    the unit file's own setting is dropped (no override needed); an empty
    result removes the drop-in. Shape changes against a KV_MEM byte pin need
    confirm:true."""
    data = request.get_json(silent=True) or {}
    raw = data.get('overrides')
    if not isinstance(raw, dict):
        return err('overrides must be an object of {PARAM: value}')
    if len(raw) > 64:
        return err('too many parameters')
    st = _base_status()
    if not st['installed']:
        return err(st.get('error') or 'unit not found', 404)
    if not st['editable']:
        return err('Boot parameters are read-only on this node: the dashboard is not root and the '
                   '%s helper is not installed (run the installer with --helpers-only)' % RADIANCE_HELPER)
    clean = {}
    for k, v in raw.items():
        if v is None or v == '':
            continue                                   # = remove this override
        nv, e = validate_knob(k, v if isinstance(v, str) else str(v))
        if e:
            return err(e)
        if st['unit_env'].get(k) == nv:
            continue                                   # same as the unit file — no override
        if k == 'TP' and st['unit_launcher_tp']:
            if nv == st['unit_launcher_tp']:
                continue                               # the unit's own wrapper already pins it
            if not os.path.isfile(tp_launcher_path(st['unit_launcher'], nv)):
                return err('TP=%s is not available here: the unit starts through %s, which pins TP, and '
                           'there is no %s beside it' % (nv, os.path.basename(st['unit_launcher']),
                                                         'serve-tp%s.sh' % nv))
        clean[k] = nv
    if clean.get('SPEC_METHOD', st['env'].get('SPEC_METHOD', 'dflash')) == 'dflash':
        snap = clean.get('SNAP') or st['env'].get('SNAP')
        drafter = clean.get('DRAFTER') or st['env'].get('DRAFTER')
        if snap and drafter and snap == drafter:
            return err('DRAFTER must be a different checkpoint from SNAP under SPEC_METHOD=dflash')
    # A KV_MEM byte pin is per batch shape — changing the shape under a pin
    # is the OOM-several-minutes-into-startup failure kv-profiles.tsv warns about.
    current = dict(st['env'])
    current.setdefault('TP', st['launcher_tp'])
    after = dict(st['unit_env'])
    after.setdefault('TP', st['unit_launcher_tp'])
    after.update(clean)
    kv_after = after.get('KV_MEM', 'auto')
    shape_changed = [k for k in SHAPE_KEYS if after.get(k) != current.get(k)]
    if shape_changed and kv_after.isdigit() and kv_after != '0' and not data.get('confirm'):
        return jsonify({'success': False, 'needs_confirm': True,
                        'warning': 'KV_MEM is pinned to %s bytes, a value measured for the CURRENT shape; '
                                   'changing %s under that pin can OOM minutes into startup. Set KV_MEM to '
                                   'auto (or 0 to profile) — or confirm to keep the pin anyway.'
                                   % (kv_after, ', '.join(shape_changed))})
    out, e = _apply_overrides(clean, bool(data.get('restart')), st['unit_launcher'])
    if e:
        return err(e, 500)
    return jsonify(out)


@bp.route('/api/radiance/params/reset', methods=['POST'])
def radiance_params_reset():
    data = request.get_json(silent=True) or {}
    st = _base_status()
    if not st['installed']:
        return err(st.get('error') or 'unit not found', 404)
    if not st['editable']:
        return err('Boot parameters are read-only on this node (see the page)')
    out, e = _apply_overrides({}, bool(data.get('restart')))
    if e:
        return err(e, 500)
    return jsonify(out)


@bp.route('/api/radiance/logs')
def radiance_logs():
    source = request.args.get('source', 'container')
    tail = max(1, min(int(_num(request.args.get('tail')) or 200), 5000))
    st = _base_status()
    if not st['installed']:
        return err(st.get('error') or 'unit not found', 404)
    if source == 'journal':
        out, e, rc = run(['journalctl', '-u', st['unit'] + '.service', '--no-pager', '-n', str(tail),
                          '--output=short-iso'])
        return jsonify({'source': 'journal', 'name': st['unit'] + '.service',
                        'logs': out if rc == 0 else (e or 'journalctl failed')})
    if source == 'container':
        return jsonify({'source': 'container', 'name': st['container'],
                        'logs': _container_logs(st['container'], tail) or '(no container log — not running, or the docker socket is not readable)'})
    return err('source must be journal or container')


# ─── Hooks (summary / alerts / history / metrics) ─────────────────────

def _radiance_summary():
    """Additive /api/summary block `radiance`: unit state + one /metrics
    scrape. None (block absent) when the unit is not on this node. `model`
    (the loaded checkpoint) and `state` are what a fleet console needs to
    show the running LLM; `served_name` is the API alias."""
    st = _base_status()
    if not st['installed']:
        return None
    m = st.get('metrics') or {}
    return {
        'installed': True, 'unit': st['unit'], 'state': st['active'], 'sub': st['sub'],
        'running': st['running'], 'enabled': st['enabled'], 'nrestarts': st['nrestarts'],
        # `model` is the CHECKPOINT actually loaded (what a fleet console should
        # name); the metrics label is only the first served alias (SERVED_NAMES).
        'model': st.get('checkpoint') or m.get('model'), 'checkpoint': st.get('checkpoint'),
        'served_name': m.get('model'),
        'image': st.get('image'), 'tp': st.get('tp'), 'port': st['port'],
        'ready': bool(m), 'has_overrides': bool(st['overrides']),
        'requests_running': m.get('requests_running'), 'requests_waiting': m.get('requests_waiting'),
        'kv_cache_pct': m.get('kv_cache_pct'), 'draft_accept_ratio': m.get('draft_accept_ratio'),
        'generation_tokens_total': m.get('generation_tokens_total'), 'requests_total': m.get('requests_total'),
    }


def _radiance_alerts():
    st = _base_status()
    if not st['installed']:
        return []
    out = []
    unit = st['unit']
    if st['enabled'] and not st['running']:
        out.append({'key': 'radiance_down',
                    'message': 'vLLM Radiance (%s) is enabled at boot but %s' % (unit, st['active'] or 'not running')})
    if st['nrestarts'] > 0:
        out.append({'key': 'radiance_restarting',
                    'message': 'vLLM Radiance (%s) has been restarted by systemd %d time(s) since boot — check its log'
                               % (unit, st['nrestarts'])})
    m = st.get('metrics') or {}
    if (m.get('requests_waiting') or 0) > 0:
        out.append({'key': 'radiance_queue',
                    'message': 'vLLM Radiance has %d request(s) waiting behind %d running — MAXSEQS is full'
                               % (m['requests_waiting'], m.get('requests_running') or 0)})
    c = _conflict_state()
    if c['present'] and c['enabled'] and st['enabled']:
        out.append({'key': 'radiance_conflict',
                    'message': 'Both %s and %s are enabled at boot; they conflict (same port and GPU) — '
                               'whichever starts last stops the other' % (unit, CONFLICT_UNIT)})
    return out


RADIANCE_HISTORY_METRICS = {'radiance_running', 'radiance_waiting', 'radiance_kv_pct',
                            'radiance_accept', 'radiance_tokens'}


def _radiance_history():
    """One /metrics scrape per tick, label = the unit. `radiance_tokens` is
    the generation-token DELTA since the previous tick (the counter resets
    on restart → a negative delta is reported as 0)."""
    st = _base_status()
    m = st.get('metrics')
    if not m:
        return []
    prev = {}
    try:
        with open(RADIANCE_PREV_FILE) as f:
            prev = json.load(f) or {}
    except (OSError, ValueError):
        prev = {}
    total = m['generation_tokens_total']
    delta = None
    if isinstance(prev.get('generation_tokens_total'), (int, float)):
        delta = max(0, total - prev['generation_tokens_total'])
    try:
        write_json_atomic(RADIANCE_PREV_FILE, {'generation_tokens_total': total, 'ts': time.time()})
    except OSError:
        pass
    label = st['unit']
    return [('radiance_running', label, m['requests_running']),
            ('radiance_waiting', label, m['requests_waiting']),
            ('radiance_kv_pct', label, m['kv_cache_pct']),
            ('radiance_accept', label, None if m['draft_accept_ratio'] is None else round(m['draft_accept_ratio'] * 100, 1)),
            ('radiance_tokens', label, delta)]


def _radiance_metrics():
    st = _base_status()
    if not st['installed']:
        return []
    lines = ['# HELP storagedash_radiance_up vLLM Radiance unit active (1) or not (0)',
             '# TYPE storagedash_radiance_up gauge',
             'storagedash_radiance_up %d' % (1 if st['running'] else 0)]
    m = st.get('metrics')
    if not m:
        return lines
    gauges = [('requests_running', 'Requests being generated', m['requests_running']),
              ('requests_waiting', 'Requests waiting for a slot', m['requests_waiting']),
              ('kv_cache_usage_pct', 'KV cache in use, percent', m['kv_cache_pct']),
              ('generation_tokens_total', 'Tokens generated (counter)', m['generation_tokens_total']),
              ('requests_total', 'Requests completed (counter)', m['requests_total'])]
    if m['draft_accept_ratio'] is not None:
        gauges.append(('draft_accept_ratio', 'Speculative draft tokens accepted / drafted', m['draft_accept_ratio']))
    for name, help_, val in gauges:
        n = 'storagedash_radiance_' + name
        lines += ['# HELP %s %s' % (n, help_), '# TYPE %s gauge' % n, '%s %s' % (n, val)]
    return lines


MODULE = {'id': 'radiance', 'order': 123, 'label': 'Radiance', 'category': 'AI Tools',
          'default_enabled': False,
          'nav': {'cat': 'ai', 'cat_order': 40, 'pages': [
                  {'id': 'radiance', 'label': 'Radiance', 'icon': 'flame'}]},
          'blueprint': bp,
          'summary': _radiance_summary,
          'alerts': _radiance_alerts,
          'history': _radiance_history,
          'history_metrics': RADIANCE_HISTORY_METRICS,
          'metrics': _radiance_metrics}
