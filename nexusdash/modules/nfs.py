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

def parse_exports(filepath=EXPORTS_FILE):
    exports = []
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if re.match(r'^\s*/\S', line):
                    parts = line.split()
                    path = parts[0]
                    clients = []
                    for p in parts[1:]:
                        client_parts = p.split('(')
                        if len(client_parts) == 2:
                            clients.append({
                                'host': client_parts[0],
                                'options': client_parts[1].rstrip(')')
                            })
                    exports.append({'path': path, 'clients': clients, 'raw': line})
    except FileNotFoundError:
        pass
    return exports

@bp.route('/api/nfs/exports')
def nfs_exports():
    exports = parse_exports()
    return jsonify(exports)

@bp.route('/api/nfs/exports', methods=['POST'])
def nfs_export_create():
    data = request.get_json()
    path = data.get('path', '').strip()
    clients = data.get('clients', [])
    if not path or not RE_PATH.match(path):
        return err('Invalid export path')

    client_entries = []
    for c in clients:
        host = (c.get('host') or '*').strip()
        opts = (c.get('options') or 'rw,sync,no_subtree_check,no_root_squash').strip()
        if not RE_HOST.match(host):
            return err(f'Invalid client/host: {host}')
        if not RE_NFSOPTS.match(opts):
            return err(f'Invalid export options: {opts}')
        client_entries.append(f'{host}({opts})')
    if not client_entries:
        client_entries.append('*(rw,sync,no_subtree_check,no_root_squash)')

    lines = []
    try:
        with open(EXPORTS_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass

    export_line = f'{path}\t{" ".join(client_entries)}\n'
    # Match an existing export of the exact same path (first whitespace token).
    replaced = False
    for i, l in enumerate(lines):
        toks = l.split()
        if toks and not l.lstrip().startswith('#') and toks[0] == path:
            lines[i] = export_line
            replaced = True
    if not replaced:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(export_line)

    r1 = run_safe(['tee', EXPORTS_FILE], input_data=''.join(lines))
    if not r1['success']:
        return jsonify(r1)
    run_safe(['mkdir', '-p', '--', path])
    return jsonify(run_safe(['exportfs', '-ra']))

@bp.route('/api/nfs/exports/<path:export_path>', methods=['DELETE'])
def nfs_export_delete(export_path):
    if not RE_PATH.match('/' + export_path.lstrip('/')):
        return err('Invalid export path')
    norm = '/' + export_path.lstrip('/')
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
MODULE = {'id': 'nfs', 'label': 'NFS Exports', 'category': 'Sharing',
          'blueprint': bp}
