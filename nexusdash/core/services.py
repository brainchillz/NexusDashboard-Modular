"""System-service registry (SYSTEM_SERVICES) + per-family overrides.

Extracted verbatim from the single-file dashboard. The /api/service/* action
routes join this module in Stage 1; Stage 2 derives service entries from module
descriptors while keeping this table as the merged result.
"""
import os
import re
from pathlib import Path

from .config import FAMILY

# llama.cpp inference server — managed like a system service (status/control via
# the shared service endpoints) plus its own page for model + CLI-arg editing.
LLAMA_SERVICE = 'llama-server'
LLAMA_CONF = os.environ.get('DASHBOARD_LLAMA_CONF', '/etc/llama.conf')
LLAMA_MODELS_DIR = os.environ.get('DASHBOARD_LLAMA_MODELS_DIR', '/usr/share/models')
LLAMA_DEFAULT_BIN = os.environ.get('DASHBOARD_LLAMA_BIN', '/usr/local/llama.cpp/llama-server')
LLAMA_URL = os.environ.get('DASHBOARD_LLAMA_URL', 'http://localhost:8080')

RE_SERVICE = re.compile(r'^[a-zA-Z0-9@._-]+$')

SYSTEM_SERVICES = {
    'zfs': {'name': 'ZFS', 'service': 'zfs.target', 'pkg': 'zfsutils-linux', 'binary': '/usr/sbin/zpool'},
    'iscsi': {'name': 'iSCSI Target', 'service': 'target', 'pkg': 'targetcli-fb', 'binary': '/usr/bin/targetcli'},
    'nfs': {'name': 'NFS Server', 'service': 'nfs-server', 'pkg': 'nfs-kernel-server', 'binary': '/usr/sbin/nfsdclnts'},
    'smb': {'name': 'Samba', 'service': 'smbd', 'pkg': 'samba', 'binary': '/usr/sbin/smbd'},
    # No apt package (pkg=None) and never raises health alerts (alert=False) —
    # llama-server is frequently stopped on purpose / absent on storage hosts.
    'llamacpp': {'name': 'llama.cpp', 'service': LLAMA_SERVICE, 'pkg': None,
                 'binary': LLAMA_DEFAULT_BIN, 'alert': False},
    # A media (DLNA) server is often intentionally off; a stopped one isn't an
    # operational emergency, so it never raises health alerts (alert=False).
    'minidlna': {'name': 'MiniDLNA', 'service': 'minidlna', 'pkg': 'minidlna',
                 'binary': '/usr/sbin/minidlnad', 'alert': False},
    # Reverse-proxy front door — absent on most nodes by design (alert=False).
    'caddy': {'name': 'Caddy', 'service': 'caddy', 'pkg': 'caddy',
              'binary': '/usr/bin/caddy', 'alert': False},
    # DNS/DHCP server managed by the dnsmasq module — off/absent on most nodes
    # by design (alert=False; the module raises its own dnsmasq-down alert only
    # when a feature is actually enabled).
    'dnsmasq': {'name': 'Dnsmasq', 'service': 'dnsmasq', 'pkg': 'dnsmasq',
                'binary': '/usr/sbin/dnsmasq', 'alert': False},
    # Docker engine — the `docker` module drives containers over the socket; the
    # service manager controls the daemon itself. pkg=None (the package name
    # varies: docker-ce / docker.io / moby-engine), so presence is inferred from
    # the unit file or binary. alert=False: absent on most nodes by design.
    'docker': {'name': 'Docker', 'service': 'docker', 'pkg': None,
               'binary': '/usr/bin/docker', 'alert': False},
    # Host firewall — the firewall module is ufw-only, so this is ufw on
    # Debian/Ubuntu and shows "Missing" on RHEL (firewalld), matching the
    # module's own report. alert=False: a firewall is off on most nodes.
    'firewall': {'name': 'UFW Firewall', 'service': 'ufw', 'pkg': 'ufw',
                 'binary': '/usr/sbin/ufw', 'alert': False},
}

# Container runtime (LXD via snap or deb, or Incus) — the containers modules
# talk to it over a unix socket; the service manager controls its systemd unit.
# Which unit/label/binary applies depends on which runtime is installed, so it
# is detected by socket path — mirroring modules/containers/client.py's
# _daemon_detect WITHOUT importing it (core must not depend on a feature
# module). Keyed 'instances' (the core containers module id) so disabling that
# module hides the service line, like every other entry. alert=False.
_CONTAINER_RUNTIMES = [
    ('/var/snap/lxd/common/lxd/unix.socket',
     {'name': 'LXD', 'service': 'snap.lxd.daemon', 'pkg': None,
      'binary': '/snap/bin/lxd', 'alert': False}),
    ('/var/lib/lxd/unix.socket',
     {'name': 'LXD', 'service': 'lxd', 'pkg': None,
      'binary': '/usr/bin/lxd', 'alert': False}),
    ('/var/lib/incus/unix.socket',
     {'name': 'Incus', 'service': 'incus', 'pkg': None,
      'binary': '/usr/bin/incus', 'alert': False}),
]


def _detect_container_service():
    override = os.environ.get('DASHBOARD_LXD_SOCKET')
    if override:
        if 'incus' in override:
            return dict(_CONTAINER_RUNTIMES[2][1])
        # Explicit LXD override: snap path → snap unit, else the deb unit.
        return dict(_CONTAINER_RUNTIMES[0 if 'snap' in override else 1][1])
    for path, entry in _CONTAINER_RUNTIMES:
        if os.path.exists(path):
            return dict(entry)
    # None installed — still list an entry (Missing/inactive), like caddy/dnsmasq.
    return dict(_CONTAINER_RUNTIMES[0][1])


SYSTEM_SERVICES['instances'] = _detect_container_service()

# Per-family overrides for the services whose systemd unit and/or package name
# differ from the Debian/Ubuntu defaults above. RHEL/Rocky: Samba's unit is
# `smb` (not `smbd`), NFS ships in `nfs-utils`, iSCSI in `targetcli`, ZFS from
# the OpenZFS repo's `zfs` package. The `nfs-server` and `target` unit names are
# already correct on both families.
SERVICE_OVERRIDES = {
    'rhel': {
        'zfs':   {'pkg': 'zfs'},
        'iscsi': {'pkg': 'targetcli'},
        'nfs':   {'pkg': 'nfs-utils'},
        'smb':   {'service': 'smb', 'pkg': 'samba'},
    },
}
for _key, _ov in SERVICE_OVERRIDES.get(FAMILY, {}).items():
    if _key in SYSTEM_SERVICES:
        SYSTEM_SERVICES[_key].update(_ov)


def _unit_present(unit):
    """True if a systemd unit file exists in any standard location."""
    name = unit if ('.' in unit) else unit + '.service'
    return (Path(f'/etc/systemd/system/{name}').exists() or
            Path(f'/usr/lib/systemd/system/{name}').exists() or
            Path(f'/lib/systemd/system/{name}').exists())


def resolve_service(service):
    """Map a service key to its systemd unit, validating arbitrary input."""
    if service in SYSTEM_SERVICES:
        return SYSTEM_SERVICES[service]['service']
    return service if RE_SERVICE.match(service or '') else None
