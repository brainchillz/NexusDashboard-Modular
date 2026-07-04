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
# Candidate sockets, most specific first. LXD (snap or deb) or Incus.
_SOCKET_CANDIDATES = [
    ('lxd', '/var/snap/lxd/common/lxd/unix.socket', 'https://images.lxd.canonical.com'),
    ('lxd', '/var/lib/lxd/unix.socket', 'https://images.lxd.canonical.com'),
    ('incus', '/var/lib/incus/unix.socket', 'https://images.linuxcontainers.org'),
]


def _daemon_detect():
    override = os.environ.get('DASHBOARD_LXD_SOCKET')
    if override and os.path.exists(override):
        daemon = 'incus' if 'incus' in override else 'lxd'
        remote = ('https://images.linuxcontainers.org' if daemon == 'incus'
                  else 'https://images.lxd.canonical.com')
        return daemon, override, remote
    for daemon, path, remote in _SOCKET_CANDIDATES:
        if os.path.exists(path):
            return daemon, path, remote
    # Fall back to LXD snap path even if absent, so the app still starts and the
    # UI can report the daemon as unreachable rather than crashing at import.
    return 'lxd', _SOCKET_CANDIDATES[0][1], _SOCKET_CANDIDATES[0][2]


DAEMON, SOCKET_PATH, DEFAULT_IMAGE_REMOTE = _daemon_detect()

# simplestreams remotes offered in the image browser.
IMAGE_REMOTES = {
    'lxd': [
        {'name': 'images', 'url': 'https://images.lxd.canonical.com'},
        {'name': 'ubuntu', 'url': 'https://cloud-images.ubuntu.com/releases'},
    ],
    'incus': [
        {'name': 'images', 'url': 'https://images.linuxcontainers.org'},
    ],
}[DAEMON]
IMAGE_REMOTE_URLS = {r['url'] for r in IMAGE_REMOTES}

# Instance names: start with a letter, letters/digits/hyphen, <=63, no trailing '-'.
RE_INSTANCE = re.compile(r'^[a-zA-Z][a-zA-Z0-9-]{0,62}\Z')
RE_CT_NETWORK = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}\Z')
RE_CT_POOL = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}\Z')
RE_PROFILE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}\Z')
RE_PROJECT = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}\Z')
RE_IMAGE_ALIAS = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_./:+-]{0,127}\Z')
INSTANCE_TYPES = {'container', 'virtual-machine'}
STATE_ACTIONS = {'start', 'stop', 'restart', 'freeze', 'unfreeze'}

# simplestreams item ftypes that indicate container vs VM support.
_CT_FTYPES = ('rootfs.squashfs', 'root.tar.xz', 'squashfs')
_VM_FTYPES = ('disk.qcow2', 'disk-kvm.img', 'disk1.img')

# Map kernel arch → simplestreams/LXD arch names.
_ARCH_MAP = {'x86_64': 'amd64', 'aarch64': 'arm64', 'armv7l': 'armhf',
             'ppc64le': 'ppc64el', 's390x': 's390x', 'riscv64': 'riscv64'}


def valid_instance_name(name):
    return bool(name) and bool(RE_INSTANCE.match(name)) and not name.endswith('-')


RE_CT_DEVNAME = re.compile(r'^[a-zA-Z0-9._-]{1,63}\Z')
RE_SNAPNAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}\Z')
RE_FINGERPRINT = re.compile(r'^[0-9a-f]{4,64}\Z')
# proxy device listen/connect address, e.g. tcp:0.0.0.0:80 / udp:127.0.0.1:53
RE_PROXY_ADDR = re.compile(r'^(tcp|udp):[0-9A-Za-z.\[\]:_*-]+:\d{1,5}\Z')
# Instance config keys the UI may edit on an existing instance.
CONFIG_EDIT_KEYS = {'limits.cpu', 'limits.memory', 'boot.autostart',
                    'security.nesting', 'security.privileged'}


class LxdError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status or 500
        self.message = message


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that dials a Unix domain socket instead of TCP."""
    def __init__(self, socket_path, timeout=None):
        super().__init__('localhost', timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            s.settimeout(self.timeout)
        s.connect(self._socket_path)
        self.sock = s


def lxd_raw(method, path, body=None, timeout=60):
    """One HTTP round-trip to the daemon over the Unix socket. Returns
    (status_code, raw_bytes). Raises LxdError on transport failure."""
    conn = _UnixHTTPConnection(SOCKET_PATH, timeout=timeout)
    data = None
    headers = {'Host': 'lxd', 'Accept': 'application/json'}
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw
    except (OSError, http.client.HTTPException) as e:
        raise LxdError(502, f'Cannot reach {DAEMON} daemon at {SOCKET_PATH}: {e}')
    finally:
        conn.close()


def lxd_request(method, path, body=None, wait=True, wait_timeout=300):
    """REST call returning the response `metadata`. Async operations are waited
    on (up to wait_timeout) unless wait=False; a failed operation raises
    LxdError with the daemon's message."""
    status, raw = lxd_raw(method, path, body)
    try:
        doc = json.loads(raw or b'{}')
    except ValueError:
        raise LxdError(status, (raw[:300].decode('utf-8', 'replace') or 'Malformed response'))
    if doc.get('type') == 'error':
        raise LxdError(doc.get('error_code') or status, doc.get('error', 'daemon error'))
    if doc.get('type') == 'async' and wait:
        opid = (doc.get('operation') or '').rstrip('/').split('/')[-1]
        return _wait_operation(opid, wait_timeout)
    return doc.get('metadata')


def _wait_operation(opid, timeout=300):
    if not opid:
        return {}
    st, raw = lxd_raw('GET', f'/1.0/operations/{opid}/wait?timeout={timeout}', timeout=timeout + 15)
    try:
        doc = json.loads(raw or b'{}')
    except ValueError:
        raise LxdError(st, 'Malformed operation response')
    if doc.get('type') == 'error':
        raise LxdError(doc.get('error_code') or st, doc.get('error', 'operation error'))
    meta = doc.get('metadata') or {}
    if meta.get('status_code', 200) >= 400 or meta.get('err'):
        raise LxdError(meta.get('status_code') or 500, meta.get('err') or 'operation failed')
    return meta


def _lxd_error_response(e):
    return jsonify({'success': False, 'error': e.message}), (e.status if 400 <= e.status < 600 else 500)


def _host_arch():
    try:
        return _ARCH_MAP.get(os.uname().machine, os.uname().machine)
    except Exception:
        return 'amd64'


# ═══════════════════════════════════════════════════════════════════════
#  Server info
# ═══════════════════════════════════════════════════════════════════════

def _nic_device_for(net_name, dev='eth0'):
    """Build a NIC device attaching to a network. Managed networks (bridge,
    macvlan, …) use the `network` key so LXD derives the nictype; an unmanaged
    Linux bridge is attached with nictype=bridged + parent."""
    net_obj = lxd_request('GET', f'/1.0/networks/{net_name}')
    if net_obj.get('managed'):
        return {'type': 'nic', 'name': dev, 'network': net_name}
    if net_obj.get('type') == 'bridge':
        return {'type': 'nic', 'name': dev, 'nictype': 'bridged', 'parent': net_name}
    raise LxdError(400, f'Cannot attach an instance to a {net_obj.get("type")} network')


