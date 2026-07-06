"""Docker exec terminal — bridges an xterm.js websocket to a hijacked
Engine-API exec stream.

Docker has no per-operation websockets (unlike LXD): POST /exec/{id}/start
with `Upgrade: tcp` flips the HTTP connection into a raw bidirectional byte
stream (unframed, because the exec is created with Tty=true). Browser-side
protocol matches the LXD console: JSON {"type":"stdin"|"resize"} up, raw
bytes down. The ws route is not blueprint-scoped, so the handler re-checks
the `docker` module toggle on every connection (same as the LXD console)."""
import json
import socket
import threading
from flask import g

from ..core.registry import load_disabled_modules
from .docker import DOCKER_SOCKET, RE_DK_NAME, DockerError, docker_request

# The shell probes for bash and falls back to sh — one exec works everywhere
# from full distros to busybox images.
DK_SHELL_CMD = ['/bin/sh', '-c', 'command -v bash >/dev/null 2>&1 && exec bash || exec sh']


def _exec_create(cid):
    body = {'AttachStdin': True, 'AttachStdout': True, 'AttachStderr': True,
            'Tty': True, 'Env': ['TERM=xterm-256color'], 'Cmd': DK_SHELL_CMD}
    return docker_request('POST', '/containers/%s/exec' % cid, body=body)['Id']


def _parse_hijack_head(buf):
    """Split a raw HTTP response head from stream bytes that may already
    follow it. Returns (status_code, leftover_stream_bytes)."""
    head, sep, leftover = buf.partition(b'\r\n\r\n')
    if not sep:
        raise DockerError(502, 'malformed exec handshake')
    try:
        code = int(head.split(b'\r\n', 1)[0].split()[1])
    except (IndexError, ValueError):
        raise DockerError(502, 'malformed exec status line')
    return code, leftover


def _exec_start_hijack(exec_id):
    """Start the exec and hijack the connection. Returns (unix_socket,
    leftover_bytes) — the socket is a raw duplex stream from here on."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(DOCKER_SOCKET)
    payload = json.dumps({'Detach': False, 'Tty': True}).encode()
    s.sendall(b'POST /exec/' + exec_id.encode() + b'/start HTTP/1.1\r\n'
              b'Host: docker\r\n'
              b'Content-Type: application/json\r\n'
              b'Connection: Upgrade\r\n'
              b'Upgrade: tcp\r\n'
              b'Content-Length: ' + str(len(payload)).encode() + b'\r\n\r\n'
              + payload)
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = s.recv(4096)
        if not chunk:
            s.close()
            raise DockerError(502, 'exec stream closed during handshake')
        buf += chunk
        if len(buf) > 65536:
            s.close()
            raise DockerError(502, 'oversized exec handshake')
    code, leftover = _parse_hijack_head(buf)
    if code not in (101, 200):
        s.close()
        raise DockerError(code, 'exec start failed (HTTP %d)' % code)
    return s, leftover


def _ws_fail(ws, message):
    try:
        ws.send(json.dumps({'type': 'error', 'error': message}))
    finally:
        try:
            ws.close()
        except Exception:
            pass


def ws_docker_shell(ws, cid):
    """Bridge an xterm.js session to a shell inside a running container.
    require_login authenticated the handshake; a shell is write access, so
    admin is required explicitly (the LXD console's rules)."""
    if getattr(g, 'identity_role', None) != 'admin':
        return _ws_fail(ws, 'Administrator access required')
    if 'docker' in load_disabled_modules():
        return _ws_fail(ws, "module 'docker' is disabled on this node")
    if not RE_DK_NAME.match(cid or ''):
        try:
            ws.close()
        except Exception:
            pass
        return
    try:
        insp = docker_request('GET', '/containers/%s/json' % cid)
        if not ((insp.get('State') or {}).get('Running')):
            return _ws_fail(ws, 'Container is not running')
        exec_id = _exec_create(cid)
        stream, leftover = _exec_start_hijack(exec_id)
    except DockerError as e:
        return _ws_fail(ws, e.message)

    stop = threading.Event()

    def pump_daemon_to_browser():
        try:
            if leftover:
                ws.send(leftover)
            while not stop.is_set():
                data = stream.recv(16384)
                if not data:
                    break
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
                try:
                    docker_request('POST', '/exec/%s/resize?h=%d&w=%d'
                                   % (exec_id, int(obj.get('height', 24)),
                                      int(obj.get('width', 80))))
                except (DockerError, ValueError):
                    pass
                continue
            if isinstance(obj, dict) and obj.get('type') == 'stdin':
                payload = obj.get('data', '')
                stream.sendall(payload.encode('utf-8')
                               if isinstance(payload, str) else payload)
                continue
            stream.sendall(msg.encode('utf-8') if isinstance(msg, str) else msg)
    except Exception:
        pass
    finally:
        stop.set()
        try:
            stream.close()
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass


def register_ws(sock):
    """Attach the Docker shell websocket to the app's flask-sock instance."""
    sock.route('/ws/docker/<cid>')(ws_docker_shell)
