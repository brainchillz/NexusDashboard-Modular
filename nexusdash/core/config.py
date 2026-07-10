"""Core configuration: paths, env helpers, atomic JSON writes, platform detect.

Extracted verbatim from the single-file dashboard (NexusStationDashboard
app.py). All DASHBOARD_* environment variable names and defaults are preserved
— systemd units, install scripts and fleet deployments depend on them.
"""
import os
import json
from datetime import timedelta

# APP_DIR is the directory holding the ROOT app.py entrypoint (the repo root or
# /opt install dir) — NOT the package dir. State files (auth.json, modules.json,
# certs/ ...) live next to app.py exactly as they did in the single-file app.
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(APP_DIR, 'static')
TEMPLATES_DIR = os.path.join(APP_DIR, 'templates')

# Dashboard version. Surfaced via /api/version and /api/me so a cluster
# controller can detect API/version skew across enrolled nodes.
APP_VERSION = '2.1.0'

# Deployment naming prefix — systemd units (<prefix>.service, <prefix>-*.timer),
# the root-owned sudo helpers in /usr/local/sbin, and derived sentinels like the
# managed-fstab markers. Fresh installs are named nexus-dashboard and set this
# in every unit file; legacy in-place-upgraded nodes (fleet: storage-dashboard,
# llama-dashboard units but storage-dashboard timers/helpers) have no env var
# and keep the storage-dashboard names their installer wrote.
UNIT_PREFIX = os.environ.get('DASHBOARD_UNIT_PREFIX', 'storage-dashboard')
HELPER_PREFIX = '/usr/local/sbin/' + UNIT_PREFIX


def env_bool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ('1', 'true', 'yes', 'on')


# ─── TLS configuration ────────────────────────────────────────────────
# The dashboard serves HTTPS by default with a self-signed certificate it
# generates on first run. To use your own certificate, either drop your PEM
# files at the paths below (replacing the self-signed ones) or point
# DASHBOARD_TLS_CERT / DASHBOARD_TLS_KEY at them — or upload them from the
# UI. Set DASHBOARD_TLS=0 to serve plain HTTP (e.g. when a reverse proxy
# terminates TLS in front of the app).
TLS_ENABLED = env_bool('DASHBOARD_TLS', True)
# The listen port. Resolved here (not just in __main__) so request handlers can
# build self-referential URLs (e.g. the network handoff link to the new IP).
DASHBOARD_PORT = int(os.environ.get('DASHBOARD_PORT', 8443 if TLS_ENABLED else 8080))
TLS_DIR = os.environ.get('DASHBOARD_TLS_DIR', os.path.join(APP_DIR, 'certs'))
TLS_CERT = os.environ.get('DASHBOARD_TLS_CERT', os.path.join(TLS_DIR, 'dashboard.crt'))
TLS_KEY = os.environ.get('DASHBOARD_TLS_KEY', os.path.join(TLS_DIR, 'dashboard.key'))

# Session cookie hardening values consumed by create_app().
SESSION_COOKIE_CONFIG = dict(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=env_bool('DASHBOARD_COOKIE_SECURE', TLS_ENABLED),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


def write_json_atomic(path, data, mode=0o600):
    """Write JSON to ``path`` atomically: serialize into a temp file in the same
    directory, fsync it, then os.replace() over the target (an atomic rename on
    POSIX). A crash or full disk mid-write leaves the *original* file intact
    rather than a truncated one — critical for auth.json, where a corrupt
    credentials file would lock everyone out of the dashboard."""
    tmp = '%s.tmp.%d' % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ─── Platform detection (Debian/Ubuntu vs RHEL/Rocky) ─────────────────
# All OS coupling (service unit names, package names, the package manager) is
# driven from per-family tables keyed off this, rather than `if rhel` scattered
# through the code. Detect once from /etc/os-release.
def _platform_from_osrelease(text):
    """Pure parser: given /etc/os-release contents, return
    {family: 'debian'|'rhel', id, version}. Defaults to 'debian' when unknown."""
    data = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    osid = (data.get('ID') or '').lower()
    like = set((data.get('ID_LIKE') or '').lower().split())
    rhel_ids = {'rhel', 'centos', 'rocky', 'almalinux', 'fedora'}
    debian_ids = {'debian', 'ubuntu'}
    if osid in rhel_ids or (rhel_ids & like):
        family = 'rhel'
    elif osid in debian_ids or (debian_ids & like):
        family = 'debian'
    else:
        family = 'debian'  # safe default — Ubuntu is the historical target
    return {'family': family, 'id': osid, 'version': data.get('VERSION_ID', '')}


def detect_platform(path='/etc/os-release'):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        text = ''
    return _platform_from_osrelease(text)


PLATFORM = detect_platform()
FAMILY = PLATFORM['family']

# Per-family file paths / commands that differ between Debian and RHEL.
# mdadm.conf lives in /etc/mdadm/ on Debian but directly in /etc/ on RHEL; the
# initramfs is rebuilt with update-initramfs on Debian, dracut on RHEL.
if FAMILY == 'rhel':
    MDADM_CONF = '/etc/mdadm.conf'
    INITRAMFS_UPDATE = ['dracut', '-f']
else:
    MDADM_CONF = '/etc/mdadm/mdadm.conf'
    INITRAMFS_UPDATE = ['update-initramfs', '-u']
