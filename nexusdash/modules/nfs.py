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

bp = Blueprint('nfs', __name__)

EXPORTS_FILE = '/etc/exports'
# exportfs reads /etc/exports AND every /etc/exports.d/*.exports (exports(5));
# a node whose exports live only in the drop-in dir (Houston / cockpit-file-
# sharing, `zfs set sharenfs` → zfs.exports, a hand-restored file) showed
# "No exports configured" here, and a create wrote a second definition.
EXPORTS_DIR = '/etc/exports.d'
EXPORTS_SUFFIX = '.exports'
# /etc/exports is a whitespace-separated line format where '#' opens a
# comment; RE_PATH allows all of those in a path. A path containing one wrote
# a line that exportfs split at the wrong place (and that the delete route,
# matching on the first token, could never find again).
RE_EXPORT_PATH_BAD = re.compile(r'[\s#"\\]')


def _export_path_error(path):
    if not path or not RE_PATH.match(path):
        return 'Invalid export path'
    if RE_EXPORT_PATH_BAD.search(path):
        return 'Export path may not contain spaces, quotes, backslashes or #'
    return None


def _exports_files(exports_dir):
    """The files exportfs reads, in its order: /etc/exports first, then the
    drop-ins (only regular files with the .exports suffix — zfs's
    `zfs.exports.lock` and editor backups are not exports)."""
    files = [EXPORTS_FILE]
    try:
        names = sorted(os.listdir(exports_dir))
    except OSError:
        names = []
    for n in names:
        full = os.path.join(exports_dir, n)
        if n.endswith(EXPORTS_SUFFIX) and os.path.isfile(full):
            files.append(full)
    return files


def _parse_export_line(line):
    """One export line → {path, defaults, clients, comment} or None.

    exports(5) shape: `<path> [-defaults] client(opts) client ...  # comment`.
    The `-fsid=…` default-options token is what a re-written line must carry
    verbatim (changing an fsid gives every client ESTALE), so it is kept
    apart from the clients rather than dropped the way it used to be."""
    if not re.match(r'^\s*/\S', line):
        return None
    body, comment = line, ''
    m = re.search(r'\s#(.*)$', line)
    if m:
        body, comment = line[:m.start()], m.group(1).strip()
    parts = body.split()
    path = parts[0]
    defaults = ''
    clients = []
    for p in parts[1:]:
        if p.startswith('-') and not clients and not defaults:
            defaults = p[1:]
            continue
        client_parts = p.split('(')
        if len(client_parts) == 2:
            clients.append({'host': client_parts[0], 'options': client_parts[1].rstrip(')')})
        elif len(client_parts) == 1 and p:
            # A bare host exports with exportfs's own defaults (ro,sync,…).
            clients.append({'host': p, 'options': ''})
    return {'path': path, 'defaults': defaults, 'clients': clients, 'comment': comment}


def _render_export_line(path, client_entries, defaults='', comment=''):
    line = path + '\t'
    if defaults:
        line += '-' + defaults + ' '
    line += ' '.join(client_entries)
    if comment:
        line += '  # ' + comment
    return line + '\n'


def _parse_exports_file(filepath):
    exports = []
    editable = (filepath == EXPORTS_FILE)
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                ex = _parse_export_line(line)
                if ex is None:
                    continue
                ex.update({'raw': line, 'source': filepath, 'editable': editable})
                exports.append(ex)
    except OSError:
        # Missing is the normal fresh-node case; unreadable (a 0600 drop-in)
        # is reported as a gap rather than hidden.
        if os.path.exists(filepath):
            exports.append({'path': None, 'clients': [], 'raw': '', 'defaults': '',
                            'comment': '', 'source': filepath, 'editable': False,
                            'error': 'not readable by the dashboard user'})
    return exports


def parse_exports(filepath=None, exports_dir=None):
    """Every export the server will serve: /etc/exports plus /etc/exports.d.

    `parse_exports(somefile)` reads that ONE file (the old signature, kept for
    the parser tests); the no-argument form reads the real set. Rows carry
    `source` (the file) and `editable` — only /etc/exports lines are, the
    sudoers tee grant is pinned to that path and the drop-ins belong to
    whatever wrote them."""
    if filepath is None and exports_dir is None:
        files = _exports_files(EXPORTS_DIR)
    elif filepath is not None and exports_dir is None:
        files = [filepath]
    else:
        files = [filepath or EXPORTS_FILE]
        files += [f for f in _exports_files(exports_dir) if f != EXPORTS_FILE]
    exports = []
    for f in files:
        exports.extend(_parse_exports_file(f))
    return exports


def _foreign_export(path):
    """The drop-in file that already exports `path`, or None."""
    for ex in parse_exports():
        if ex.get('path') == path and not ex['editable']:
            return ex['source']
    return None

@bp.route('/api/nfs/exports')
def nfs_exports():
    exports = parse_exports()
    return jsonify(exports)

@bp.route('/api/nfs/exports', methods=['POST'])
def nfs_export_create():
    data = request.get_json() or {}
    path = (data.get('path') or '').strip()
    clients = data.get('clients') or []
    bad = _export_path_error(path)
    if bad:
        return err(bad)

    client_entries = []
    for c in clients:
        if not isinstance(c, dict):
            return err('Each client must be an object with host/options')
        host = (c.get('host') or '*').strip()
        opts = (c.get('options') or 'rw,sync,no_subtree_check,no_root_squash').strip()
        if not RE_HOST.match(host):
            return err(f'Invalid client/host: {host}')
        if not RE_NFSOPTS.match(opts):
            return err(f'Invalid export options: {opts}')
        client_entries.append(f'{host}({opts})')
    if not client_entries:
        client_entries.append('*(rw,sync,no_subtree_check,no_root_squash)')

    owner = _foreign_export(path)
    if owner:
        return err(f'{path} is already exported by {owner}; edit that file by hand, '
                   f'or move its line into {EXPORTS_FILE} to manage it here')

    lines = []
    try:
        with open(EXPORTS_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass

    # Match an existing export of the exact same path (first whitespace token),
    # keeping its `-fsid=…` defaults and trailing comment: only the clients
    # are being edited.
    replaced = False
    for i, l in enumerate(lines):
        toks = l.split()
        if toks and not l.lstrip().startswith('#') and toks[0] == path:
            old = _parse_export_line(l.strip()) or {}
            lines[i] = _render_export_line(path, client_entries,
                                           old.get('defaults', ''), old.get('comment', ''))
            replaced = True
    if not replaced:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(_render_export_line(path, client_entries))

    r1 = run_safe(['tee', EXPORTS_FILE], input_data=''.join(lines))
    if not r1['success']:
        return jsonify(r1)
    run_safe(['mkdir', '-p', '--', path])
    return jsonify(run_safe(['exportfs', '-ra']))

@bp.route('/api/nfs/exports/<path:export_path>', methods=['DELETE'])
def nfs_export_delete(export_path):
    norm = '/' + export_path.lstrip('/')
    bad = _export_path_error(norm)
    if bad:
        return err(bad)
    owner = _foreign_export(norm)
    if owner:
        return err(f'{norm} is exported by {owner}; remove it from that file by hand')
    try:
        with open(EXPORTS_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return err('No exports file')
    new_lines = []
    for l in lines:
        toks = l.split()
        # Drop only the export line whose path token matches exactly.
        if toks and not l.lstrip().startswith('#') and toks[0] == norm:
            continue
        new_lines.append(l)
    r = run_safe(['tee', EXPORTS_FILE], input_data=''.join(new_lines))
    if not r['success']:
        return jsonify(r)
    # Best-effort: drop the export directory if it is now empty. rmdir can only
    # remove empty directories, so this never deletes a user's data.
    run_safe(['rmdir', norm])
    return jsonify(run_safe(['exportfs', '-ra']))

@bp.route('/api/nfs/exportfs')
def nfs_exportfs_status():
    out, _, _ = run(['exportfs', '-v'])
    return jsonify({'exports': out})

@bp.route('/api/nfs/clients')
def nfs_clients():
    # showmount queries rpc.mountd over RPC; it doesn't need root.
    out, _, _ = run(['showmount', '-a', '--no-headers'], no_sudo=True)
    return jsonify({'clients': out.strip()})

# ─── SMB Share Management ────────────────────────────────────────────



# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'nfs', 'order': 90, 'label': 'NFS Exports', 'category': 'Sharing',
          'nav': {'cat': 'sharing', 'cat_order': 30, 'pages': [
                  {'id': 'nfs', 'label': 'NFS Exports', 'icon': 'share'}]},
          'blueprint': bp,
          'services': {'nfs': {'name': 'NFS Server', 'service': 'nfs-server',
                               'pkg': 'nfs-kernel-server', 'binary': '/usr/sbin/nfsdclnts'}},
          # RHEL/Rocky: NFS ships in `nfs-utils`.
          'services_overrides': {'rhel': {'nfs': {'pkg': 'nfs-utils'}}}}
