"""Halogen — the halogen-flash-server inference stack (AI Tools > Halogen).

A first-class page + status card for the docker compose stack that node4 and
framework-class boxes run instead of llama-server: ONE image, TWO containers
(`engine` holds the GPU and the weights, `api` is the OpenAI front-end).
Everything here talks to two things and needs NO root:

- the Docker Engine API over the socket (`docker.docker_request`; the
  service user is in the `docker` group on every halogen node) — discovery
  by compose label, container state/health, the restart policy, logs; and
- the api container's HTTP (stdlib urllib) at the published port, for
  `/health` (pings the engine, ~1 s — page load only), `/metrics` (cheap,
  safe for the 30 s poll) and `/cache`.

Start/stop/restart go through `docker compose` exactly as the compose
module does (argv list, `no_sudo`, a long timeout — a cold `up` pins ~68 GiB
of weights). "Enabled at boot" is the containers' restart policy: there is no
systemd unit by design (docker restarts `unless-stopped` containers at boot
and the api retries until the engine is healthy), so the toggle is a
`POST /containers/<id>/update` — no recreate, no model reload.

The stack directory comes from the compose labels, never hard-coded, and is
REMEMBERED in `halogen.json` once seen: after `down` the containers (and
their labels) are gone, and `up` has to know where the compose file lives.
`DASHBOARD_HALOGEN_DIR` / `DASHBOARD_HALOGEN_URL` / `DASHBOARD_HALOGEN_PROJECT`
override discovery. Default-off (dnsmasq precedent): dead weight on a node
without the stack, and the page says "not deployed" rather than erroring.

Deliberately NOT here yet (backlog): the env tunables editor with the KV-fit
pre-flight, image upgrade, and checkpoint backup to the NFS mount.
"""
import os
import re
import json
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request

from ..core.config import APP_DIR, write_json_atomic
from ..core.runcmd import run, err, _num
from .docker import DockerError, docker_request, docker_raw, _dk_demux_logs, RE_DK_NAME
from .docker_compose import _compose_args, RE_COMPOSE_PROJECT
from . import docker_registry as reg
import time

bp = Blueprint('halogen', __name__)

HALOGEN_PROJECT = os.environ.get('DASHBOARD_HALOGEN_PROJECT', 'halogen')
HALOGEN_URL = os.environ.get('DASHBOARD_HALOGEN_URL', '')          # '' = discover
HALOGEN_DIR = os.environ.get('DASHBOARD_HALOGEN_DIR', '')          # '' = discover
HALOGEN_STATE_FILE = os.environ.get('DASHBOARD_HALOGEN_STATE_FILE',
                                    os.path.join(APP_DIR, 'halogen.json'))
API_PORT = '8731/tcp'            # the api service's container port (compose file)
COMPOSE_FILE_NAMES = ('docker-compose.yml', 'docker-compose.yaml', 'compose.yaml', 'compose.yml')

# Stack-level actions → (compose subcommand, timeout). `down` never takes -v.
# A cold `up` loads ~68 GiB of weights and can take minutes from a cold page
# cache; `restart-api` is the cheap one (no model reload).
HALOGEN_ACTIONS = {
    'start':       (['start'], 300),
    'stop':        (['stop'], 300),
    'restart':     (['restart'], 600),
    'restart-api': (['restart', 'api'], 120),
    'up':          (['up', '-d'], 900),
    'down':        (['down'], 300),
}
HALOGEN_SERVICES = ('engine', 'api')
BOOT_POLICY = {True: 'unless-stopped', False: 'no'}

# Engine-log lines that carry the engine's OWN memory arithmetic. Surfaced
# verbatim on the page — the module never recomputes the budget itself.
RE_BUDGET_LINE = re.compile(r'^(halogen: (KV budget|GTT in use before|WARNING)|kv pool:|startup \[.*\] memory:|slots: |flash_serve: listening)')
RE_STARTUP_MARK = re.compile(r'^halogen: halogen-flash-server \S+, mode engine')
RE_LOG_TS = re.compile(r'^\d{4}-\d\d-\d\dT[0-9:.]+Z ')


# ─── State (remembered stack dir) ─────────────────────────────────────

def _load_state():
    try:
        with open(HALOGEN_STATE_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_stack(working_dir, config_files):
    st = _load_state()
    if st.get('working_dir') == working_dir and st.get('config_files') == config_files:
        return
    write_json_atomic(HALOGEN_STATE_FILE, {'working_dir': working_dir, 'config_files': config_files})


def _stack_dir():
    """Where the compose file lives: env override, else what discovery last
    saw. None when nothing is known (a node that never ran the stack)."""
    if HALOGEN_DIR:
        return HALOGEN_DIR
    return _load_state().get('working_dir') or None


def _stack_config_files(working_dir):
    st = _load_state()
    if st.get('working_dir') == working_dir and st.get('config_files'):
        return list(st['config_files'])
    for n in COMPOSE_FILE_NAMES:
        p = os.path.join(working_dir, n)
        if os.path.isfile(p):
            return [p]
    return []


# ─── Docker side ──────────────────────────────────────────────────────

def _container_summary(c):
    """Flatten one /containers/<id>/json document."""
    st = c.get('State') or {}
    cfg = c.get('Config') or {}
    labels = cfg.get('Labels') or {}
    hc = c.get('HostConfig') or {}
    ports = (c.get('NetworkSettings') or {}).get('Ports') or {}
    published = {}
    for cport, binds in ports.items():
        for b in binds or []:
            if b.get('HostPort'):
                published[cport] = int(b['HostPort'])
                break
    image = cfg.get('Image') or ''
    return {
        'id': (c.get('Id') or '')[:12],
        'name': (c.get('Name') or '').lstrip('/'),
        'service': labels.get('com.docker.compose.service', ''),
        'image': image,
        'tag': image.rsplit(':', 1)[1] if ':' in image.rsplit('/', 1)[-1] else '',
        'state': st.get('Status', ''),                  # running / exited / created …
        'running': bool(st.get('Running')),
        'health': (st.get('Health') or {}).get('Status', ''),   # healthy / unhealthy / starting / ''
        'started_at': st.get('StartedAt', ''),
        'restart_policy': (hc.get('RestartPolicy') or {}).get('Name', ''),
        'published': published,
        'working_dir': labels.get('com.docker.compose.project.working_dir', ''),
        'config_files': [f for f in labels.get('com.docker.compose.project.config_files', '').split(',') if f],
    }


def _find_containers():
    """{service: summary} for every container of the compose project, or
    raises DockerError when the daemon is unreachable."""
    flt = json.dumps({'label': ['com.docker.compose.project=' + HALOGEN_PROJECT]})
    rows = docker_request('GET', '/containers/json?all=1&filters=' + urllib.request.quote(flt), timeout=10)
    out = {}
    for row in rows or []:
        cid = row.get('Id') or ''
        if not RE_DK_NAME.match(cid):
            continue
        c = _container_summary(docker_request('GET', '/containers/%s/json' % cid, timeout=10))
        if c['service'] in HALOGEN_SERVICES:
            out[c['service']] = c
    return out


def _api_url(containers):
    if HALOGEN_URL:
        return HALOGEN_URL
    api = containers.get('api') or {}
    port = (api.get('published') or {}).get(API_PORT)
    return 'http://127.0.0.1:%d' % port if port else None


# ─── HTTP side (the api container) ────────────────────────────────────

def _http_get(base, path, timeout):
    """(text, error) — never raises."""
    if not base:
        return None, 'api port not published'
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.read().decode('utf-8', 'replace'), None
    except urllib.error.HTTPError as e:
        return None, 'HTTP %d' % e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(getattr(e, 'reason', e))


def _parse_metrics(text):
    """Prometheus exposition → {name: float}. Pure."""
    out = {}
    for line in (text or '').splitlines():
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def _metrics_summary(m):
    """The handful of figures the card, summary and history use, from the
    parsed scrape. Names are llama.cpp's on the llamacpp: side (the server
    keeps them), halogen's on the halogen: side."""
    if not m:
        return None
    drafts = m.get('halogen:draft_tokens_total') or 0
    accepted = m.get('halogen:draft_tokens_accepted_total') or 0
    return {
        'tps': m.get('llamacpp:predicted_tokens_seconds', 0.0),
        'prompt_tps': m.get('llamacpp:prompt_tokens_seconds', 0.0),
        'requests_processing': int(m.get('llamacpp:requests_processing', 0)),
        'requests_deferred': int(m.get('llamacpp:requests_deferred', 0)),
        'kv_cache_usage_ratio': m.get('llamacpp:kv_cache_usage_ratio', 0.0),
        'kv_cache_tokens': int(m.get('llamacpp:kv_cache_tokens', 0)),
        'requests_total': int(m.get('halogen:requests_total', 0)),
        'tokens_predicted_total': int(m.get('llamacpp:tokens_predicted_total', 0)),
        'prompt_tokens_total': int(m.get('llamacpp:prompt_tokens_total', 0)),
        'prompt_tokens_cached_total': int(m.get('halogen:prompt_tokens_cached_total', 0)),
        'draft_tokens_total': int(drafts),
        'draft_accept_ratio': round(accepted / drafts, 3) if drafts else None,
        'structured_requests_total': int(m.get('halogen:structured_requests_total', 0)),
    }


def _health_summary(h):
    """The page-relevant subset of /health (it is a 5 KB capability doc)."""
    if not isinstance(h, dict):
        return None
    ver = h.get('version') or {}
    return {
        'status': h.get('status'),
        'model': h.get('model'),
        'api_version': ver.get('api'),
        'engine_version': ver.get('engine'),
        'version_match': ver.get('match'),
        'engine_responds': (h.get('engine') or {}).get('responds'),
        'probe_s': (h.get('engine') or {}).get('probe_s'),
        'context': h.get('context'),
        'slots': h.get('slots'),
        'slot_ctx': h.get('slot_ctx'),
        'kv_pool_positions': h.get('kv_pool_positions'),
        'rope_scaling': h.get('rope_scaling'),
        'busy': h.get('busy'),
        'in_flight': h.get('in_flight'),
        'queued': h.get('queued'),
        'busy_for_s': h.get('busy_for_s'),
        'vision': bool((h.get('vision') or {}).get('enabled')),
        'checkpoint_format': h.get('checkpoint_format'),
        'drafter_default': h.get('drafter_default'),
        'prompt_cache': bool((h.get('prompt_cache') or {}).get('enabled')),
        'composable_context': bool((h.get('composable_context') or {}).get('enabled')),
        'max_tokens_default': h.get('max_tokens_default'),
        'max_tokens_cap': h.get('max_tokens_cap'),
        'reasoning_effort_default': h.get('reasoning_effort_default'),
        'reasoning_effort_values': h.get('reasoning_effort_values') or [],
    }


def _budget_lines(log_text):
    """The engine's own memory arithmetic from its startup log — the
    'KV budget' pre-flight, the kv pool derivation, the measured figures once
    loaded, and the listen line. Verbatim, timestamps stripped. Pure."""
    lines = [RE_LOG_TS.sub('', l.rstrip()) for l in (log_text or '').splitlines()]
    # Anchor on the LAST start ("halogen: halogen-flash-server 0.13.0, mode
    # engine") so a container that restarted shows THIS run's figures, not
    # a mix of every run in the log. No marker (old image) → the last 12.
    starts = [i for i, l in enumerate(lines) if RE_STARTUP_MARK.match(l)]
    if starts:
        head = lines[starts[-1]]
        return [head] + [l for l in lines[starts[-1] + 1:] if RE_BUDGET_LINE.match(l)][:14]
    return [l for l in lines if RE_BUDGET_LINE.match(l)][-12:]


def _container_logs(cid, tail):
    status, raw = docker_raw('GET', '/containers/%s/logs?stdout=1&stderr=1&tail=%d' % (cid, tail), timeout=20)
    if status >= 400:
        return ''
    return _dk_demux_logs(raw).decode('utf-8', 'replace')


_LATEST = {}          # image repo -> {'ts', 'tag', 'error'}; page-load only, 1 h


def _latest_tag(image):
    """Highest X.Y.Z tag at the registry for this image's repo (cached 1 h)."""
    if not image or '@' in image:
        return None, None
    host, repo, _tag, _d = reg.parse_ref(image)
    key = host + '/' + repo
    hit = _LATEST.get(key)
    if hit and time.time() - hit['ts'] < 3600:
        return hit['tag'], hit['error']
    tags, e = reg.list_tags(host, repo)
    tag = reg.newest_semver(tags)
    _LATEST[key] = {'ts': time.time(), 'tag': tag, 'error': e}
    return tag, e


# ─── Status assembly ──────────────────────────────────────────────────

def _base_status(with_health=False):
    """Everything the page / card / hooks need. Docker unreachable → a
    reachable:false stub (not an error); no containers → deployed:false."""
    st = {'project': HALOGEN_PROJECT, 'reachable': True, 'deployed': False,
          'containers': {}, 'api_url': HALOGEN_URL or None,
          'working_dir': _stack_dir(), 'boot_enabled': None,
          'running': False, 'healthy': False, 'image_tag': None}
    try:
        containers = _find_containers()
    except DockerError as e:
        st.update({'reachable': False, 'error': e.message})
        return st
    st['containers'] = containers
    if not containers:
        return st
    st['deployed'] = True
    api = containers.get('api') or {}
    engine = containers.get('engine') or {}
    wd = engine.get('working_dir') or api.get('working_dir')
    if wd:
        st['working_dir'] = HALOGEN_DIR or wd
        _remember_stack(wd, engine.get('config_files') or api.get('config_files') or [])
    st['image_tag'] = engine.get('tag') or api.get('tag') or None
    st['tags_match'] = (engine.get('image') == api.get('image')) if (engine and api) else None
    st['running'] = bool(engine.get('running') and api.get('running'))
    st['healthy'] = engine.get('health') == 'healthy' and api.get('health') == 'healthy'
    policies = {c.get('restart_policy') for c in containers.values()}
    st['boot_enabled'] = policies == {'unless-stopped'} or policies == {'always'}
    st['api_url'] = _api_url(containers)
    if st['running']:
        text, e = _http_get(st['api_url'], '/metrics', 3)
        st['metrics'] = _metrics_summary(_parse_metrics(text)) if text else None
        st['metrics_error'] = e
        if with_health:
            text, e = _http_get(st['api_url'], '/health', 8)
            try:
                st['health'] = _health_summary(json.loads(text)) if text else None
            except ValueError:
                st['health'], e = None, 'unparseable /health'
            st['health_error'] = e
            text, _ = _http_get(st['api_url'], '/cache', 3)
            try:
                st['cache'] = json.loads(text) if text else None
            except ValueError:
                st['cache'] = None
    if with_health and engine.get('id'):
        try:
            st['budget'] = _budget_lines(_container_logs(engine['id'], 2000))
        except DockerError:
            st['budget'] = []
    if with_health:
        latest, e = _latest_tag(engine.get('image') or api.get('image'))
        st['latest_tag'] = latest
        st['latest_error'] = e
        st['update_available'] = bool(latest and st['image_tag'] and reg.semver_newer(latest, st['image_tag']))
    return st


# ─── Routes ───────────────────────────────────────────────────────────

@bp.route('/api/halogen')
def halogen_status():
    return jsonify(_base_status(with_health=True))


@bp.route('/api/halogen/action', methods=['POST'])
def halogen_action():
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    if action not in HALOGEN_ACTIONS:
        return err('Unknown action')
    if not RE_COMPOSE_PROJECT.match(HALOGEN_PROJECT):
        return err('Invalid compose project name')
    wd = _stack_dir()
    if not wd or not os.path.isdir(wd):
        return err('Stack directory unknown — the stack has never been seen running on this node '
                   '(set DASHBOARD_HALOGEN_DIR or bring it up once by hand)')
    files = _stack_config_files(wd)
    if not files:
        return err('No compose file found in %s' % wd)
    sub, timeout = HALOGEN_ACTIONS[action]
    argv = _compose_args({'name': HALOGEN_PROJECT, 'working_dir': wd, 'config_files': files}, sub)
    out, errout, rc = run(argv, no_sudo=True, timeout=timeout)
    if rc != 0:
        return err((errout or out).strip()[-2000:] or 'docker compose %s failed' % action)
    return jsonify({'success': True, 'action': action, 'detail': (errout or out).strip()[-2000:]})


@bp.route('/api/halogen/boot', methods=['POST'])
def halogen_boot():
    """Enabled-at-boot = restart policy on BOTH containers. No recreate."""
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        return err('enabled must be true or false')
    try:
        containers = _find_containers()
    except DockerError as e:
        return jsonify({'success': False, 'error': e.message}), e.status
    if not containers:
        return err('Stack is not deployed (no containers) — bring it up first')
    body = {'RestartPolicy': {'Name': BOOT_POLICY[enabled], 'MaximumRetryCount': 0}}
    try:
        for c in containers.values():
            docker_request('POST', '/containers/%s/update' % c['id'], body, timeout=30)
    except DockerError as e:
        return jsonify({'success': False, 'error': e.message}), e.status
    return jsonify({'success': True, 'boot_enabled': enabled, 'policy': BOOT_POLICY[enabled]})


@bp.route('/api/halogen/logs')
def halogen_logs():
    service = request.args.get('service', 'engine')
    if service not in HALOGEN_SERVICES:
        return err('Unknown service')
    tail = max(1, min(_num(request.args.get('tail')) or 200, 5000))
    try:
        containers = _find_containers()
        c = containers.get(service)
        if not c:
            return err('%s container not found' % service, 404)
        return jsonify({'service': service, 'container': c['name'], 'logs': _container_logs(c['id'], tail)})
    except DockerError as e:
        return jsonify({'success': False, 'error': e.message}), e.status


# ─── Hooks (summary / alerts / history / metrics) ─────────────────────

def _halogen_summary():
    """Additive /api/summary block `halogen` — docker state + one /metrics
    scrape, NO /health (that pings the engine). None when docker is
    unreachable or the stack is not deployed, so the block is simply absent
    on a node without the stack."""
    st = _base_status()
    if not st['reachable'] or not st['deployed']:
        return None
    m = st.get('metrics') or {}
    c = st['containers']
    return {
        'deployed': True, 'running': st['running'], 'healthy': st['healthy'],
        'image_tag': st['image_tag'], 'boot_enabled': st['boot_enabled'],
        'engine': (c.get('engine') or {}).get('health') or (c.get('engine') or {}).get('state'),
        'api': (c.get('api') or {}).get('health') or (c.get('api') or {}).get('state'),
        'tps': m.get('tps'), 'requests_processing': m.get('requests_processing'),
        'requests_deferred': m.get('requests_deferred'),
        'kv_cache_usage_ratio': m.get('kv_cache_usage_ratio'),
        'draft_accept_ratio': m.get('draft_accept_ratio'),
        'requests_total': m.get('requests_total'),
    }


def _halogen_alerts():
    st = _base_status()
    if not st['reachable'] or not st['deployed']:
        return []
    out = []
    c = st['containers']
    for svc in HALOGEN_SERVICES:
        row = c.get(svc)
        if not row:
            out.append({'key': 'halogen_missing:' + svc,
                        'message': 'Halogen %s container is missing from the stack' % svc})
        elif not row['running']:
            out.append({'key': 'halogen_down:' + svc,
                        'message': 'Halogen %s container is %s' % (svc, row['state'] or 'not running')})
        elif row['health'] and row['health'] != 'healthy':
            out.append({'key': 'halogen_unhealthy:' + svc,
                        'message': 'Halogen %s container is %s' % (svc, row['health'])})
    if st.get('tags_match') is False:
        out.append({'key': 'halogen_tag_drift',
                    'message': 'Halogen engine and api run different images — set one HALOGEN_TAG and recreate'})
    if st['running'] and st['boot_enabled'] is False:
        out.append({'key': 'halogen_boot_off',
                    'message': 'Halogen is running but not enabled at boot — it will not survive a reboot'})
    m = st.get('metrics') or {}
    if (m.get('requests_deferred') or 0) > 0:
        out.append({'key': 'halogen_queue',
                    'message': 'Halogen has %d request(s) queued behind %d in flight — slots are full'
                               % (m['requests_deferred'], m.get('requests_processing') or 0)})
    return out


HALOGEN_HISTORY_METRICS = {'halogen_tps', 'halogen_requests', 'halogen_draft_accept', 'halogen_kv_ratio'}


def _halogen_history():
    """One /metrics scrape per tick, labelled 'halogen'. `halogen_requests` is
    the server's completed-request COUNTER (a restart resets it)."""
    st = _base_status()
    m = st.get('metrics')
    if not m:
        return []
    return [('halogen_tps', 'halogen', m['tps']),
            ('halogen_requests', 'halogen', m['requests_total']),
            ('halogen_draft_accept', 'halogen', None if m['draft_accept_ratio'] is None else round(m['draft_accept_ratio'] * 100, 1)),
            ('halogen_kv_ratio', 'halogen', round(m['kv_cache_usage_ratio'] * 100, 1))]


def _halogen_metrics():
    """/metrics lines. `up` is 0 when deployed but not fully running, absent
    (no lines) when the stack is not deployed at all."""
    st = _base_status()
    if not st['reachable'] or not st['deployed']:
        return []
    lines = ['# HELP storagedash_halogen_up Halogen engine + api running (1) or not (0)',
             '# TYPE storagedash_halogen_up gauge',
             'storagedash_halogen_up %d' % (1 if st['running'] else 0)]
    m = st.get('metrics')
    if not m:
        return lines
    gauges = [('predicted_tokens_seconds', 'Generation throughput, tokens/s', m['tps']),
              ('requests_processing', 'Requests in flight', m['requests_processing']),
              ('requests_deferred', 'Requests queued for a slot', m['requests_deferred']),
              ('kv_cache_usage_ratio', 'KV pool usage ratio', m['kv_cache_usage_ratio']),
              ('requests_total', 'Requests completed (counter)', m['requests_total']),
              ('tokens_predicted_total', 'Tokens generated (counter)', m['tokens_predicted_total'])]
    if m['draft_accept_ratio'] is not None:
        gauges.append(('draft_accept_ratio', 'MTP draft tokens accepted / drafted', m['draft_accept_ratio']))
    for name, help_, val in gauges:
        n = 'storagedash_halogen_' + name
        lines += ['# HELP %s %s' % (n, help_), '# TYPE %s gauge' % n, '%s %s' % (n, val)]
    return lines


MODULE = {'id': 'halogen', 'order': 122, 'label': 'Halogen', 'category': 'AI Tools',
          'default_enabled': False,
          'nav': {'cat': 'ai', 'cat_order': 40, 'pages': [
                  {'id': 'halogen', 'label': 'Halogen', 'icon': 'mon'}]},
          'blueprint': bp,
          'summary': _halogen_summary,
          'alerts': _halogen_alerts,
          'history': _halogen_history,
          'history_metrics': HALOGEN_HISTORY_METRICS,
          'metrics': _halogen_metrics}
