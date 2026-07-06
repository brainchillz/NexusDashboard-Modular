"""Docker subsystem — containers, images, volumes and networks over the local
Engine API unix socket.

Same access model as the Containers (LXD) module: the dashboard user joins the
`docker` group and talks straight to /var/run/docker.sock — no sudo, no CLI.
On a host without Docker the page reports reachable:false and everything
degrades gracefully (the LXD-without-socket contract).

Tier 1 scope: manage what exists — lifecycle actions, logs, stats, inspect,
image pull/delete/prune, volume and network create/delete. Container creation
and compose stacks are the next tier.
"""
import json
import os
import re
import shlex
import urllib.parse
from flask import Blueprint, jsonify, request

from ..core.runcmd import err
from .containers.client import _UnixHTTPConnection

bp = Blueprint('docker', __name__)

DOCKER_SOCKET = os.environ.get('DASHBOARD_DOCKER_SOCKET', '/var/run/docker.sock')

# Container/volume/network names and hex ids. No '/', so these are safe to
# embed in an API path.
RE_DK_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z')
# Image reference: [registry[:port]/]repo/name[:tag][@sha256:...]. Goes into
# the API as a QUERY VALUE (urlencoded) or a quoted path segment, never a
# shell — the character class only needs to keep the reference sane.
RE_DK_IMAGE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,255}\Z')
RE_DK_SUBNET = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}\Z')

DK_CONTAINER_ACTIONS = {'start', 'stop', 'restart', 'pause', 'unpause', 'kill'}
# Docker's built-in networks — deleting them breaks the daemon's networking.
DK_BUILTIN_NETWORKS = {'bridge', 'host', 'none'}
DK_RESTART_POLICIES = {'no', 'always', 'unless-stopped', 'on-failure'}
RE_DK_ENV = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=[^\x00\n]*\Z')
RE_DK_HOSTIP = re.compile(r'^[0-9a-fA-F:.]+\Z')


class DockerError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status or 500
        self.message = message


def docker_raw(method, path, body=None, timeout=60):
    """One HTTP round-trip to the Docker Engine API over the unix socket.
    Returns (status_code, raw_bytes). Raises DockerError on transport
    failure. Unversioned paths — the daemon serves its newest API."""
    conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout=timeout)
    data = None
    headers = {'Host': 'docker', 'Accept': 'application/json'}
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw
    except (OSError, ValueError) as e:
        raise DockerError(502, 'Cannot reach the Docker daemon at %s: %s'
                          % (DOCKER_SOCKET, e))
    finally:
        conn.close()


def docker_request(method, path, body=None, timeout=60):
    """Engine API call returning decoded JSON ({} for empty 2xx responses).
    Docker reports errors as {"message": ...} with a 4xx/5xx status; 304
    (already started/stopped) counts as success."""
    status, raw = docker_raw(method, path, body, timeout=timeout)
    if status == 304 or not raw:
        doc = {}
    else:
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = {'message': raw[:300].decode('utf-8', 'replace')}
    if status >= 400:
        msg = doc.get('message') if isinstance(doc, dict) else None
        raise DockerError(status, msg or 'Docker daemon error (HTTP %d)' % status)
    return doc


def _dk_error_response(e):
    return (jsonify({'success': False, 'error': e.message}),
            e.status if 400 <= e.status < 600 else 500)


def _dk_demux_logs(raw):
    """Docker log/attach streams from non-TTY containers are multiplexed in
    8-byte-header frames [stream, 0, 0, 0, len(4, BE)]; TTY containers send
    plain bytes. Returns the payload bytes."""
    out = []
    i, n = 0, len(raw)
    while i + 8 <= n:
        if raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b'\x00\x00\x00':
            ln = int.from_bytes(raw[i + 4:i + 8], 'big')
            out.append(raw[i + 8:i + 8 + ln])
            i += 8 + ln
        else:
            return raw          # not multiplexed after all
    return b''.join(out) if out else raw[i:] if i else raw


def _dk_ports(ports):
    """Compact, docker-ps-style port list from a container's Ports array."""
    out = set()
    for p in ports or []:
        proto = p.get('Type', 'tcp')
        if p.get('PublicPort'):
            ip = p.get('IP') or ''
            prefix = '' if ip in ('', '0.0.0.0', '::') else ip + ':'
            out.add('%s%s->%s/%s' % (prefix, p['PublicPort'],
                                     p.get('PrivatePort'), proto))
        else:
            out.add('%s/%s' % (p.get('PrivatePort'), proto))
    return sorted(out)


def _dk_container_summary(c):
    return {
        'id': (c.get('Id') or '')[:12],
        'name': (c.get('Names') or ['/?'])[0].lstrip('/'),
        'image': c.get('Image'),
        'state': c.get('State'),          # running/exited/paused/created/...
        'status': c.get('Status'),        # human text, e.g. "Up 3 days (healthy)"
        'created': c.get('Created'),
        'ports': _dk_ports(c.get('Ports')),
        'compose_project': (c.get('Labels') or {}).get('com.docker.compose.project', ''),
    }


def _dk_stats_summary(s):
    """Boil a /stats document down to what the UI shows. CPU% is the classic
    delta formula; memory subtracts inactive_file (what `docker stats` shows
    on cgroup v2)."""
    cpu_pct = None
    try:
        cd = (s['cpu_stats']['cpu_usage']['total_usage']
              - s['precpu_stats']['cpu_usage']['total_usage'])
        sd = (s['cpu_stats']['system_cpu_usage']
              - s['precpu_stats']['system_cpu_usage'])
        ncpu = (s['cpu_stats'].get('online_cpus')
                or len(s['cpu_stats']['cpu_usage'].get('percpu_usage') or []) or 1)
        if sd > 0 and cd >= 0:
            cpu_pct = round(cd / sd * ncpu * 100.0, 1)
    except (KeyError, TypeError):
        pass
    mem = s.get('memory_stats') or {}
    mem_usage = mem.get('usage')
    if mem_usage is not None:
        mem_usage -= (mem.get('stats') or {}).get('inactive_file', 0)
    rx = tx = 0
    for nic in (s.get('networks') or {}).values():
        rx += nic.get('rx_bytes', 0)
        tx += nic.get('tx_bytes', 0)
    blk_read = blk_write = 0
    for entry in (s.get('blkio_stats') or {}).get('io_service_bytes_recursive') or []:
        op = (entry.get('op') or '').lower()
        if op == 'read':
            blk_read += entry.get('value', 0)
        elif op == 'write':
            blk_write += entry.get('value', 0)
    return {
        'cpu_pct': cpu_pct,
        'mem_usage': mem_usage,
        'mem_limit': mem.get('limit'),
        'net_rx': rx, 'net_tx': tx,
        'blk_read': blk_read, 'blk_write': blk_write,
        'pids': (s.get('pids_stats') or {}).get('current'),
    }


# ═══════════════════════════════════════════════════════════════════════
#  Engine / overview
# ═══════════════════════════════════════════════════════════════════════

@bp.route('/api/docker')
def docker_overview():
    try:
        info = docker_request('GET', '/info', timeout=10)
        version = docker_request('GET', '/version', timeout=10)
    except DockerError as e:
        return jsonify({'reachable': False, 'socket': DOCKER_SOCKET,
                        'error': e.message})
    return jsonify({
        'reachable': True,
        'socket': DOCKER_SOCKET,
        'version': version.get('Version'),
        'api_version': version.get('ApiVersion'),
        'containers': info.get('Containers', 0),
        'running': info.get('ContainersRunning', 0),
        'paused': info.get('ContainersPaused', 0),
        'stopped': info.get('ContainersStopped', 0),
        'images': info.get('Images', 0),
        'storage_driver': info.get('Driver'),
        'os': info.get('OperatingSystem'),
        'kernel': info.get('KernelVersion'),
        'compose': None,        # tier 2: compose stack support
    })


def _docker_summary():
    """Summary hook (aggregated into /api/summary when the module is enabled)."""
    try:
        info = docker_request('GET', '/info', timeout=5)
    except DockerError:
        return {}
    return {'docker': {'containers': info.get('Containers', 0),
                       'running': info.get('ContainersRunning', 0),
                       'images': info.get('Images', 0)}}


# ═══════════════════════════════════════════════════════════════════════
#  Containers
# ═══════════════════════════════════════════════════════════════════════

@bp.route('/api/docker/containers')
def dk_containers_list():
    try:
        cts = docker_request('GET', '/containers/json?all=1')
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify([_dk_container_summary(c) for c in cts])


def _dk_pull(ref, timeout=600):
    """Pull an image; raises DockerError on failure (including mid-stream
    NDJSON errors, which arrive with HTTP 200)."""
    q = urllib.parse.urlencode({'fromImage': ref})
    status, raw = docker_raw('POST', '/images/create?%s' % q, timeout=timeout)
    last_error = None
    for line in raw.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get('error'):
            last_error = doc['error']
        if isinstance(doc, dict) and doc.get('message') and status >= 400:
            last_error = doc['message']
    if status >= 400 or last_error:
        raise DockerError(status if status >= 400 else 500,
                          last_error or 'Pull failed (HTTP %d)' % status)


def _dk_create_body(data):
    """Validate + translate the UI's create request into an Engine-API
    container config. Returns (body, error) — error is a string on bad input."""
    image = (data.get('image') or '').strip()
    if not RE_DK_IMAGE.match(image):
        return None, 'Invalid image reference'
    restart = data.get('restart', 'no')
    if restart not in DK_RESTART_POLICIES:
        return None, 'Restart policy must be one of: %s' % ', '.join(sorted(DK_RESTART_POLICIES))
    env = []
    for e in data.get('env') or []:
        e = (e or '').strip()
        if not e:
            continue
        if not RE_DK_ENV.match(e):
            return None, 'Environment entries must be KEY=value (got %r)' % e[:40]
        env.append(e)
    exposed, bindings = {}, {}
    for p in data.get('ports') or []:
        try:
            host, ct = int(p.get('host')), int(p.get('container'))
        except (TypeError, ValueError):
            return None, 'Ports must be numbers'
        proto = p.get('proto', 'tcp')
        if proto not in ('tcp', 'udp'):
            return None, 'Port protocol must be tcp or udp'
        if not (1 <= host <= 65535 and 1 <= ct <= 65535):
            return None, 'Ports must be 1-65535'
        host_ip = (p.get('host_ip') or '').strip()
        if host_ip and not RE_DK_HOSTIP.match(host_ip):
            return None, 'Invalid host IP in port mapping'
        key = '%d/%s' % (ct, proto)
        exposed[key] = {}
        bindings.setdefault(key, []).append(
            {'HostIp': host_ip, 'HostPort': str(host)})
    binds = []
    for v in data.get('volumes') or []:
        src = (v.get('source') or '').strip()
        dst = (v.get('destination') or '').strip()
        if not dst.startswith('/') or '..' in dst or ':' in dst:
            return None, 'Volume destination must be an absolute path (no .. or :)'
        # Source: a named volume, or an absolute host path (bind mount).
        if src.startswith('/'):
            if '..' in src or ':' in src:
                return None, 'Bind-mount source must be an absolute path (no .. or :)'
        elif not RE_DK_NAME.match(src):
            return None, 'Volume source must be a volume name or an absolute path'
        binds.append('%s:%s%s' % (src, dst, ':ro' if v.get('ro') else ''))
    network = (data.get('network') or '').strip()
    if network and not RE_DK_NAME.match(network):
        return None, 'Invalid network name'
    command = (data.get('command') or '').strip()
    cmd = None
    if command:
        try:
            cmd = shlex.split(command)
        except ValueError as e:
            return None, 'Command: %s' % e
    body = {'Image': image, 'Env': env, 'ExposedPorts': exposed,
            'HostConfig': {'PortBindings': bindings, 'Binds': binds,
                           'RestartPolicy': {'Name': restart}}}
    if cmd:
        body['Cmd'] = cmd
    if network:
        body['HostConfig']['NetworkMode'] = network
    return body, None


@bp.route('/api/docker/containers', methods=['POST'])
def dk_container_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if name and not RE_DK_NAME.match(name):
        return err('Invalid container name')
    body, bad = _dk_create_body(data)
    if bad:
        return err(bad)
    q = ('?' + urllib.parse.urlencode({'name': name})) if name else ''
    try:
        try:
            created = docker_request('POST', '/containers/create%s' % q, body=body)
        except DockerError as e:
            # Image not local yet: pull it (long) and retry once.
            if e.status == 404 and 'image' in (e.message or '').lower():
                _dk_pull(body['Image'])
                created = docker_request('POST', '/containers/create%s' % q, body=body)
            else:
                raise
        cid = (created.get('Id') or '')[:12]
        if data.get('start', True):
            docker_request('POST', '/containers/%s/start' % cid, timeout=90)
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True, 'id': cid,
                    'warnings': created.get('Warnings') or []})


@bp.route('/api/docker/containers/<cid>')
def dk_container_detail(cid):
    if not RE_DK_NAME.match(cid):
        return err('Invalid container id')
    try:
        c = docker_request('GET', '/containers/%s/json' % cid)
    except DockerError as e:
        return _dk_error_response(e)
    state = c.get('State') or {}
    cfg = c.get('Config') or {}
    host = c.get('HostConfig') or {}
    nets = ((c.get('NetworkSettings') or {}).get('Networks') or {})
    return jsonify({
        'id': (c.get('Id') or '')[:12],
        'name': (c.get('Name') or '').lstrip('/'),
        'image': cfg.get('Image'),
        'image_id': (c.get('Image') or '').replace('sha256:', '')[:12],
        'created': c.get('Created'),
        'state': {
            'status': state.get('Status'),
            'running': state.get('Running'),
            'exit_code': state.get('ExitCode'),
            'started_at': state.get('StartedAt'),
            'finished_at': state.get('FinishedAt'),
            'health': ((state.get('Health') or {}).get('Status')),
        },
        'restart_policy': (host.get('RestartPolicy') or {}).get('Name', ''),
        'cmd': cfg.get('Cmd'),
        'entrypoint': cfg.get('Entrypoint'),
        'env': cfg.get('Env') or [],
        'tty': cfg.get('Tty', False),
        'ports': _dk_ports([
            {'IP': b.get('HostIp'), 'PublicPort': int(b.get('HostPort') or 0),
             'PrivatePort': int(spec.split('/')[0]), 'Type': spec.split('/')[-1]}
            for spec, binds in ((c.get('NetworkSettings') or {}).get('Ports') or {}).items()
            for b in (binds or [{}])
        ]),
        'mounts': [{'type': m.get('Type'),
                    'source': m.get('Name') or m.get('Source'),
                    'destination': m.get('Destination'),
                    'rw': m.get('RW', True)} for m in c.get('Mounts') or []],
        'networks': {name: {'ip': (n or {}).get('IPAddress', '')}
                     for name, n in nets.items()},
        'labels': cfg.get('Labels') or {},
        'compose_project': (cfg.get('Labels') or {}).get('com.docker.compose.project', ''),
    })


@bp.route('/api/docker/containers/<cid>/logs')
def dk_container_logs(cid):
    if not RE_DK_NAME.match(cid):
        return err('Invalid container id')
    try:
        tail = int(request.args.get('tail', 200))
    except ValueError:
        return err('tail must be a number')
    tail = max(1, min(tail, 10000))
    try:
        insp = docker_request('GET', '/containers/%s/json' % cid)
        status, raw = docker_raw(
            'GET', '/containers/%s/logs?stdout=1&stderr=1&tail=%d' % (cid, tail))
    except DockerError as e:
        return _dk_error_response(e)
    if status >= 400:
        try:
            msg = json.loads(raw).get('message')
        except ValueError:
            msg = None
        return err(msg or 'Could not read logs (HTTP %d)' % status,
                   status if 400 <= status < 600 else 500)
    if not ((insp.get('Config') or {}).get('Tty')):
        raw = _dk_demux_logs(raw)
    return jsonify({'logs': raw.decode('utf-8', 'replace'), 'tail': tail})


@bp.route('/api/docker/containers/<cid>/stats')
def dk_container_stats(cid):
    if not RE_DK_NAME.match(cid):
        return err('Invalid container id')
    try:
        # stream=false takes two samples ~1s apart so the CPU delta is real.
        s = docker_request('GET', '/containers/%s/stats?stream=false' % cid,
                           timeout=15)
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify(_dk_stats_summary(s))


@bp.route('/api/docker/containers/<cid>/action', methods=['POST'])
def dk_container_action(cid):
    if not RE_DK_NAME.match(cid):
        return err('Invalid container id')
    data = request.get_json() or {}
    action = data.get('action', '')
    if action not in DK_CONTAINER_ACTIONS:
        return err('Action must be one of: %s'
                   % ', '.join(sorted(DK_CONTAINER_ACTIONS)))
    path = '/containers/%s/%s' % (cid, action)
    if action in ('stop', 'restart'):
        path += '?t=10'
    try:
        docker_request('POST', path, timeout=90)
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/docker/containers/<cid>/delete', methods=['POST'])
def dk_container_delete(cid):
    if not RE_DK_NAME.match(cid):
        return err('Invalid container id')
    data = request.get_json() or {}
    q = urllib.parse.urlencode({
        'force': '1' if data.get('force') else '0',
        'v': '1' if data.get('volumes') else '0',
    })
    try:
        docker_request('DELETE', '/containers/%s?%s' % (cid, q))
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════
#  Images
# ═══════════════════════════════════════════════════════════════════════

@bp.route('/api/docker/images')
def dk_images_list():
    try:
        imgs = docker_request('GET', '/images/json')
        cts = docker_request('GET', '/containers/json?all=1')
    except DockerError as e:
        return _dk_error_response(e)
    in_use = {c.get('ImageID') for c in cts}
    out = []
    for i in imgs:
        tags = [t for t in (i.get('RepoTags') or []) if t != '<none>:<none>']
        out.append({
            'id': (i.get('Id') or '').replace('sha256:', '')[:12],
            'tags': tags,
            'dangling': not tags,
            'size': i.get('Size', 0),
            'created': i.get('Created', 0),
            'in_use': i.get('Id') in in_use,
        })
    out.sort(key=lambda x: -(x['created'] or 0))
    return jsonify(out)


@bp.route('/api/docker/images/pull', methods=['POST'])
def dk_image_pull():
    ref = ((request.get_json() or {}).get('reference') or '').strip()
    if not RE_DK_IMAGE.match(ref):
        return err('Invalid image reference')
    try:
        _dk_pull(ref)
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/docker/images/delete', methods=['POST'])
def dk_image_delete():
    data = request.get_json() or {}
    ref = (data.get('id') or '').strip()
    # An id (hex) or a repo:tag reference — both are accepted by the API.
    if not RE_DK_IMAGE.match(ref):
        return err('Invalid image reference')
    q = 'force=1' if data.get('force') else 'force=0'
    try:
        docker_request('DELETE',
                       '/images/%s?%s' % (urllib.parse.quote(ref, safe=''), q))
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/docker/images/prune', methods=['POST'])
def dk_images_prune():
    try:
        res = docker_request('POST', '/images/prune')
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True,
                    'reclaimed': res.get('SpaceReclaimed', 0),
                    'deleted': len(res.get('ImagesDeleted') or [])})


# ═══════════════════════════════════════════════════════════════════════
#  Volumes
# ═══════════════════════════════════════════════════════════════════════

@bp.route('/api/docker/volumes')
def dk_volumes_list():
    try:
        vols = docker_request('GET', '/volumes')
        cts = docker_request('GET', '/containers/json?all=1')
    except DockerError as e:
        return _dk_error_response(e)
    used_by = {}
    for c in cts:
        cname = (c.get('Names') or ['/?'])[0].lstrip('/')
        for m in c.get('Mounts') or []:
            if m.get('Type') == 'volume' and m.get('Name'):
                used_by.setdefault(m['Name'], []).append(cname)
    return jsonify([{
        'name': v.get('Name'),
        'driver': v.get('Driver'),
        'mountpoint': v.get('Mountpoint'),
        'created': v.get('CreatedAt'),
        'used_by': sorted(used_by.get(v.get('Name'), [])),
    } for v in (vols.get('Volumes') or [])])


@bp.route('/api/docker/volumes/create', methods=['POST'])
def dk_volume_create():
    name = ((request.get_json() or {}).get('name') or '').strip()
    if not RE_DK_NAME.match(name):
        return err('Invalid volume name')
    try:
        docker_request('POST', '/volumes/create', body={'Name': name})
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/docker/volumes/delete', methods=['POST'])
def dk_volume_delete():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not RE_DK_NAME.match(name):
        return err('Invalid volume name')
    q = 'force=1' if data.get('force') else 'force=0'
    try:
        docker_request('DELETE', '/volumes/%s?%s' % (name, q))
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════
#  Networks
# ═══════════════════════════════════════════════════════════════════════

@bp.route('/api/docker/networks')
def dk_networks_list():
    try:
        nets = docker_request('GET', '/networks')
        cts = docker_request('GET', '/containers/json?all=1')
    except DockerError as e:
        return _dk_error_response(e)
    attached = {}
    for c in cts:
        for name in ((c.get('NetworkSettings') or {}).get('Networks') or {}):
            attached[name] = attached.get(name, 0) + 1
    out = []
    for n in nets:
        subnets = [c.get('Subnet') for c in ((n.get('IPAM') or {}).get('Config') or [])
                   if c.get('Subnet')]
        out.append({
            'id': (n.get('Id') or '')[:12],
            'name': n.get('Name'),
            'driver': n.get('Driver'),
            'scope': n.get('Scope'),
            'internal': n.get('Internal', False),
            'subnets': subnets,
            'containers': attached.get(n.get('Name'), 0),
            'builtin': n.get('Name') in DK_BUILTIN_NETWORKS,
        })
    out.sort(key=lambda x: (not x['builtin'], x['name'] or ''))
    return jsonify(out)


@bp.route('/api/docker/networks/create', methods=['POST'])
def dk_network_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    subnet = (data.get('subnet') or '').strip()
    if not RE_DK_NAME.match(name):
        return err('Invalid network name')
    if name in DK_BUILTIN_NETWORKS:
        return err('That name is reserved by Docker')
    if subnet and not RE_DK_SUBNET.match(subnet):
        return err('Subnet must be IPv4 CIDR, e.g. 172.30.0.0/16')
    body = {'Name': name, 'Driver': 'bridge'}
    if subnet:
        body['IPAM'] = {'Driver': 'default', 'Config': [{'Subnet': subnet}]}
    try:
        docker_request('POST', '/networks/create', body=body)
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/docker/networks/delete', methods=['POST'])
def dk_network_delete():
    name = ((request.get_json() or {}).get('name') or '').strip()
    if not RE_DK_NAME.match(name):
        return err('Invalid network name')
    if name in DK_BUILTIN_NETWORKS:
        return err("Docker's built-in '%s' network cannot be deleted" % name)
    try:
        docker_request('DELETE', '/networks/%s' % name)
    except DockerError as e:
        return _dk_error_response(e)
    return jsonify({'success': True})


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'docker', 'label': 'Docker', 'category': 'Docker',
          'blueprint': bp, 'summary': _docker_summary}
