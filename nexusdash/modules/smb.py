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

bp = Blueprint('smb', __name__)

SMBCONF_FILE = '/etc/samba/smb.conf'

DEFAULT_GLOBAL = {'workgroup': 'WORKGROUP', 'server string': '%h server (Samba)',
                  'security': 'user', 'map to guest': 'bad user', 'dns proxy': 'no'}


def smbconf_parse():
    """Round-trip parse: {section: {key: value}} preserving order (lowercased
    keys). Comments/blank lines are dropped on rewrite."""
    sections = {}
    cur = None
    try:
        with open(SMBCONF_FILE) as f:
            for line in f:
                s = line.strip()
                if not s or s[0] in '#;':
                    continue
                if s.startswith('[') and s.endswith(']'):
                    cur = s[1:-1]
                    sections.setdefault(cur, {})
                elif cur is not None and '=' in s:
                    k, v = s.split('=', 1)
                    sections[cur][k.strip().lower()] = v.strip()
    except FileNotFoundError:
        pass
    if 'global' not in sections:
        sections = {'global': dict(DEFAULT_GLOBAL), **sections}
    return sections


def smbconf_render(sections):
    out = []
    for sec, kv in sections.items():
        out.append(f'[{sec}]')
        for k, v in kv.items():
            out.append(f'   {k} = {v}')
        out.append('')
    return '\n'.join(out) + '\n'


def smbconf_apply(sections):
    """Validate with testparm, then write + reload Samba. Never applies a config
    testparm rejects."""
    content = smbconf_render(sections)
    tmp = os.path.join(APP_DIR, '.smb.conf.check')
    try:
        with open(tmp, 'w') as f:
            f.write(content)
        _, e, rc = run(['testparm', '-s', tmp], no_sudo=True)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if rc != 0:
        return {'success': False, 'error': 'Rejected by testparm: ' + e.strip()[-300:]}
    r = run_safe(['tee', SMBCONF_FILE], input_data=content)
    if not r['success']:
        return r
    return run_safe(['systemctl', 'reload-or-restart', SYSTEM_SERVICES['smb']['service']])


def _yn(v, default='no'):
    if isinstance(v, bool):
        return 'yes' if v else 'no'
    return 'yes' if str(v).lower() in ('yes', 'true', '1', 'on') else ('no' if v not in (None, '') else default)


@bp.route('/api/smb/shares')
def smb_shares():
    sections = smbconf_parse()
    shares = []
    for name, kv in sections.items():
        if name.lower() in ('global', 'homes'):
            continue
        objs = kv.get('vfs objects', '')
        shares.append({
            'name': name,
            'path': kv.get('path', ''),
            'comment': kv.get('comment', ''),
            'read_only': kv.get('read only', 'yes'),
            'browseable': kv.get('browseable', 'yes'),
            'guest_ok': kv.get('guest ok', 'no'),
            'available': kv.get('available', 'yes'),
            'valid_users': kv.get('valid users', ''),
            'write_list': kv.get('write list', ''),
            'read_list': kv.get('read list', ''),
            'admin_users': kv.get('admin users', ''),
            'hosts_allow': kv.get('hosts allow', ''),
            'hosts_deny': kv.get('hosts deny', ''),
            'force_user': kv.get('force user', ''),
            'force_group': kv.get('force group', ''),
            'create_mask': kv.get('create mask', ''),
            'directory_mask': kv.get('directory mask', ''),
            'vfs': {'recycle': 'recycle' in objs, 'shadow_copy': 'shadow_copy2' in objs,
                    'time_machine': 'fruit' in objs, 'audit': 'full_audit' in objs},
        })
    return jsonify(shares)


@bp.route('/api/smb/shares', methods=['POST'])
def smb_share_save():
    """Create or update a share (upsert by name)."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    path = (data.get('path') or '').strip()
    if not RE_SHARE.match(name) or name.lower() in ('global', 'homes'):
        return err('Invalid share name')
    if not RE_PATH.match(path):
        return err('Invalid path')

    def acl(field):
        v = (data.get(field) or '').strip()
        if v and not RE_ACL.match(v):
            raise ValueError(field)
        return v

    try:
        valid_users, write_list, read_list, admin_users = (acl('valid_users'), acl('write_list'),
                                                            acl('read_list'), acl('admin_users'))
        force_user, force_group = acl('force_user'), acl('force_group')
    except ValueError as ex:
        return err(f'Invalid value for {ex}')
    hosts_allow = (data.get('hosts_allow') or '').strip()
    hosts_deny = (data.get('hosts_deny') or '').strip()
    if (hosts_allow and not RE_HOSTS.match(hosts_allow)) or (hosts_deny and not RE_HOSTS.match(hosts_deny)):
        return err('Invalid hosts allow/deny')
    cmask = (data.get('create_mask') or '').strip()
    dmask = (data.get('directory_mask') or '').strip()
    if (cmask and not RE_MASK.match(cmask)) or (dmask and not RE_MASK.match(dmask)):
        return err('Invalid mask')
    comment = (data.get('comment') or '').strip()
    if not RE_COMMENT.match(comment):
        return err('Invalid comment')

    kv = {}
    if comment:
        kv['comment'] = comment
    kv['path'] = path
    kv['browseable'] = _yn(data.get('browseable', 'yes'))
    kv['read only'] = _yn(data.get('read_only', 'no'))
    kv['guest ok'] = _yn(data.get('guest_ok', 'no'))
    if not _yn(data.get('available', 'yes')) == 'yes':
        kv['available'] = 'no'
    for key, val in (('valid users', valid_users), ('write list', write_list), ('read list', read_list),
                     ('admin users', admin_users), ('hosts allow', hosts_allow), ('hosts deny', hosts_deny),
                     ('force user', force_user), ('force group', force_group),
                     ('create mask', cmask), ('directory mask', dmask)):
        if val:
            kv[key] = val

    # VFS modules
    vfs = data.get('vfs') or {}
    objects, extra = [], {}
    if vfs.get('time_machine'):
        objects += ['catia', 'fruit', 'streams_xattr']
        extra.update({'fruit:time machine': 'yes', 'fruit:metadata': 'stream'})
    if vfs.get('recycle'):
        objects.append('recycle')
        extra.update({'recycle:repository': '.recycle/%U', 'recycle:keeptree': 'yes', 'recycle:versions': 'yes'})
    if vfs.get('shadow_copy'):
        objects.append('shadow_copy2')
        extra.update({'shadow:snapdir': '.zfs/snapshot', 'shadow:sort': 'desc', 'shadow:localtime': 'yes',
                      'shadow:snapprefix': r'autosnap_\(hourly\|daily\|weekly\|monthly\)',
                      'shadow:delimiter': '_', 'shadow:format': '%Y-%m-%d_%H%M%S'})
    if vfs.get('audit'):
        objects.append('full_audit')
        extra.update({'full_audit:prefix': '%u|%I|%S', 'full_audit:success': 'mkdir rename unlink rmdir pwrite',
                      'full_audit:failure': 'none', 'full_audit:facility': 'local5', 'full_audit:priority': 'notice'})
    if objects:
        kv['vfs objects'] = ' '.join(objects)
        kv.update(extra)

    run_safe(['mkdir', '-p', '--', path])
    run_safe(['chmod', '2775', '--', path])
    sections = smbconf_parse()
    sections[name] = kv
    return jsonify(smbconf_apply(sections))


@bp.route('/api/smb/shares/<name>', methods=['DELETE'])
def smb_share_delete(name):
    if not RE_SHARE.match(name):
        return err('Invalid share name')
    sections = smbconf_parse()
    if name in sections:
        del sections[name]
    return jsonify(smbconf_apply(sections))


@bp.route('/api/smb/shares/<name>/toggle', methods=['POST'])
def smb_share_toggle(name):
    if not RE_SHARE.match(name):
        return err('Invalid share name')
    sections = smbconf_parse()
    if name not in sections:
        return err('No such share', 404)
    if sections[name].get('available', 'yes') == 'no':
        sections[name].pop('available', None)  # available
    else:
        sections[name]['available'] = 'no'
    return jsonify(smbconf_apply(sections))

@bp.route('/api/smb/status')
def smb_status():
    out, _, _ = run(['smbstatus', '--json'])
    try:
        d = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        d = {}
    sessions = []
    for s in (d.get('sessions') or {}).values():
        enc = s.get('encryption')
        sessions.append({
            'username': s.get('username', ''),
            'machine': s.get('remote_machine') or s.get('hostname', ''),
            'dialect': s.get('session_dialect', ''),
            'encryption': enc.get('cipher', '-') if isinstance(enc, dict) else (enc or '-'),
        })
    tcons = [{'share': t.get('service', ''), 'machine': t.get('machine', '')}
             for t in (d.get('tcons') or {}).values()]
    return jsonify({'sessions': sessions, 'tcons': tcons, 'open_files': len(d.get('open_files') or {})})

# ─── SMB global settings ─────────────────────────────────────────────

SMB_GLOBAL_KEYS = ['workgroup', 'server string', 'server min protocol',
                   'map to guest', 'smb encrypt', 'server signing']


@bp.route('/api/smb/global')
def smb_global_get():
    g = smbconf_parse().get('global', {})
    return jsonify({k: g.get(k, '') for k in SMB_GLOBAL_KEYS})

@bp.route('/api/smb/global', methods=['POST'])
def smb_global_set():
    data = request.get_json() or {}
    workgroup = (data.get('workgroup') or '').strip()
    server_string = (data.get('server string') or '').strip()
    minproto = (data.get('server min protocol') or '').strip()
    mtg = (data.get('map to guest') or '').strip()
    enc = (data.get('smb encrypt') or '').strip()
    sign = (data.get('server signing') or '').strip()
    if workgroup and not re.match(r'^[A-Za-z0-9_-]{1,15}$', workgroup):
        return err('Invalid workgroup')
    if not RE_COMMENT.match(server_string):
        return err('Invalid server string')
    if minproto not in ('', 'NT1', 'SMB2', 'SMB3'):
        return err('Invalid min protocol')
    if mtg not in ('', 'Never', 'Bad User', 'Bad Password'):
        return err('Invalid map to guest')
    if enc not in ('', 'off', 'desired', 'required', 'auto', 'enabled'):
        return err('Invalid smb encrypt')
    if sign not in ('', 'auto', 'mandatory', 'disabled', 'default'):
        return err('Invalid server signing')
    sections = smbconf_parse()
    g = sections.setdefault('global', {})
    for k, v in (('workgroup', workgroup), ('server string', server_string),
                 ('server min protocol', minproto), ('map to guest', mtg),
                 ('smb encrypt', enc), ('server signing', sign)):
        if v:
            g[k] = v
        else:
            g.pop(k, None)  # empty = leave at Samba default (remove the key)
    return jsonify(smbconf_apply(sections))

@bp.route('/api/smb/users', methods=['POST'])
def smb_user_create():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not RE_USER.match(username):
        return err('Invalid username')
    if not password:
        return err('Password required')
    run(['useradd', '-M', '-s', '/usr/sbin/nologin', username])
    out, e, rc = run(['smbpasswd', '-a', '-s', username], input_data=f'{password}\n{password}\n')
    return jsonify({'success': rc == 0, 'stdout': out, 'stderr': e})

@bp.route('/api/smb/users/<username>', methods=['DELETE'])
def smb_user_delete(username):
    if not RE_USER.match(username):
        return err('Invalid username')
    return jsonify(run_safe(['smbpasswd', '-x', username]))

@bp.route('/api/smb/users')
def smb_users_list():
    """SMB users with enabled/disabled state (from pdbedit account flags)."""
    out, _, _ = run(['pdbedit', '-Lw'])
    users = []
    for line in out.strip().split('\n'):
        parts = line.split(':')
        if len(parts) >= 5 and parts[0]:
            flags = parts[4].strip('[] ')
            users.append({'username': parts[0], 'enabled': 'D' not in flags})
    return jsonify(users)

@bp.route('/api/smb/users/<username>/password', methods=['POST'])
def smb_user_password(username):
    if not RE_USER.match(username):
        return err('Invalid username')
    password = (request.get_json() or {}).get('password') or ''
    if not password:
        return err('Password required')
    out, e, rc = run(['smbpasswd', '-s', username], input_data=f'{password}\n{password}\n')
    return jsonify({'success': rc == 0, 'stdout': out, 'stderr': e})

@bp.route('/api/smb/users/<username>/enable', methods=['POST'])
def smb_user_enable(username):
    if not RE_USER.match(username):
        return err('Invalid username')
    return jsonify(run_safe(['smbpasswd', '-e', username]))

@bp.route('/api/smb/users/<username>/disable', methods=['POST'])
def smb_user_disable(username):
    if not RE_USER.match(username):
        return err('Invalid username')
    return jsonify(run_safe(['smbpasswd', '-d', username]))

# ─── SMB groups (for group-based share access) ───────────────────────

@bp.route('/api/smb/groups')
def smb_groups_list():
    out, _, _ = run(['getent', 'group'], no_sudo=True)
    groups = []
    for line in out.strip().split('\n'):
        parts = line.split(':')
        if len(parts) >= 4 and parts[2].isdigit() and 1000 <= int(parts[2]) < 65534:
            members = [m for m in parts[3].split(',') if m]
            groups.append({'name': parts[0], 'gid': int(parts[2]), 'members': members})
    return jsonify(groups)

@bp.route('/api/smb/groups', methods=['POST'])
def smb_group_create():
    name = ((request.get_json() or {}).get('name') or '').strip()
    if not RE_GROUP.match(name):
        return err('Invalid group name')
    return jsonify(run_safe(['groupadd', name]))

@bp.route('/api/smb/groups/<name>', methods=['DELETE'])
def smb_group_delete(name):
    if not RE_GROUP.match(name):
        return err('Invalid group name')
    return jsonify(run_safe(['groupdel', name]))

@bp.route('/api/smb/groups/<name>/members', methods=['POST'])
def smb_group_member(name):
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    action = data.get('action', 'add')
    if not RE_GROUP.match(name) or not RE_USER.match(username):
        return err('Invalid group or username')
    if action == 'add':
        return jsonify(run_safe(['gpasswd', '-a', username, name]))
    if action == 'remove':
        return jsonify(run_safe(['gpasswd', '-d', username, name]))
    return err('Invalid action')

# ─── SMB home directories ([homes] special share) ────────────────────

HOMES_BLOCK = (
    '\n[homes]\n'
    '   comment = Home Directories\n'
    '   browseable = no\n'
    '   read only = no\n'
    '   valid users = %S\n'
    '   create mask = 0700\n'
    '   directory mask = 0700\n'
)


def _smb_has_homes():
    try:
        with open(SMBCONF_FILE) as f:
            return any(l.strip().lower() == '[homes]' for l in f)
    except FileNotFoundError:
        return False


def _smb_remove_section(content, name):
    """Return smb.conf content with the named [section] removed."""
    out, skip = [], False
    for line in content.split('\n'):
        s = line.strip()
        if s.startswith('[') and s.endswith(']'):
            skip = (s[1:-1].lower() == name.lower())
        if not skip:
            out.append(line)
    return '\n'.join(out)


@bp.route('/api/smb/homes')
def smb_homes_get():
    return jsonify({'enabled': _smb_has_homes()})

@bp.route('/api/smb/homes', methods=['POST'])
def smb_homes_set():
    enabled = bool((request.get_json() or {}).get('enabled'))
    try:
        with open(SMBCONF_FILE) as f:
            content = f.read()
    except FileNotFoundError:
        content = '[global]\n   workgroup = WORKGROUP\n   security = user\n'
    has = _smb_has_homes()
    if enabled and not has:
        content = content.rstrip('\n') + '\n' + HOMES_BLOCK
    elif not enabled and has:
        content = _smb_remove_section(content, 'homes')
    else:
        return jsonify({'success': True, 'enabled': enabled})
    r = run_safe(['tee', SMBCONF_FILE], input_data=content)
    if r['success']:
        run(['testparm', '-s'])
        r = run_safe(['systemctl', 'restart', SYSTEM_SERVICES['smb']['service']])
    r['enabled'] = enabled
    return jsonify(r)


# ─── MiniDLNA / ReadyMedia (media server) ────────────────────────────
# Manage the core settings of the `minidlna` (ReadyMedia) DLNA server: round-trip
# /etc/minidlna.conf (friendly name, port, interface, inotify, root container, and
# the list of media directories), control the service via the shared endpoints
# (minidlna is registered in SYSTEM_SERVICES), and force a database rescan/rebuild.
#
# Same discipline as the NFS/SMB config models: every value is validated against a
# regex/allowlist before it reaches the file (config-file injection — a newline
# into minidlna.conf would inject arbitrary directives). minidlna has no
# `testparm`-equivalent validator, so the up-front allowlists ARE the guard. The
# write goes through the pinned `tee /etc/minidlna.conf` sudoers grant, and the
# DB rebuild through a root-owned wrapper (deleting files.db + `minidlnad -R` as
# root are escalation-sensitive) — never open()+write() to /etc, never `sudo rm`.


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'smb', 'label': 'SMB/CIFS', 'category': 'Sharing',
          'blueprint': bp}
