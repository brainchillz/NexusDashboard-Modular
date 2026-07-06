"""Compose stacks — discover, drive and (for dashboard-created stacks) edit
docker-compose projects.

Two kinds of stack, one list:

* ADOPTED — anything already deployed on the host. Discovered purely from the
  compose labels Docker stamps on containers (project, service, working_dir,
  config_files); the dashboard never needs to know where the files live until
  an action shells out to `docker compose` against that directory. Files are
  shown read-only when the service user can read them.
* MANAGED — created here, stored under COMPOSE_ROOT (`compose/` next to the
  app's state files, one directory per stack, `compose.yaml` inside). Content
  is validated with `docker compose config` before it is ever kept — a bad
  edit restores the previous file, the netplan-helper pattern.

`docker compose` runs as the service user via the docker group — run() with
no_sudo=True, never sudo. Actions are argument-list only; the project name
and every path are validated/confined before they reach the CLI.
"""
import os
import re
import shutil
from flask import Blueprint, jsonify, request

from ..core.config import APP_DIR
from ..core.runcmd import run, err
from .docker import DockerError, docker_request

bp = Blueprint('compose', __name__)

COMPOSE_ROOT = os.environ.get('DASHBOARD_COMPOSE_DIR',
                              os.path.join(APP_DIR, 'compose'))
COMPOSE_FILE = 'compose.yaml'

# Compose project names: lowercase alnum, -, _ (compose's own constraint).
RE_COMPOSE_PROJECT = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}\Z')

COMPOSE_ACTIONS = {
    'up': (['up', '-d'], 600),
    'down': (['down'], 300),          # never -v from the UI: volumes survive
    'stop': (['stop'], 300),
    'start': (['start'], 300),
    'restart': (['restart'], 300),
    'pull': (['pull'], 600),
}

_LBL = 'com.docker.compose.'


def compose_available():
    _, _, rc = run(['docker', 'compose', 'version'], no_sudo=True)
    return rc == 0


def _managed_dir(name):
    """COMPOSE_ROOT/<name>, confined. Returns None on any funny business."""
    if not RE_COMPOSE_PROJECT.match(name or ''):
        return None
    d = os.path.realpath(os.path.join(COMPOSE_ROOT, name))
    if os.path.dirname(d) != os.path.realpath(COMPOSE_ROOT):
        return None
    return d


def _discover_stacks():
    """Merge label-discovered (adopted) stacks with on-disk managed ones."""
    stacks = {}
    try:
        cts = docker_request('GET', '/containers/json?all=1')
    except DockerError:
        cts = []
    for c in cts:
        lbl = c.get('Labels') or {}
        project = lbl.get(_LBL + 'project')
        if not project:
            continue
        st = stacks.setdefault(project, {
            'name': project,
            'working_dir': lbl.get(_LBL + 'project.working_dir', ''),
            'config_files': [f for f in
                             lbl.get(_LBL + 'project.config_files', '').split(',') if f],
            'services': [], 'running': 0, 'total': 0,
        })
        state = c.get('State')
        st['services'].append({
            'service': lbl.get(_LBL + 'service', '?'),
            'container': (c.get('Names') or ['/?'])[0].lstrip('/'),
            'id': (c.get('Id') or '')[:12],
            'state': state,
            'status': c.get('Status'),
        })
        st['total'] += 1
        st['running'] += 1 if state == 'running' else 0
    # Managed stacks that exist on disk but have no containers (yet).
    try:
        names = sorted(os.listdir(COMPOSE_ROOT))
    except OSError:
        names = []
    for name in names:
        d = _managed_dir(name)
        if d and os.path.isfile(os.path.join(d, COMPOSE_FILE)):
            st = stacks.setdefault(name, {
                'name': name, 'working_dir': d, 'config_files': [],
                'services': [], 'running': 0, 'total': 0,
            })
            st['managed'] = True
            st['working_dir'] = d
            st['config_files'] = [os.path.join(d, COMPOSE_FILE)]
    for st in stacks.values():
        st.setdefault('managed', False)
        st['services'].sort(key=lambda s: s['service'])
        # Actions need a directory to run in.
        st['actions_available'] = bool(st['working_dir'])
        st['file_readable'] = any(os.access(f, os.R_OK)
                                  for f in st['config_files'])
    return sorted(stacks.values(), key=lambda s: s['name'])


def _compose_args(st, sub):
    """Build the docker compose argv for a stack (validated upstream)."""
    args = ['docker', 'compose', '-p', st['name'],
            '--project-directory', st['working_dir']]
    for f in st['config_files']:
        args += ['-f', f]
    return args + sub


def _find_stack(project):
    if not RE_COMPOSE_PROJECT.match(project or ''):
        return None
    return next((s for s in _discover_stacks() if s['name'] == project), None)


@bp.route('/api/compose')
def compose_list():
    return jsonify({'available': compose_available(),
                    'compose_root': COMPOSE_ROOT,
                    'stacks': _discover_stacks()})


@bp.route('/api/compose/<project>/action', methods=['POST'])
def compose_action(project):
    action = (request.get_json() or {}).get('action', '')
    if action not in COMPOSE_ACTIONS:
        return err('Action must be one of: %s' % ', '.join(sorted(COMPOSE_ACTIONS)))
    st = _find_stack(project)
    if st is None:
        return err('Unknown stack', 404)
    if not st['actions_available']:
        return err('This stack has no usable project directory (deployed by a '
                   'compose version without directory labels)')
    if not os.path.isdir(st['working_dir']):
        return err('Project directory %s does not exist on this host'
                   % st['working_dir'])
    sub, timeout = COMPOSE_ACTIONS[action]
    out, errout, rc = run(_compose_args(st, sub), no_sudo=True, timeout=timeout)
    if rc != 0:
        return err((errout or out).strip()[-2000:] or
                   'docker compose %s failed' % action)
    return jsonify({'success': True, 'detail': (errout or out).strip()[-2000:]})


@bp.route('/api/compose/<project>/logs')
def compose_logs(project):
    st = _find_stack(project)
    if st is None:
        return err('Unknown stack', 404)
    if not st['actions_available']:
        return err('No project directory for this stack')
    try:
        tail = max(1, min(int(request.args.get('tail', 200)), 5000))
    except ValueError:
        return err('tail must be a number')
    out, errout, rc = run(_compose_args(st, ['logs', '--no-color',
                                             '--tail', str(tail)]),
                          no_sudo=True, timeout=60)
    if rc != 0:
        return err((errout or out).strip()[-2000:] or 'compose logs failed')
    return jsonify({'logs': out, 'tail': tail})


@bp.route('/api/compose/<project>/file')
def compose_file_get(project):
    st = _find_stack(project)
    if st is None:
        return err('Unknown stack', 404)
    for f in st['config_files']:
        try:
            with open(f) as fh:
                return jsonify({'file': f, 'content': fh.read(),
                                'editable': st['managed']})
        except OSError:
            continue
    return err('Compose file is not readable by the dashboard user', 403)


def _validate_compose_dir(d):
    """`docker compose config` in the stack dir — the YAML gatekeeper."""
    out, errout, rc = run(['docker', 'compose', '--project-directory', d,
                           '-f', os.path.join(d, COMPOSE_FILE), 'config',
                           '--quiet'], no_sudo=True, timeout=60)
    return rc == 0, (errout or out).strip()[-2000:]


@bp.route('/api/compose/create', methods=['POST'])
def compose_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    content = data.get('content') or ''
    d = _managed_dir(name)
    if d is None:
        return err('Stack name: lowercase letters, digits, - and _ only')
    if os.path.exists(d):
        return err('A managed stack with that name already exists')
    if not content.strip():
        return err('Compose file content required')
    if '\x00' in content:
        return err('Invalid content')
    os.makedirs(d, mode=0o755)
    with open(os.path.join(d, COMPOSE_FILE), 'w') as f:
        f.write(content)
    ok, detail = _validate_compose_dir(d)
    if not ok:
        shutil.rmtree(d, ignore_errors=True)
        return err('Compose rejected the file:\n%s' % detail)
    return jsonify({'success': True, 'name': name})


@bp.route('/api/compose/<project>/file', methods=['POST'])
def compose_file_save(project):
    st = _find_stack(project)
    if st is None:
        return err('Unknown stack', 404)
    if not st['managed']:
        return err('Only stacks created here are editable — this one is '
                   'managed outside the dashboard')
    content = (request.get_json() or {}).get('content') or ''
    if not content.strip() or '\x00' in content:
        return err('Compose file content required')
    d = _managed_dir(project)
    path = os.path.join(d, COMPOSE_FILE)
    with open(path) as f:
        prev = f.read()
    with open(path, 'w') as f:
        f.write(content)
    ok, detail = _validate_compose_dir(d)
    if not ok:
        with open(path, 'w') as f:
            f.write(prev)
        return err('Compose rejected the edit (previous file restored):\n%s'
                   % detail)
    return jsonify({'success': True})


@bp.route('/api/compose/<project>/delete', methods=['POST'])
def compose_delete(project):
    st = _find_stack(project)
    if st is None:
        return err('Unknown stack', 404)
    if not st['managed']:
        return err('Only stacks created here can be deleted from the dashboard')
    if st['running']:
        return err('Stack has running services — bring it down first')
    if st['total']:
        return err('Stack still has containers — bring it down first')
    d = _managed_dir(project)
    if d and os.path.isdir(d):
        shutil.rmtree(d)
    return jsonify({'success': True})


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'compose', 'label': 'Compose Stacks', 'category': 'Docker',
          'blueprint': bp}
