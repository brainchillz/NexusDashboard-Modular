"""Nexus Containers — extracted from the LXD-Console app.py (Stage 3 port).
Logic unchanged except: shared core imports, RE_POOL/RE_DEVNAME renamed to
RE_CT_* (the dashboard owns those names with different patterns), and the
LXDWEB_SOCKET env override renamed DASHBOARD_LXD_SOCKET."""
import os
import re
import json
import time
import socket
import threading
import http.client
import urllib.request
import urllib.error
from datetime import datetime
from flask import Blueprint, jsonify, request, g, Response

from ...core.config import APP_DIR, write_json_atomic
from ...core.runcmd import run, run_safe, err
from ...core.registry import load_disabled_modules
from .client import (
    DAEMON, SOCKET_PATH, DEFAULT_IMAGE_REMOTE, IMAGE_REMOTES, IMAGE_REMOTE_URLS,
    RE_INSTANCE, RE_CT_NETWORK, RE_CT_POOL, RE_PROFILE, RE_PROJECT,
    RE_IMAGE_ALIAS, RE_CT_DEVNAME, RE_SNAPNAME, RE_FINGERPRINT, RE_PROXY_ADDR,
    INSTANCE_TYPES, STATE_ACTIONS, CONFIG_EDIT_KEYS, _CT_FTYPES, _VM_FTYPES,
    _ARCH_MAP, valid_instance_name, LxdError, _UnixHTTPConnection,
    lxd_raw, lxd_request, _wait_operation, _lxd_error_response, _host_arch,
    _nic_device_for,
)

bp = Blueprint('images', __name__)

_ss_cache = {}  # url -> (ts, products)
_SS_TTL = 600


def _get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'lxd-console'})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def _fetch_stream_products(server):
    """Return the raw simplestreams products dict for a server, handling BOTH
    layouts: the flat `streams/v1/images.json` used by the linuxcontainers /
    images.lxd.canonical.com servers, AND the `index.json → …:download.json`
    layout used by cloud-images.ubuntu.com (the `ubuntu:` remote)."""
    base = server.rstrip('/')
    try:
        return _get_json(base + '/streams/v1/images.json').get('products') or {}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # Flat file absent — resolve the products file from the index.
    idx = _get_json(base + '/streams/v1/index.json').get('index') or {}
    # The downloadable-images stream is the one with datatype 'image-downloads'.
    # (Every entry is format 'products:1.0'; the cloud image-ID streams — aws/gce/
    # … — are datatype 'image-ids' and carry no downloadable rootfs/disk items.)
    path = None
    for name, meta in idx.items():
        if meta.get('datatype') == 'image-downloads':
            path = meta.get('path')
            break
    if not path:
        raise LxdError(502, 'No downloadable-image stream found on image server')
    return _get_json(base + '/' + path.lstrip('/')).get('products') or {}


def _variant_of(key, p):
    """simplestreams variant — explicit field, else the second token of the
    product key (e.g. com.ubuntu.cloud:*server*:22.04:amd64)."""
    if p.get('variant'):
        return p['variant']
    parts = key.split(':')
    return parts[1] if len(parts) > 2 else ''


def _simplestreams_products(server):
    now = time.time()
    hit = _ss_cache.get(server)
    if hit and now - hit[0] < _SS_TTL:
        return hit[1]
    products = _fetch_stream_products(server)
    out = []
    for key, p in products.items():
        versions = p.get('versions') or {}
        if not versions:
            continue
        latest = sorted(versions.keys())[-1]
        ftypes = set((versions[latest].get('items') or {}).keys())
        types = []
        if any(f in ftypes for f in _CT_FTYPES):
            types.append('container')
        if any(f in ftypes for f in _VM_FTYPES):
            types.append('virtual-machine')
        if not types:
            continue
        aliases = [a.strip() for a in (p.get('aliases') or '').split(',') if a.strip()]
        # Prefer the numeric version (e.g. Ubuntu "22.04") for display; fall back
        # to the release codename used by the linuxcontainers-style servers.
        release = p.get('version') or p.get('release', '')
        out.append({
            'product': key,
            'os': p.get('os', ''),
            'release': release,
            'release_title': p.get('release_title', ''),
            'arch': p.get('arch', ''),
            'variant': _variant_of(key, p),
            'aliases': aliases,
            'alias': aliases[0] if aliases else '',
            'types': types,
        })
    out.sort(key=lambda x: (x['os'].lower(), str(x['release']), x['variant']))
    _ss_cache[server] = (now, out)
    return out


@bp.route('/api/images/remotes')
def images_remotes():
    return jsonify({'remotes': IMAGE_REMOTES, 'default': DEFAULT_IMAGE_REMOTE,
                    'host_arch': _host_arch()})


@bp.route('/api/images/remote')
def images_remote():
    server = (request.args.get('server') or DEFAULT_IMAGE_REMOTE).strip()
    if server not in IMAGE_REMOTE_URLS:
        return err('Unknown image server')
    try:
        products = _simplestreams_products(server)
    except Exception as e:
        return err(f'Cannot reach image server: {e}', 502)
    arch = request.args.get('arch')
    itype = request.args.get('type')
    if arch:
        products = [p for p in products if p['arch'] == arch]
    if itype in INSTANCE_TYPES:
        products = [p for p in products if itype in p['types']]
    return jsonify({'server': server, 'count': len(products), 'images': products})


@bp.route('/api/images')
def images_local():
    try:
        imgs = lxd_request('GET', '/1.0/images?recursion=1')
    except LxdError as e:
        return _lxd_error_response(e)
    out = []
    for im in imgs:
        props = im.get('properties') or {}
        fp = im.get('fingerprint', '')
        out.append({
            'fingerprint': fp[:12],
            'fingerprint_full': fp,
            'aliases': [a.get('name') for a in im.get('aliases', [])],
            'description': props.get('description', ''),
            'os': props.get('os', ''),
            'release': props.get('release', ''),
            'architecture': im.get('architecture', ''),
            'type': im.get('type', 'container'),
            'size': im.get('size', 0),
            'uploaded_at': im.get('uploaded_at', ''),
            'cached': im.get('cached', False),
        })
    return jsonify(out)


@bp.route('/api/images/<fp>', methods=['DELETE'])
def image_delete(fp):
    if not RE_FINGERPRINT.match(fp):
        return err('Invalid fingerprint')
    try:
        lxd_request('DELETE', f'/1.0/images/{fp}', wait=True)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


@bp.route('/api/images/copy', methods=['POST'])
def image_copy():
    """Pre-seed a remote image into the local cache (pull without launching)."""
    data = request.get_json() or {}
    server = (data.get('server') or DEFAULT_IMAGE_REMOTE).strip()
    alias = (data.get('alias') or '').strip()
    if server not in IMAGE_REMOTE_URLS:
        return err('Unknown image server')
    if not RE_IMAGE_ALIAS.match(alias):
        return err('Invalid image alias')
    body = {'source': {'type': 'image', 'mode': 'pull', 'protocol': 'simplestreams',
                       'server': server, 'alias': alias}}
    try:
        lxd_request('POST', '/1.0/images', body, wait=True, wait_timeout=1800)
    except LxdError as e:
        return _lxd_error_response(e)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════
#  Storage pools / profiles / networks  (feed the create dialog)
# ═══════════════════════════════════════════════════════════════════════



# ─── Module descriptor ─────────────────────────────────────────────────
MODULE = {'id': 'images', 'label': 'Images', 'category': 'LXD / Incus',
          'blueprint': bp}
