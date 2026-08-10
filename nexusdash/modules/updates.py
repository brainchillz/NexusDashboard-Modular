"""System package updates — pending-update visibility for the front page.

Checks the backend package manager for available updates (apt on
Debian/Ubuntu, dnf on RHEL/Rocky — dispatched off core.config FAMILY, the
same per-family table that drives service/package naming) and surfaces the
counts on /api/summary so the dashboard can flag "Updates available" /
security updates beside the health line, plus a System > Updates page
listing the packages.

CHECKING is read-only and rootless — no sudoers grant needed:
- debian: ``apt-get -s dist-upgrade`` simulates against the apt lists the
  distro's own apt-daily timer refreshes (we never run ``apt-get update``).
- rhel:   ``dnf check-update`` fetches per-user metadata as the service
  user (exit 100 = updates pending); ``--security`` narrows to advisories.

Checks are slow (dnf metadata can take tens of seconds), so results are
cached module-level with a TTL and refreshed in a background thread kicked
from the summary hook / GET — the 30s dashboard poll only ever reads the
cache.

APPLYING goes through the root-owned ``<HELPER_PREFIX>-updates`` helper
(installers carry it; fixed argv — the dashboard passes no user input across
that boundary). The helper streams the package manager's own output, which
the apply thread parses into a progress counter the page polls; afterwards
_reboot_required() gives the authoritative reboot answer (the per-package
`reboot_likely` flag is only ever a heuristic — apt/dnf don't know reboot
needs in advance). Degrades to a read-only viewer when the helper is absent
(caddy-module precedent).
"""
import os
import re
import time
import subprocess
import threading

from flask import Blueprint, jsonify, current_app

from ..core.config import FAMILY, HELPER_PREFIX
from ..core.runcmd import run, err

bp = Blueprint('updates', __name__)

# Re-check this often at most; the front page just reads the cache.
CHECK_TTL = 3600

# Root-owned apply helper (installers carry it; --helpers-only refreshes it on
# existing nodes). Applying degrades cleanly to unavailable without it —
# the caddy-module `editable` precedent.
UPDATES_HELPER = HELPER_PREFIX + '-updates'

_lock = threading.Lock()
_state = {'checked': None, 'available': 0, 'security': 0,
          'packages': [], 'error': None, 'checking': False}

# One apply at a time, machine-wide; the page polls this while it runs.
_apply_lock = threading.Lock()
_apply = {'running': False, 'rc': None, 'started': None, 'finished': None,
          'log': [], 'done': 0, 'total': 0, 'reboot_required': None}
_APPLY_LOG_MAX = 400

# Pre-classification of updates that usually demand a reboot (kernel, libc,
# init, crypto, microcode, bootloader). HEURISTIC ONLY — neither apt nor dnf
# knows this in advance; the authoritative answer is _reboot_required() after
# an apply. Prefix-matched against the package name.
REBOOT_LIKELY_PREFIXES = (
    'linux-image', 'linux-generic', 'linux-headers', 'linux-modules',
    'kernel', 'glibc', 'libc6', 'systemd', 'dbus', 'openssl', 'libssl',
    'intel-microcode', 'amd64-microcode', 'microcode_ctl', 'grub')


def _reboot_likely(name):
    return name.lower().startswith(REBOOT_LIKELY_PREFIXES)


# ─── Pure parsers (unit-tested; text in, rows out) ─────────────────────
_RE_APT_INST = re.compile(r'^Inst (\S+)(?: \[([^\]]+)\])? \((\S+) ([^)]*)\)')


def parse_apt_dist_upgrade(text):
    """Rows from ``apt-get -s dist-upgrade`` output: only ``Inst`` lines
    matter (Conf/Remv are simulation noise). A package is a security update
    when its candidate's origin/archive mentions 'security'
    (noble-security, bookworm-security, …)."""
    rows = []
    for line in (text or '').splitlines():
        m = _RE_APT_INST.match(line.strip())
        if not m:
            continue
        name, cur, cand, origin = m.groups()
        rows.append({'name': name, 'current': cur or '', 'candidate': cand,
                     'origin': origin.rsplit(' ', 1)[0],
                     'security': 'security' in origin.lower()})
    return rows


def parse_dnf_check_update(text):
    """Rows from ``dnf -q check-update`` output: ``name.arch  version  repo``
    triples, one per line. The trailing 'Obsoleting Packages' section (and
    its indented continuation lines) is not a pending update — stop there."""
    rows = []
    for line in (text or '').splitlines():
        if line.strip().lower().startswith('obsoleting packages'):
            break
        if not line.strip() or line != line.lstrip():
            continue
        parts = line.split()
        if len(parts) != 3 or '.' not in parts[0]:
            continue
        name, version, repo = parts
        rows.append({'name': name, 'current': '', 'candidate': version,
                     'origin': repo, 'security': False})
    return rows


# ─── The check itself (runs in a background thread) ────────────────────
def _check_debian():
    out, e, rc = run(['apt-get', '-s', '-o', 'Debug::NoLocking=1',
                      'dist-upgrade'], no_sudo=True, timeout=120)
    if rc != 0:
        return None, (e or out or 'apt-get failed').strip().splitlines()[-1]
    return parse_apt_dist_upgrade(out), None


def _check_rhel():
    # exit 100 = updates available, 0 = none, anything else = error. -y
    # auto-accepts repo GPG-key imports into the user cache (third-party
    # repos prompt otherwise — bit live with a 45Drives repo); the parser
    # skips the import noise.
    out, e, rc = run(['dnf', '-q', '-y', 'check-update'],
                     no_sudo=True, timeout=300)
    if rc not in (0, 100):
        return None, (e or out or 'dnf failed').strip().splitlines()[-1]
    rows = parse_dnf_check_update(out) if rc == 100 else []
    if rows:
        sout, _, src = run(['dnf', '-q', '-y', 'check-update', '--security'],
                           no_sudo=True, timeout=300)
        if src == 100:
            sec = {r['name'] for r in parse_dnf_check_update(sout)}
            for r in rows:
                r['security'] = r['name'] in sec
    return rows, None


def _refresh():
    rows, error = (_check_rhel if FAMILY == 'rhel' else _check_debian)()
    with _lock:
        if error is not None:
            _state.update({'error': error, 'checking': False,
                           'checked': int(time.time())})
        else:
            for r in rows:
                r['reboot_likely'] = _reboot_likely(r['name'])
            rows.sort(key=lambda r: (not r['security'], r['name']))
            _state.update({'checked': int(time.time()), 'error': None,
                           'available': len(rows),
                           'security': sum(1 for r in rows if r['security']),
                           'packages': rows, 'checking': False})


def _kick_refresh(force=False):
    """Start a background check if the cache is stale (or force'd). Never
    kicked under test (TESTING) so suites stay deterministic and command-free,
    and never while an apply runs (a concurrent dnf would fight the lock)."""
    if current_app.config.get('TESTING') or _apply['running']:
        return
    with _lock:
        if _state['checking']:
            return
        stale = _state['checked'] is None or \
            time.time() - _state['checked'] > CHECK_TTL
        if not (stale or force):
            return
        _state['checking'] = True
    threading.Thread(target=_refresh, daemon=True).start()


def _snapshot():
    with _lock:
        return dict(_state)


# ─── Applying (root helper; streamed progress) ─────────────────────────
# apt has no machine progress on stdout, but prints one 'Unpacking X' and one
# 'Setting up X' line per package — 2 steps each against the planned count.
# dnf prints an explicit ' N/M' counter on every transaction line.
_RE_DNF_STEP = re.compile(r'\s(\d+)/(\d+)\s*$')


def _apt_progress_step(line):
    return line.startswith('Unpacking ') or line.startswith('Setting up ')


def _dnf_progress(line):
    m = _RE_DNF_STEP.search(line.rstrip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _reboot_required():
    """Authoritative post-apply answer (the upfront per-package split can only
    ever be the reboot_likely heuristic). None = cannot tell."""
    if FAMILY == 'rhel':
        # dnf-utils; rc 1 = reboot needed, 0 = not, else unknown/not installed
        _, _, rc = run(['needs-restarting', '-r'], no_sudo=True, timeout=60)
        return True if rc == 1 else (False if rc == 0 else None)
    return os.path.exists('/run/reboot-required')


def _apply_thread():
    proc = None
    try:
        proc = subprocess.Popen(['sudo', '-n', UPDATES_HELPER, 'apply'],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            line = line.rstrip('\n')
            with _apply_lock:
                _apply['log'].append(line)
                del _apply['log'][:-_APPLY_LOG_MAX]
                if FAMILY == 'rhel':
                    step = _dnf_progress(line)
                    if step and step[1] >= _apply['total']:
                        _apply['done'], _apply['total'] = step
                elif _apt_progress_step(line):
                    _apply['done'] += 1
        rc = proc.wait()
    except Exception as e:                          # helper spawn/read failure
        if proc is not None:
            proc.kill()
        rc = -1
        with _apply_lock:
            _apply['log'].append('apply failed: %s' % e)
    reboot = _reboot_required()
    # Re-check BEFORE flipping running off: the moment the page sees the apply
    # finish, the pending counts are already the post-apply truth (otherwise
    # its first re-render shows the stale pre-apply numbers for a beat).
    _refresh()
    with _apply_lock:
        _apply.update({'rc': rc, 'finished': int(time.time()),
                       'reboot_required': reboot, 'running': False})


def _apply_snapshot():
    with _apply_lock:
        s = dict(_apply)
        s['log'] = list(s['log'])
        return s


# ─── Hooks / routes ────────────────────────────────────────────────────
def updates_summary():
    """Front-page block: counts only (the package list stays on GET
    /api/updates). Also opportunistically kicks a re-check when stale, so a
    node that just sits on the dashboard stays current."""
    _kick_refresh()
    s = _snapshot()
    return {'available': s['available'], 'security': s['security'],
            'checked': s['checked'], 'error': s['error']}


@bp.route('/api/updates')
def api_updates():
    _kick_refresh()
    s = _snapshot()
    s['family'] = FAMILY
    s['manager'] = 'dnf' if FAMILY == 'rhel' else 'apt'
    s['apply_available'] = os.path.exists(UPDATES_HELPER)
    apply_state = _apply_snapshot()
    # The full log only while running / after a run — trimmed to the tail the
    # page shows. reboot_required stays authoritative-after-apply; on debian
    # the marker file is cheap enough to consult live too.
    if FAMILY != 'rhel' and not apply_state['running']:
        apply_state['reboot_required'] = os.path.exists('/run/reboot-required')
    s['apply'] = apply_state
    return jsonify(s)


@bp.route('/api/updates/apply', methods=['POST'])
def api_updates_apply():
    """Run the full upgrade via the root helper (admin — central RBAC blocks
    read-only POSTs). Async: the page polls GET /api/updates and renders the
    log/progress from its `apply` block."""
    if not os.path.exists(UPDATES_HELPER):
        return err('updates helper not installed on this node — re-run the '
                   'installer with --helpers-only (or fleet-deploy --helpers)')
    snap = _snapshot()
    if snap['checking']:
        return err('a check is still running — retry when it finishes')
    if not snap['available']:
        return err('nothing to apply — no pending updates')
    with _apply_lock:
        if _apply['running']:
            return err('an apply is already running', 409)
        _apply.update({'running': True, 'rc': None, 'finished': None,
                       'started': int(time.time()), 'log': [], 'done': 0,
                       # apt: one Unpacking + one Setting-up line per package;
                       # dnf overwrites total from its own N/M counter lines
                       'total': 0 if FAMILY == 'rhel' else snap['available'] * 2,
                       'reboot_required': None})
    threading.Thread(target=_apply_thread, daemon=True).start()
    return jsonify({'success': True, 'started': True})


@bp.route('/api/updates/check', methods=['POST'])
def api_updates_check():
    """Force a re-check now (admin — central RBAC blocks read-only POSTs).
    Async: returns immediately; the page polls GET until checking clears."""
    if _snapshot()['checking']:
        return jsonify({'success': True, 'started': False, 'checking': True})
    _kick_refresh(force=True)
    return jsonify({'success': True, 'started': True, 'checking': True})


MODULE = {'id': 'updates', 'order': 240, 'label': 'System Updates',
          'category': 'System',
          'nav': {'cat': 'system', 'cat_order': 100, 'pages': [
                  # order 15 slots between core Services (10) and Tasks (20)
                  {'id': 'updates', 'label': 'Updates', 'icon': 'dl',
                   'order': 15}]},
          'blueprint': bp,
          'summary': updates_summary}
