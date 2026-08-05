"""System-service registry (SYSTEM_SERVICES) + per-family overrides.

3.0.0: entries are CONTRIBUTED by module descriptors (`services` /
`services_overrides` keys) and merged here by rebuild_services(), called from
registry.finalize() during create_app(). The module-level names keep their
object identity across rebuilds (the facade and every `from .services import
SYSTEM_SERVICES` binding hold references) — rebuilds mutate in place.
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

# The merged result — filled by rebuild_services() (registry.finalize()).
# The key IS the module id: that linkage drives the disabled-module filter in
# /api/summary and the module_disabled flag on /api/status.
SYSTEM_SERVICES = {}

# The pre-3.0 hand-written insertion order. Byte-significant: the /metrics
# plain-text exposition iterates SYSTEM_SERVICES in insertion order (JSON
# responses sort keys; text does not). Contributed keys not listed here —
# future modules, plugins — append after the seed set in registration order.
_SERVICE_SEED_ORDER = ('zfs', 'iscsi', 'nfs', 'smb', 'llamacpp', 'minidlna',
                       'caddy', 'dnsmasq', 'docker', 'firewall', 'instances')

# Merged per-family override map (contributed via `services_overrides`), kept
# as a module-level name because history/tasks/summary import it.
SERVICE_OVERRIDES = {}


def rebuild_services(contribs, overrides):
    """Merge descriptor-contributed service entries into SYSTEM_SERVICES.

    contribs:  [(module_id, {key: entry})] in registration order
    overrides: [{family: {key: patch}}]   in registration order

    Seed-ordered first (see _SERVICE_SEED_ORDER), then registration order for
    new keys; the FAMILY's override patches are applied last — reproducing the
    old hand-written table byte-for-byte on both families. Idempotent; mutates
    the module-level dicts in place (facade contract).
    """
    merged = {}
    for _mid, table in contribs:
        for key, entry in table.items():
            merged.setdefault(key, dict(entry))
    ordered = {}
    for key in _SERVICE_SEED_ORDER:
        if key in merged:
            ordered[key] = merged.pop(key)
    ordered.update(merged)
    ov_merged = {}
    for ov in overrides:
        for family, patches in ov.items():
            fam = ov_merged.setdefault(family, {})
            for key, patch in patches.items():
                fam.setdefault(key, {}).update(patch)
    for key, patch in ov_merged.get(FAMILY, {}).items():
        if key in ordered:
            ordered[key].update(patch)
    SYSTEM_SERVICES.clear()
    SYSTEM_SERVICES.update(ordered)
    SERVICE_OVERRIDES.clear()
    SERVICE_OVERRIDES.update(ov_merged)


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
