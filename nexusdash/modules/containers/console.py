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


def _open_vga_console(name, width=1280, height=800):
    """Start a SPICE (VGA) console operation on a VM. Returns (op_id, fd0 secret).

    The daemon returns the SAME `class:websocket` op shape as the serial console
    (`metadata.fds` with a '0' secret); that fd carries the raw SPICE protocol
    stream the browser client (spice-html5) speaks. VM-only — a container has no
    framebuffer, and asking for a vga console on one errors."""
    body = {'type': 'vga', 'width': width, 'height': height}
    status, raw = lxd_raw('POST', f'/1.0/instances/{name}/console', body)
    doc = json.loads(raw or b'{}')
    if doc.get('type') == 'error':
        raise LxdError(doc.get('error_code') or status, doc.get('error', 'console error'))
    op = (doc.get('operation') or '').rstrip('/').split('/')[-1]
    fds = ((doc.get('metadata') or {}).get('metadata') or {}).get('fds') or {}
    return op, fds.get('0')


def ws_vga_console(ws, name):
    """Bridge spice-html5 in the browser to a VM's SPICE (VGA) console.

    Unlike ws_console this is a RAW byte pump: spice-html5 speaks the binary
    SPICE protocol directly, with NO JSON stdin/resize framing. SPICE opens each
    of its channels (main/display/inputs/cursor) as a SEPARATE websocket, so
    every connection here allocates its OWN fresh `type:vga` console op — the
    daemon proxies each to a new connection into qemu's SPICE server, which is
    exactly how SPICE multiplexes. Admin-only (a console is write access) and
    module-gated, same as the serial console; VM instances only."""
    if getattr(g, 'identity_role', None) != 'admin':
        ws.close()
        return
    # Not blueprint-scoped, so the central module gate can't see it.
    if 'instances' in load_disabled_modules():
        ws.close()
        return
    if not valid_instance_name(name):
        ws.close()
        return
    try:
        inst = lxd_request('GET', f'/1.0/instances/{name}')
        if inst.get('type') != 'virtual-machine':
            ws.close()
            return
        if (inst.get('status') or '').lower() != 'running':
            ws.close()
            return
        op, sec0 = _open_vga_console(name)
    except LxdError:
        ws.close()
        return
    if not sec0:
        ws.close()
        return
    try:
        dws = _daemon_ws(op, sec0)
    except Exception:
        ws.close()
        return

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

    threading.Thread(target=pump_daemon_to_browser, daemon=True).start()
    try:
        while not stop.is_set():
            msg = ws.receive(timeout=30)
            if msg is None:
                continue
            dws.send_binary(msg if isinstance(msg, (bytes, bytearray))
                            else msg.encode('utf-8'))
    except Exception:
        pass
    finally:
        stop.set()
        try:
            dws.close()
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass


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
#  Graphical (SPICE/VGA) console page
# ═══════════════════════════════════════════════════════════════════════
bp = Blueprint('ct_console', __name__)


def _render_vga_page(name):
    """A self-contained SPICE console page: loads the vendored spice-html5 ES
    modules and points SpiceMainConn at this node's /ws/vga/<name> bridge. Kept
    off the main SPA because spice-html5 is ES-module-only; opens in its own
    window. `name` is already valid_instance_name-checked (alnum/-/. — no quote
    or angle-bracket can reach the markup), and JSON-encoded into the script."""
    nm_js = json.dumps(name)
    nm_txt = (name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Console — {nm_txt}</title>
<style>
  :root {{ --bg:#1c1e22; --panel:#26292e; --fg:#e8e6e3; --accent:#c8642d; --line:#3a3d42; }}
  * {{ box-sizing:border-box; }}
  html,body {{ margin:0; height:100%; background:var(--bg); color:var(--fg);
    font:13px/1.4 system-ui,sans-serif; }}
  #bar {{ display:flex; align-items:center; gap:12px; padding:6px 12px;
    background:var(--panel); border-bottom:2px solid var(--accent); }}
  #bar b {{ color:var(--accent); }}
  #bar .name {{ font-family:ui-monospace,monospace; }}
  #bar button {{ background:#31353b; color:var(--fg); border:1px solid var(--line);
    border-radius:4px; padding:4px 10px; cursor:pointer; font-size:12px; }}
  #bar button:hover {{ border-color:var(--accent); }}
  #status {{ margin-left:auto; opacity:.8; }}
  #spice-area {{ position:absolute; top:37px; left:0; right:0; bottom:0; overflow:auto; }}
  #spice-screen {{ display:inline-block; }}
  /* spice-html5 writes verbose per-channel notices here (incl. channels the JS
     client doesn't implement — usbredir/smartcard/webdav — which are harmless);
     hidden by default, shown only via the Log toggle. Real connection state is
     surfaced in the status bar. */
  #message-div {{ display:none; position:fixed; bottom:8px; left:8px; max-width:60%;
    max-height:40%; overflow:auto; color:#e5a; font-size:12px;
    background:rgba(0,0,0,.6); padding:4px 8px; border-radius:4px; }}
  #message-div.show {{ display:block; }}
  #debug-div {{ display:none; }}
</style>
</head><body>
<div id="bar">
  <b>SPICE</b><span class="name">{nm_txt}</span>
  <button id="cad">Ctrl-Alt-Del</button>
  <button id="logtoggle">Log</button>
  <span id="status">connecting…</span>
</div>
<div id="spice-area"><div id="spice-screen" class="spice-screen"></div></div>
<div id="message-div"></div>
<div id="debug-div"></div>
<script type="module">
import * as SpiceHtml5 from '/static/vendor/spice/src/main.js';
const NAME = {nm_js};
const statusEl = document.getElementById('status');
let sc = null;
function setStatus(t){{ statusEl.textContent = t; }}
function onerror(e){{
  setStatus('disconnected');
  if (sc) {{ try {{ sc.stop(); }} catch(_e) {{}} sc = null; }}
}}
function onagent(){{
  window.addEventListener('resize', SpiceHtml5.handle_resize);
  SpiceHtml5.resize_helper(this);
}}
function connect(){{
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  const uri = proto + location.host + '/ws/vga/' + encodeURIComponent(NAME);
  try {{
    sc = new SpiceHtml5.SpiceMainConn({{ uri, screen_id:'spice-screen',
      dump_id:'debug-div', message_id:'message-div', password:'',
      onerror, onagent, onsuccess: () => setStatus('connected') }});
  }} catch(e) {{ setStatus('error: ' + e); }}
}}
document.getElementById('cad').addEventListener('click', () => {{
  if (sc) SpiceHtml5.sendCtrlAltDel(sc);
}});
document.getElementById('logtoggle').addEventListener('click', () => {{
  document.getElementById('message-div').classList.toggle('show');
}});
window.addEventListener('beforeunload', () => {{ if (sc) {{ try {{ sc.stop(); }} catch(_e) {{}} }} }});
connect();
</script>
</body></html>"""


@bp.route('/console/vga/<name>')
def vga_console_page(name):
    """Serve the graphical-console page. Admin-only + instances-module-gated
    (the ws re-checks both — this is the friendlier front door). require_login
    already guarantees an authenticated session (not a PUBLIC_ENDPOINT)."""
    if getattr(g, 'identity_role', None) != 'admin':
        return Response('Administrator access required', status=403)
    if 'instances' in load_disabled_modules():
        return Response("module 'instances' is disabled on this node", status=404)
    if not valid_instance_name(name):
        return Response('invalid instance name', status=400)
    return Response(_render_vga_page(name), mimetype='text/html')


def register_ws(sock):
    """Attach the console websockets to a flask-sock instance (called by
    create_app only when the instances module is enabled at boot)."""
    sock.route('/ws/console/<name>')(ws_console)
    sock.route('/ws/vga/<name>')(ws_vga_console)
