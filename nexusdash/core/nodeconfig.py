"""Node configuration export / restore (System > Backup & Restore).

Everything that makes THIS node this node and is not in the code tree or the
package: users + token hashes, module toggles, SSO enrolment, notification
targets, the network config, replication jobs + the SSH key pair + known
hosts, snapshot schedules, maintenance and llama settings (incl. the HF
token), halogen's remembered stack dir, the TLS pair, dnsmasq's state store
and the managed compose stacks. One tarball, optionally encrypted, that
turns a rebuild into "install, restore, restart" instead of the by-hand
list the framework reinstall needed.

Format: `NEXUSCFG1` + 16-byte salt + 32-byte HMAC-SHA256 + ciphertext, where
the ciphertext is `openssl enc -aes-256-cbc -pbkdf2` output and the HMAC key
is PBKDF2(passphrase, salt) over the ciphertext (encrypt-then-MAC — CBC alone
is malleable). No passphrase → plain tar.gz. openssl is the same binary the
TLS module already relies on; no new Python dependency.

Restore is an ALLOWLIST: only the manifest's paths, relative, no `..`, no
symlinks, size-capped, written with the recorded mode via a temp file and
os.replace. auth.json is read per request, so a restored admin works at
once; the TLS pair is loaded at start, hence restart_recommended.
"""
import io
import os
import hmac
import json
import time
import base64
import socket
import tarfile
import hashlib
import secrets
import subprocess
from flask import Blueprint, jsonify, request, Response

from .config import APP_DIR, APP_VERSION, UNIT_PREFIX, TLS_CERT, TLS_KEY
from .runcmd import err
from .auth import _is_admin

bp = Blueprint('nodeconfig', __name__)

MAGIC = b'NEXUSCFG1'
PBKDF2_ITER = 600000
MAX_BUNDLE = 64 * 1024 * 1024
MANIFEST = 'nexus-config.json'

# id -> (relative path, kind, secret?, description). Paths relative to APP_DIR
# except the TLS pair, which follow the configured cert/key paths.
ITEMS = [
    ('auth', 'auth.json', 'file', True, 'Users, roles, password hashes and API token hashes'),
    ('modules', 'modules.json', 'file', False, 'Module on/off toggles'),
    ('sso', 'sso.json', 'file', True, 'SSO enrolment (issuer + public key)'),
    ('notifications', 'notifications.json', 'file', True, 'Notification targets (webhooks / SMTP)'),
    ('network', 'network_config.json', 'file', False, 'Network page settings'),
    ('replication', 'replication.json', 'file', False, 'ZFS replication jobs'),
    ('replication_key', 'replication_key', 'file', True, 'Replication SSH private key'),
    ('replication_key_pub', 'replication_key.pub', 'file', False, 'Replication SSH public key'),
    ('replication_known_hosts', 'replication_known_hosts', 'file', False, 'Replication known hosts'),
    ('schedules', 'schedules.json', 'file', False, 'Snapshot schedules'),
    ('maintenance', 'maintenance.json', 'file', False, 'Scrub / SMART-test schedules'),
    ('llama_presets', 'llama_presets.json', 'file', False, 'llama.cpp presets'),
    ('llama_hf', 'llama_hf.json', 'file', True, 'Model downloader settings incl. the Hugging Face token'),
    ('halogen', 'halogen.json', 'file', False, 'Halogen stack directory'),
    ('tls', None, 'tls', True, 'TLS certificate + private key'),
    ('dnsmasq', 'dnsmasq/state', 'dir', False, 'dnsmasq DNS/DHCP state store'),
    ('compose', 'compose', 'dir', False, 'Managed compose stacks'),
]
ITEM_BY_ID = {i[0]: i for i in ITEMS}


def _abs(rel):
    return os.path.join(APP_DIR, rel)


def _item_paths(item_id):
    """[(archive_path, absolute_path)] for an item that exists here."""
    _id, rel, kind, _secret, _desc = ITEM_BY_ID[item_id]
    if kind == 'tls':
        out = []
        for name, path in (('certs/dashboard.crt', TLS_CERT), ('certs/dashboard.key', TLS_KEY)):
            if os.path.isfile(path):
                out.append((name, path))
        return out
    if kind == 'file':
        return [(rel, _abs(rel))] if os.path.isfile(_abs(rel)) else []
    base = _abs(rel)
    out = []
    if os.path.isdir(base):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in sorted(files):
                p = os.path.join(root, f)
                if f.startswith('.') or f.endswith('.tmp'):
                    continue                          # editor/atomic-write leftovers
                if os.path.isfile(p) and not os.path.islink(p):
                    out.append((os.path.relpath(p, APP_DIR), p))
    return out


def inventory():
    """What this node has to export, per item."""
    rows = []
    for _id, rel, kind, secret, desc in ITEMS:
        paths = _item_paths(_id)
        rows.append({'id': _id, 'description': desc, 'secret': secret, 'kind': kind,
                     'present': bool(paths), 'files': len(paths),
                     'bytes': sum(os.path.getsize(p) for _, p in paths)})
    return rows


def build_bundle(include=None):
    """(tar.gz bytes, manifest). include = item ids (None = every present item)."""
    ids = [i[0] for i in ITEMS] if include is None else [i for i in include if i in ITEM_BY_ID]
    files = []
    for _id in ids:
        for arc, path in _item_paths(_id):
            with open(path, 'rb') as f:
                data = f.read()
            files.append({'path': arc, 'item': _id, 'mode': os.stat(path).st_mode & 0o777,
                          'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), '_data': data})
    manifest = {'format': 1, 'app_version': APP_VERSION, 'hostname': socket.gethostname(),
                'unit_prefix': UNIT_PREFIX, 'created': int(time.time()),
                'items': ids, 'files': [{k: v for k, v in f.items() if k != '_data'} for f in files]}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        mdata = json.dumps(manifest, indent=2).encode()
        ti = tarfile.TarInfo(MANIFEST)
        ti.size, ti.mode, ti.mtime = len(mdata), 0o600, manifest['created']
        tar.addfile(ti, io.BytesIO(mdata))
        for f in files:
            ti = tarfile.TarInfo(f['path'])
            ti.size, ti.mode, ti.mtime = f['bytes'], f['mode'], manifest['created']
            tar.addfile(ti, io.BytesIO(f['_data']))
    return buf.getvalue(), manifest


# ─── Encryption ───────────────────────────────────────────────────────

def _openssl_enc(data, passphrase, decrypt=False):
    r, w = os.pipe()
    os.write(w, passphrase.encode() + b'\n')
    os.close(w)
    try:
        p = subprocess.run(['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', str(PBKDF2_ITER), '-md', 'sha256',
                            '-salt', '-pass', 'fd:%d' % r] + (['-d'] if decrypt else []),
                           input=data, capture_output=True, pass_fds=(r,), timeout=60)
    finally:
        os.close(r)
    if p.returncode != 0:
        raise ValueError('openssl: ' + p.stderr.decode(errors='replace').strip()[-200:])
    return p.stdout


def _mac_key(passphrase, salt):
    return hashlib.pbkdf2_hmac('sha256', passphrase.encode(), salt, PBKDF2_ITER, 32)


def encrypt(data, passphrase):
    salt = secrets.token_bytes(16)
    ct = _openssl_enc(data, passphrase)
    tag = hmac.new(_mac_key(passphrase, salt), ct, 'sha256').digest()
    return MAGIC + salt + tag + ct


def is_encrypted(blob):
    return blob[:len(MAGIC)] == MAGIC


def decrypt(blob, passphrase):
    if not is_encrypted(blob):
        raise ValueError('not an encrypted bundle')
    salt, tag, ct = blob[9:25], blob[25:57], blob[57:]
    if not passphrase:
        raise ValueError('this bundle is encrypted — passphrase required')
    if not hmac.compare_digest(hmac.new(_mac_key(passphrase, salt), ct, 'sha256').digest(), tag):
        raise ValueError('wrong passphrase or corrupted bundle')
    return _openssl_enc(ct, passphrase, decrypt=True)


# ─── Reading a bundle back ────────────────────────────────────────────

def _safe_member(name):
    return (name and not name.startswith('/') and '..' not in name.split('/')
            and not name.startswith('.') and len(name) < 512)


def read_bundle(blob, passphrase=''):
    """→ (manifest, {path: (bytes, mode)}) with every path validated against
    the manifest AND the allowlist. Raises ValueError on anything off."""
    if len(blob) > MAX_BUNDLE:
        raise ValueError('bundle too large')
    if is_encrypted(blob):
        blob = decrypt(blob, passphrase)
    try:
        tar = tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz')
    except (tarfile.TarError, OSError) as e:
        raise ValueError('not a bundle: %s' % e)
    members = {m.name: m for m in tar.getmembers() if m.isfile()}
    if MANIFEST not in members:
        raise ValueError('bundle has no manifest')
    manifest = json.loads(tar.extractfile(members[MANIFEST]).read().decode())
    if manifest.get('format') != 1:
        raise ValueError('unsupported bundle format')
    allowed = set()
    for _id in manifest.get('items') or []:
        if _id not in ITEM_BY_ID:
            raise ValueError('unknown item %r' % _id)
        _, rel, kind, _s, _d = ITEM_BY_ID[_id]
        allowed.add(_id)
    files = {}
    for f in manifest.get('files') or []:
        path, item = f.get('path'), f.get('item')
        if item not in allowed or not _safe_member(path) or path not in members:
            raise ValueError('manifest lists an invalid file: %r' % path)
        _, rel, kind, _s, _d = ITEM_BY_ID[item]
        if kind == 'tls':
            if path not in ('certs/dashboard.crt', 'certs/dashboard.key'):
                raise ValueError('bad tls path %r' % path)
        elif kind == 'file':
            if path != rel:
                raise ValueError('bad path %r for %s' % (path, item))
        elif not path.startswith(rel + '/'):
            raise ValueError('bad path %r for %s' % (path, item))
        data = tar.extractfile(members[path]).read()
        if hashlib.sha256(data).hexdigest() != f.get('sha256'):
            raise ValueError('checksum mismatch for %s' % path)
        mode = int(f.get('mode') or 0o600) & 0o777
        files[path] = (data, mode, item)
    return manifest, files


def _dest(path, item):
    kind = ITEM_BY_ID[item][2]
    if kind == 'tls':
        return TLS_KEY if path.endswith('.key') else TLS_CERT
    return _abs(path)


def inspect_bundle(blob, passphrase=''):
    manifest, files = read_bundle(blob, passphrase)
    rows = []
    for path, (data, mode, item) in sorted(files.items()):
        dest = _dest(path, item)
        exists = os.path.isfile(dest)
        same = False
        if exists:
            with open(dest, 'rb') as f:
                same = hashlib.sha256(f.read()).hexdigest() == hashlib.sha256(data).hexdigest()
        rows.append({'path': path, 'item': item, 'bytes': len(data), 'mode': '%o' % mode,
                     'status': 'same' if same else 'differs' if exists else 'new'})
    return {'manifest': {k: v for k, v in manifest.items() if k != 'files'}, 'files': rows}


def apply_bundle(blob, passphrase='', items=None):
    """Write the bundle's files (all items, or the given ids) into place.
    Returns {written, skipped, restart_recommended}."""
    manifest, files = read_bundle(blob, passphrase)
    want = set(manifest['items']) if items is None else {i for i in items if i in ITEM_BY_ID}
    written, restart = [], False
    for path, (data, mode, item) in sorted(files.items()):
        if item not in want:
            continue
        dest = _dest(path, item)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = '%s.tmp.%d' % (dest, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        written.append(path)
        if item == 'tls':
            restart = True
    return {'written': written, 'skipped': sorted(set(manifest['items']) - want),
            'restart_recommended': restart, 'source_host': manifest.get('hostname'),
            'source_version': manifest.get('app_version')}


# ─── Routes (admin-only: the bundle IS the credentials) ───────────────

def _admin_or_403():
    return None if _is_admin() else err('Admin required', 403)


@bp.route('/api/config/inventory')
def config_inventory():
    r = _admin_or_403()
    if r:
        return r
    return jsonify({'items': inventory(), 'hostname': socket.gethostname(), 'app_version': APP_VERSION})


@bp.route('/api/config/export', methods=['POST'])
def config_export():
    data = request.get_json(silent=True) or {}
    passphrase = str(data.get('passphrase') or '')
    include = data.get('include')
    if include is not None and not isinstance(include, list):
        return err('include must be a list of item ids')
    blob, manifest = build_bundle(include)
    name = 'nexus-config-%s-%s.tar.gz' % (manifest['hostname'], time.strftime('%Y%m%d-%H%M%S'))
    if passphrase:
        if len(passphrase) < 8:
            return err('Passphrase must be at least 8 characters')
        blob, name = encrypt(blob, passphrase), name + '.enc'
    return Response(blob, mimetype='application/octet-stream',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % name,
                             'X-Nexus-Files': str(len(manifest['files']))})


def _bundle_from_request():
    data = request.get_json(silent=True) or {}
    try:
        blob = base64.b64decode(data.get('bundle') or '', validate=True)
    except (ValueError, TypeError):
        raise ValueError('bundle must be base64')
    if not blob:
        raise ValueError('no bundle')
    return blob, str(data.get('passphrase') or ''), data


@bp.route('/api/config/inspect', methods=['POST'])
def config_inspect():
    try:
        blob, passphrase, _ = _bundle_from_request()
        return jsonify(inspect_bundle(blob, passphrase))
    except ValueError as e:
        return err(str(e))


@bp.route('/api/config/restore', methods=['POST'])
def config_restore():
    try:
        blob, passphrase, data = _bundle_from_request()
        if data.get('confirm') is not True:
            return err('confirm:true required — this overwrites live configuration')
        items = data.get('items')
        if items is not None and not isinstance(items, list):
            return err('items must be a list of item ids')
        res = apply_bundle(blob, passphrase, items)
        res['success'] = True
        return jsonify(res)
    except ValueError as e:
        return err(str(e))


# ─── CLI (the fresh-install case: no admin session yet) ───────────────

def cli_config_export(argv):
    """python app.py config-export <file> [--items a,b] ; passphrase from
    DASHBOARD_CONFIG_PASSPHRASE (empty = plain)."""
    if len(argv) < 3:
        print('usage: app.py config-export <file> [--items id,id]')
        return 2
    items = None
    if '--items' in argv:
        items = argv[argv.index('--items') + 1].split(',')
    blob, manifest = build_bundle(items)
    pw = os.environ.get('DASHBOARD_CONFIG_PASSPHRASE', '')
    if pw:
        blob = encrypt(blob, pw)
    with open(argv[2], 'wb') as f:
        f.write(blob)
    os.chmod(argv[2], 0o600)
    print('wrote %s: %d files from %d items%s' % (argv[2], len(manifest['files']), len(manifest['items']),
                                                 ' (encrypted)' if pw else ''))
    return 0


def cli_config_restore(argv):
    """python app.py config-restore <file> [--items a,b]"""
    if len(argv) < 3:
        print('usage: app.py config-restore <file> [--items id,id]')
        return 2
    items = None
    if '--items' in argv:
        items = argv[argv.index('--items') + 1].split(',')
    with open(argv[2], 'rb') as f:
        blob = f.read()
    try:
        res = apply_bundle(blob, os.environ.get('DASHBOARD_CONFIG_PASSPHRASE', ''), items)
    except ValueError as e:
        print('refused: %s' % e)
        return 1
    print('restored %d files from %s (%s)%s' % (len(res['written']), res['source_host'], res['source_version'],
                                              ' — restart the service' if res['restart_recommended'] else ''))
    return 0
