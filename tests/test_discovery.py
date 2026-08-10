"""Discovery loader: shape classification, ordering, dedupe.

The exact-nav-order and facade-winner invariants have their own tests
(test_registry.py, test_facade_winners.py); this suite covers the loader's
own mechanics so a regression points at discovery.py directly.
"""
import app
from nexusdash.core import discovery, metrics
from nexusdash.modules import logs, network, docker_console
from nexusdash.modules.containers import client as ct_client
from nexusdash.modules.containers import console as ct_console


def test_scan_finds_every_module_file():
    mods = discovery.load_builtin_modules()
    names = {m.__name__.rsplit('nexusdash.modules.', 1)[-1] for m in mods}
    assert names == {
        'disks', 'gpu', 'logs', 'zfs', 'iscsi', 'nfs', 'smb', 'minidlna',
        'replication', 'maintenance', 'llama', 'network', 'schedules', 'lvm',
        'mdraid', 'firewall', 'caddy', 'dnsmasq', 'docker', 'docker_console',
        'docker_compose', 'containers.client', 'containers.instances',
        'containers.images', 'containers.networks', 'containers.portforward',
        'containers.console', 'updates'}


def test_legacy_facade_order_reproduced():
    mods = discovery.load_builtin_modules()
    names = [m.__name__.rsplit('nexusdash.modules.', 1)[-1] for m in mods]
    # Legacy set first in the pinned order; post-3.0 module files (updates)
    # sort after it alphabetically.
    assert tuple(names) == discovery._LEGACY_FACADE_ORDER + ('updates',)


def test_shape_classification():
    mods = discovery.load_builtin_modules()
    bare = discovery.bare_blueprint_modules(mods)
    # logs/network live under modules/ but are core in role; ct_console is the
    # self-gating VGA page. None may ever grow a MODULE descriptor silently.
    assert set(bare) == {logs, network, ct_console}
    # docker_console is pure websocket (no bp, no MODULE) — import-only plus
    # registrar; client is import-only.
    assert not hasattr(docker_console, 'bp')
    assert not hasattr(docker_console, 'MODULE')
    assert not hasattr(ct_client, 'bp')
    regs = discovery.ws_registrars(mods)
    assert ct_console.register_ws in regs
    assert docker_console.register_ws in regs
    assert len(regs) == 2


def test_collect_descriptors_order_and_metrics_extra():
    mods = discovery.load_builtin_modules()
    descs = discovery.collect_descriptors(mods, extra=(metrics.MODULE,))
    ids = [d['id'] for d, _ in descs]
    assert ids == [m['id'] for m in app.MODULES]     # exact nav order
    assert ids[-2:] == ['metrics', 'updates']        # orders 230, 240 sort last
    assert all(src == 'builtin' for _, src in descs)


def test_collect_descriptors_plugin_dedupe_and_default_order():
    mods = discovery.load_builtin_modules()
    shadow = {'id': 'zfs', 'label': 'evil', 'category': 'X', 'blueprint': None}
    plug = {'id': 'zzplug', 'label': 'P', 'category': 'X', 'blueprint': None}
    descs = discovery.collect_descriptors(
        mods, plugins=[(shadow, 'plugin'), (plug, 'plugin')],
        extra=(metrics.MODULE,))
    by_id = {d['id']: (d, src) for d, src in descs}
    # builtin wins the id collision — the shadow descriptor is dropped
    assert by_id['zfs'][0]['label'] != 'evil'
    assert by_id['zfs'][1] == 'builtin'
    # no-order plugin defaults to 1000: after every builtin (metrics=230 last)
    ids = [d['id'] for d, _ in descs]
    assert ids.index('zzplug') > ids.index('metrics')
    assert by_id['zzplug'][1] == 'plugin'


def test_ws_registrars_descriptor_key_and_dedupe():
    mods = discovery.load_builtin_modules()
    fn = lambda sock: None
    descs = [{'id': 'p', 'websockets': fn},
             {'id': 'q', 'websockets': fn},                  # dup by identity
             {'id': 'r', 'websockets': ct_console.register_ws}]  # dup of attr
    regs = discovery.ws_registrars(mods, descs)
    assert regs.count(fn) == 1
    assert regs.count(ct_console.register_ws) == 1
