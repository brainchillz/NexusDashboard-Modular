"""Caddy reverse proxy — the front door: hostname -> backend routes.

One Caddy instance terminates TLS on :80/:443 and fans hostnames out to
backend apps ("adding an app is 3 lines"). This module manages exactly that
workflow: list the site blocks in /etc/caddy/Caddyfile, add/edit/delete
simple reverse-proxy sites, and edit the raw file. Every change goes through
the root-owned <prefix>-caddy helper (netplan-helper pattern): the candidate
is checked with `caddy validate` BEFORE the live file is touched, and the
service is reloaded only when it is running. On a host without caddy (or
before the helper/sudoers land) the page reports available/editable false and
everything degrades gracefully — same contract as Containers without LXD.

Caddy adds X-Forwarded-For/-Proto/-Host to proxied requests automatically, so
backends see real client addresses once they are configured to trust the
proxy — and only then; behind-Caddy-only apps should trust the header, apps
whose direct port is still open should not (the header is spoofable there).
"""
import json
import os
import re
import shutil
from flask import Blueprint, jsonify, request

from ..core.config import HELPER_PREFIX
from ..core.runcmd import run, err
from ..core.tls import cert_info

bp = Blueprint('caddy', __name__)

CADDYFILE = os.environ.get('DASHBOARD_CADDYFILE', '/etc/caddy/Caddyfile')
CADDY_HELPER = HELPER_PREFIX + '-caddy'
CADDY_SERVICE = 'caddy'

# A site address: optional wildcard label, hostname, optional :port.
RE_CADDY_HOST = re.compile(
    r'^(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?'
    r'(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*(:\d{1,5})?\Z')
# A reverse_proxy upstream: optional scheme, host/IP, optional :port.
RE_CADDY_UPSTREAM = re.compile(
    r'^(https?://)?[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?(:\d{1,5})?\Z')

RE_RP_PLAIN = re.compile(r'^reverse_proxy\s+(\S+)\Z')
RE_RP_OPEN = re.compile(r'^reverse_proxy\s+(\S+)\s+\{\Z')
# `tls <cert-file> <key-file>` — the explicit-certificate form only (not
# `tls internal` / `tls <email>`), which is what the fleet's wildcard uses.
RE_TLS_LINE = re.compile(r'^tls\s+(/\S+)\s+(/\S+)\Z')


def _strip_comment(line):
    # '#' starts a comment only at the start of a token (Caddyfile rule).
    return re.sub(r'(^|\s)#.*$', r'\1', line)


def _parse_caddyfile(text):
    """Line-oriented top-level block scan. Returns block dicts with the label,
    stripped non-empty body lines, and the [start, end) line span so edits can
    splice the original text. Assumes caddyfmt-shaped braces ('{' ending a
    line, '}' alone on its own); a file that doesn't parse that way simply
    gets no structured editing — the raw editor still works."""
    blocks = []
    lines = (text or '').splitlines()
    i = 0
    while i < len(lines):
        line = _strip_comment(lines[i]).strip()
        if line.endswith('{'):
            label, depth, start = line[:-1].strip(), 1, i
            body = []
            i += 1
            while i < len(lines) and depth:
                inner = _strip_comment(lines[i]).strip()
                if inner.endswith('{'):
                    depth += 1
                elif inner == '}':
                    depth -= 1
                if depth and inner:
                    body.append(inner)
                i += 1
            blocks.append({'label': label, 'body': body,
                           'start': start, 'end': i})
        else:
            i += 1   # blank, comment, or a top-level one-liner (import, ...)
    return blocks


def _match_simple(body):
    """(upstream, skip_tls_verify, tls_pair) when the body is exactly a shape
    this module writes — a reverse_proxy (bare, or with the self-signed
    transport block), optionally preceded by an explicit `tls <cert> <key>`
    line. Anything else — extra directives, multiple upstreams — returns None
    and is edited through the raw file only."""
    tls = None
    if body and RE_TLS_LINE.match(body[0]):
        m = RE_TLS_LINE.match(body[0])
        tls = (m.group(1), m.group(2))
        body = body[1:]
    if len(body) == 1:
        m = RE_RP_PLAIN.match(body[0])
        if m:
            return m.group(1), False, tls
    if len(body) == 5:
        m = RE_RP_OPEN.match(body[0])
        if (m and body[1] == 'transport http {'
                and body[2] == 'tls_insecure_skip_verify'
                and body[3] == '}' and body[4] == '}'):
            return m.group(1), True, tls
    return None


def _addresses(label):
    return [a.strip() for a in label.split(',') if a.strip()]


def _is_site(block):
    """Site blocks have a label that is not the global-options block (empty
    label) and not a snippet definition ('(name)')."""
    return bool(block['label']) and not block['label'].startswith('(')


def _sites(blocks):
    sites = []
    for b in blocks:
        if not _is_site(b):
            continue
        addrs = _addresses(b['label'])
        m = _match_simple(b['body'])
        sites.append({
            'addresses': addrs,
            'upstream': m[0] if m else None,
            'skip_tls_verify': bool(m and m[1]),
            'tls_cert': m[2][0] if m and m[2] else None,
            'tls_key': m[2][1] if m and m[2] else None,
            # Structured edit only for single-address blocks this module could
            # have written itself; everything else is raw-editor territory.
            'simple': bool(m) and len(addrs) == 1,
        })
    return sites


def _tls_pairs(blocks):
    """Unique (cert, key) path pairs from explicit `tls` directives anywhere
    in the file (body lines are flattened, so nested occurrences count), in
    file order. These are the pairs the UI offers for new routes and the only
    targets the cert-replace endpoint accepts."""
    pairs = []
    for b in blocks:
        for line in b['body']:
            m = RE_TLS_LINE.match(line)
            if m and (m.group(1), m.group(2)) not in pairs:
                pairs.append((m.group(1), m.group(2)))
    return pairs


def _read_caddyfile():
    """File content, or None when unreadable (root-tightened perms — the
    stock packaging is world-readable 0644)."""
    try:
        with open(CADDYFILE) as f:
            return f.read()
    except OSError:
        return None


def _caddy_status():
    st = {'available': bool(shutil.which('caddy')), 'active': False,
          'version': '', 'caddyfile': CADDYFILE, 'file_readable': False,
          'editable': os.path.exists(CADDY_HELPER), 'sites': [], 'certs': []}
    if not st['available']:
        return st
    out, _, rc = run(['caddy', 'version'], no_sudo=True)
    if rc == 0 and out.strip():
        st['version'] = out.strip().split()[0]
    out, _, rc = run(['systemctl', 'is-active', CADDY_SERVICE], no_sudo=True)
    st['active'] = rc == 0 and out.strip() == 'active'
    text = _read_caddyfile()
    if text is not None:
        st['file_readable'] = True
        blocks = _parse_caddyfile(text)
        st['sites'] = _sites(blocks)
        st['certs'] = [dict(cert_info(c), cert=c, key=k)
                       for c, k in _tls_pairs(blocks)]
    return st


def _apply(text):
    """Hand the whole candidate Caddyfile to the root-owned helper: nothing
    is written unless `caddy validate` accepts it, and the service reloads
    only when running (an inactive caddy picks the file up at next start)."""
    return run([CADDY_HELPER, 'apply'], input_data=text, timeout=60)


def _apply_or_err(text):
    out, errout, rc = _apply(text)
    if rc != 0:
        return err((errout or out).strip()[-2000:] or 'caddy apply failed')
    return jsonify({'success': True})


def _render_site(host, upstream, skip_verify, tls=None):
    tls_line = '\ttls %s %s\n' % tls if tls else ''
    if skip_verify:
        return ('%s {\n%s\treverse_proxy %s {\n\t\ttransport http {\n'
                '\t\t\ttls_insecure_skip_verify\n\t\t}\n\t}\n}\n'
                % (host, tls_line, upstream))
    return '%s {\n%s\treverse_proxy %s\n}\n' % (host, tls_line, upstream)


def _validate_site(host, upstream):
    if not RE_CADDY_HOST.match(host or ''):
        return 'Host must be a hostname like app.example.com (optional *. and :port)'
    if not RE_CADDY_UPSTREAM.match(upstream or ''):
        return 'Upstream must be host[:port], optionally with http:// or https://'
    return None


def _editable_or_err():
    """Common preconditions for every mutation. Returns (text, None) with the
    current file content, or (None, error response)."""
    if not shutil.which('caddy'):
        return None, err('caddy is not installed on this host')
    if not os.path.exists(CADDY_HELPER):
        return None, err('The caddy helper is missing on this node — it ships '
                         'with fresh installs; older nodes need the helper and '
                         'its sudoers line added by hand')
    text = _read_caddyfile()
    if text is None:
        return None, err('%s is not readable by the dashboard user' % CADDYFILE, 403)
    return text, None


def _find_site(blocks, host):
    return next((b for b in blocks
                 if _is_site(b) and host in _addresses(b['label'])), None)


@bp.route('/api/caddy')
def caddy_get():
    return jsonify(_caddy_status())


@bp.route('/api/caddy/file')
def caddy_file_get():
    if not shutil.which('caddy'):
        return err('caddy is not installed on this host')
    text = _read_caddyfile()
    if text is None:
        return err('%s is not readable by the dashboard user' % CADDYFILE, 403)
    return jsonify({'file': CADDYFILE, 'content': text,
                    'editable': os.path.exists(CADDY_HELPER)})


@bp.route('/api/caddy/file', methods=['POST'])
def caddy_file_save():
    _, bad = _editable_or_err()
    if bad:
        return bad
    content = (request.get_json() or {}).get('content') or ''
    if not content.strip() or '\x00' in content:
        return err('Caddyfile content required')
    return _apply_or_err(content)


def _tls_choice(data, blocks):
    """The (cert, key) pair a site add/update asked for, validated against
    the pairs the file already references. Returns (pair-or-None, error)."""
    cert, key = (data.get('tls_cert') or '').strip(), (data.get('tls_key') or '').strip()
    if not cert and not key:
        return None, None
    if (cert, key) not in _tls_pairs(blocks):
        return None, ('That certificate pair is not referenced by the '
                      'Caddyfile — pick one from the list or use the raw editor')
    return (cert, key), None


@bp.route('/api/caddy/site', methods=['POST'])
def caddy_site_add():
    data = request.get_json() or {}
    host = (data.get('host') or '').strip()
    upstream = (data.get('upstream') or '').strip()
    bad_input = _validate_site(host, upstream)
    if bad_input:
        return err(bad_input)
    text, bad = _editable_or_err()
    if bad:
        return bad
    blocks = _parse_caddyfile(text)
    if _find_site(blocks, host):
        return err('A site block for %s already exists' % host)
    tls, bad_tls = _tls_choice(data, blocks)
    if bad_tls:
        return err(bad_tls)
    block = _render_site(host, upstream, bool(data.get('skip_tls_verify')), tls)
    new = (text.rstrip('\n') + '\n\n' if text.strip() else '') + block
    return _apply_or_err(new)


@bp.route('/api/caddy/site/update', methods=['POST'])
def caddy_site_update():
    data = request.get_json() or {}
    host = (data.get('host') or '').strip()
    new = data.get('new') or {}
    new_host = (new.get('host') or '').strip()
    upstream = (new.get('upstream') or '').strip()
    bad_input = _validate_site(new_host, upstream)
    if bad_input:
        return err(bad_input)
    text, bad = _editable_or_err()
    if bad:
        return bad
    blocks = _parse_caddyfile(text)
    cur = _find_site(blocks, host)
    if cur is None:
        return err('No site block for %s — refresh and retry' % host, 404)
    if _match_simple(cur['body']) is None or len(_addresses(cur['label'])) != 1:
        return err('This site block has extra configuration — edit the '
                   'Caddyfile instead')
    if new_host != host and _find_site(blocks, new_host):
        return err('A site block for %s already exists' % new_host)
    tls, bad_tls = _tls_choice(new, blocks)
    if bad_tls:
        return err(bad_tls)
    block = _render_site(new_host, upstream, bool(new.get('skip_tls_verify')), tls)
    lines = text.splitlines()
    new_text = '\n'.join(lines[:cur['start']] + block.splitlines()
                         + lines[cur['end']:]) + '\n'
    return _apply_or_err(new_text)


@bp.route('/api/caddy/site/delete', methods=['POST'])
def caddy_site_delete():
    host = ((request.get_json() or {}).get('host') or '').strip()
    text, bad = _editable_or_err()
    if bad:
        return bad
    cur = _find_site(_parse_caddyfile(text), host)
    if cur is None:
        return err('No site block for %s — refresh and retry' % host, 404)
    lines = text.splitlines()
    head = lines[:cur['start']]
    while head and not head[-1].strip():   # swallow the separating blank line
        head.pop()
    kept = head + lines[cur['end']:]
    return _apply_or_err('\n'.join(kept) + '\n' if kept else '')


@bp.route('/api/caddy/cert', methods=['POST'])
def caddy_cert_replace():
    """Replace a cert/key pair the Caddyfile references (e.g. the wildcard at
    renewal). Same PEM sanity checks as the dashboard's own cert upload; the
    root-owned helper re-validates the pair (openssl public-key match),
    confines both paths to /etc/caddy, preserves owner/mode, and reloads."""
    data = request.get_json() or {}
    cert_path = (data.get('cert_path') or '').strip()
    key_path = (data.get('key_path') or '').strip()
    cert_pem = (data.get('cert') or '').strip()
    key_pem = (data.get('key') or '').strip()
    if 'BEGIN CERTIFICATE' not in cert_pem:
        return err('Certificate must be PEM (-----BEGIN CERTIFICATE-----)')
    if 'PRIVATE KEY' not in key_pem:
        return err('Key must be a PEM private key')
    if len(cert_pem) > 100_000 or len(key_pem) > 100_000:
        return err('Certificate or key too large')
    text, bad = _editable_or_err()
    if bad:
        return bad
    if (cert_path, key_path) not in _tls_pairs(_parse_caddyfile(text)):
        return err('That cert/key pair is not referenced by the Caddyfile — '
                   'refresh and retry', 404)
    out, errout, rc = run([CADDY_HELPER, 'cert', cert_path, key_path],
                          input_data=json.dumps({'cert': cert_pem,
                                                 'key': key_pem}),
                          timeout=60)
    if rc != 0:
        return err((errout or out).strip()[-2000:] or 'certificate replace failed')
    return jsonify({'success': True})


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'caddy', 'label': 'Caddy Proxy', 'category': 'Web',
          'blueprint': bp}
