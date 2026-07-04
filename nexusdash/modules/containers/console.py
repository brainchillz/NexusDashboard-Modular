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
import websocket as wsclient
def _open_console_operation(name, itype, width, height, mode='shell'):
    """Start a console/exec operation. Returns (op_id, fd0 secret, control secret).

    mode='shell' (default) runs an interactive root shell via exec — for
    containers, and for VMs via the lxd-agent — so there is NO OS login prompt.
    mode='serial' attaches the raw serial console (the guest's getty login),
    which is what you want to watch boot / run an installer. A VM whose agent
    isn't up yet (still booting / installing) automatically falls back to serial.
    """
    def _exec():
        body = {'command': ['/bin/sh'], 'wait-for-websocket': True, 'interactive': True,
                'environment': {'TERM': 'xterm-256color'}, 'width': width, 'height': height}
        return lxd_raw('POST', f'/1.0/instances/{name}/exec', body)

    def _serial():
        body = {'width': width, 'height': height, 'type': 'console'}
        return lxd_raw('POST', f'/1.0/instances/{name}/console', body)

    if mode == 'serial':
        status, raw = _serial()
    else:
        status, raw = _exec()
        first = json.loads(raw or b'{}')
        if first.get('type') == 'error' and itype == 'virtual-machine':
            status, raw = _serial()   # no agent yet → serial login
    doc = json.loads(raw or b'{}')
    if doc.get('type') == 'error':
        raise LxdError(doc.get('error_code') or status, doc.get('error', 'console error'))
    op = (doc.get('operation') or '').rstrip('/').split('/')[-1]
    fds = ((doc.get('metadata') or {}).get('metadata') or {}).get('fds') or {}
    return op, fds.get('0'), fds.get('control')


def _daemon_ws(op, secret):
    """Open a websocket to a daemon operation over the Unix socket."""
    us = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    us.connect(SOCKET_PATH)
    # Host is derived from the URL ("lxd"); do NOT add an explicit Host header —
    # the daemon's Go HTTP server 400s on a duplicate Host header.
    url = f'ws://lxd/1.0/operations/{op}/websocket?secret={secret}'
    return wsclient.create_connection(url, socket=us, timeout=None,
                                      enable_multithread=True)


def ws_console(ws, name):
    """Bridge an xterm.js session in the browser to the instance console.

    require_login already authenticated the handshake (session cookie or token);
    a console is effectively write access, so require admin explicitly. Browser
    → server messages are JSON: {"type":"stdin","data":...} or
    {"type":"resize","width":W,"height":H}. Server → browser frames are the raw
    console bytes, which xterm writes directly.
    """
    if getattr(g, 'identity_role', None) != 'admin':
        try:
            ws.send(json.dumps({'type': 'error', 'error': 'Administrator access required'}))
        finally:
            ws.close()
        return
    # The ws route is not blueprint-scoped, so the central module gate can't
    # see it — enforce the instances-module toggle here.
    if 'instances' in load_disabled_modules():
        try:
            ws.send(json.dumps({'type': 'error',
                                'error': "module 'instances' is disabled on this node"}))
        finally:
            ws.close()
        return
    if not valid_instance_name(name):
        ws.close()
        return
    mode = 'serial' if request.args.get('mode') == 'serial' else 'shell'
    try:
        inst = lxd_request('GET', f'/1.0/instances/{name}')
        itype = inst.get('type', 'container')
        if (inst.get('status') or '').lower() != 'running':
            ws.send(json.dumps({'type': 'error', 'error': 'Instance is not running'}))
            ws.close()
            return
        op, sec0, secctl = _open_console_operation(name, itype, 80, 24, mode)
    except LxdError as e:
        try:
            ws.send(json.dumps({'type': 'error', 'error': e.message}))
        finally:
            ws.close()
        return
    if not sec0:
        ws.send(json.dumps({'type': 'error', 'error': 'Daemon did not return a console channel'}))
        ws.close()
        return

    try:
        dws = _daemon_ws(op, sec0)
    except Exception as e:
        ws.send(json.dumps({'type': 'error', 'error': f'Console connect failed: {e}'}))
        ws.close()
        return
    cws = None
    if secctl:
        try:
            cws = _daemon_ws(op, secctl)
        except Exception:
            cws = None

    stop = threading.Event()

    def pump_daemon_to_browser():
        try:
            while not stop.is_set():
                data = dws.recv()
                if data is None or data == '':
                    break
                if isinstance(data, str):
                    data = data.encode('utf-8', 'replace')
                if data:
                    ws.send(data)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                ws.close()
            except Exception:
                pass

    t = threading.Thread(target=pump_daemon_to_browser, daemon=True)
    t.start()

    try:
        while not stop.is_set():
            msg = ws.receive(timeout=30)
            if msg is None:
                continue
            try:
                obj = json.loads(msg) if isinstance(msg, str) else None
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict) and obj.get('type') == 'resize':
                if cws:
                    try:
                        cws.send(json.dumps({'command': 'window-resize',
                                             'args': {'width': str(int(obj.get('width', 80))),
                                                      'height': str(int(obj.get('height', 24)))}}))
                    except Exception:
                        pass
                continue
            if isinstance(obj, dict) and obj.get('type') == 'stdin':
                payload = obj.get('data', '')
                dws.send_binary(payload.encode('utf-8') if isinstance(payload, str) else payload)
                continue
            # Fallback: forward raw bytes as stdin.
            dws.send_binary(msg.encode('utf-8') if isinstance(msg, str) else msg)
    except Exception:
        pass
    finally:
        stop.set()
        for c in (dws, cws):
            try:
                if c:
                    c.close()
            except Exception:
                pass
        try:
            ws.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#  Static / PWA
# ═══════════════════════════════════════════════════════════════════════



def register_ws(sock):
    """Attach the console websocket to a flask-sock instance (called by
    create_app only when the instances module is enabled at boot)."""
    sock.route('/ws/console/<name>')(ws_console)
