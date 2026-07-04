"""Command execution — NEVER through a shell.

Extracted verbatim from the single-file dashboard. All system commands go
through run()/run_safe(), which take an ARGUMENT LIST and run with shell=False;
that is what prevents command injection. ``sudo -n`` fails fast when a sudoers
rule is missing instead of hanging on a password prompt.
"""
import re
import subprocess
from flask import jsonify


def run(args, input_data=None, no_sudo=False):
    """Run a command given as an argument list (NO shell).

    Passing a list and shell=False means user-supplied values can never be
    interpreted by a shell, which closes off command injection. ``sudo -n``
    is used so a missing/incorrect sudoers rule fails immediately instead of
    blocking on a password prompt.
    """
    if isinstance(args, str):
        # Only fixed, trusted command strings should be passed as strings.
        args = args.split()
    if not no_sudo:
        args = ['sudo', '-n'] + list(args)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, input=input_data)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return '', 'Command timed out', -1
    except FileNotFoundError:
        return '', 'Command not found', -1


def run_safe(args, input_data=None):
    out, err_, rc = run(args, input_data=input_data)
    return {'success': rc == 0, 'stdout': out, 'stderr': err_, 'returncode': rc}


def err(message, code=400):
    return jsonify({'success': False, 'error': message}), code


def _size_to_bytes(s):
    """Parse a binary size string ('64.0MiB', '18.2TiB') to bytes."""
    m = re.match(r'^([\d.]+)\s*([KMGTP]?)i?B?$', (s or '').strip())
    if not m:
        return 0
    units = {'': 1, 'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4, 'P': 1024**5}
    return int(float(m.group(1)) * units.get(m.group(2), 1))


def _human_bytes(n):
    n = float(n or 0)
    for u in ('B', 'K', 'M', 'G', 'T', 'P'):
        if n < 1024 or u == 'P':
            return f'{int(n)}B' if u == 'B' else f'{n:.1f}{u}'
        n /= 1024


def _num(x):
    # Relocated from the history section (single-file app.py line 1341): it is
    # shared by history, gpu and metrics, and history→gpu→history would cycle.
    try:
        return int(x)
    except (TypeError, ValueError):
        return None
