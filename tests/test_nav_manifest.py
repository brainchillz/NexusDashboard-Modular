"""/api/modules/nav — the manifest the frontend renders the whole sidebar
from. The expected literal below is hand-derived from the pre-3.0 static nav
in templates/index.html (category order, item order, labels, icons,
admin-only flags, module linkage): the manifest MUST reproduce it exactly or
the rendered sidebar drifts from the original DOM.
"""
import pytest

import app
from nexusdash.core import registry


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# (cat, label, [(page, label, icon, module, admin_only)]) in sidebar order.
EXPECTED_NAV = [
    ('storage', 'Storage MGMT', [
        ('disks', 'Disks', 'disk', 'disks', False),
        ('zfs', 'ZFS Pools', 'db', 'zfs', False),
        ('lvm', 'LVM', 'lay', 'lvm', False),
        ('mdraid', 'MD RAID', 'box3', 'mdraid', False),
        ('schedules', 'Auto-Snapshots', 'clock', 'schedules', False),
        ('replication', 'Replication', 'repeat', 'replication', False),
        ('maintenance', 'Maintenance', 'wrench', 'maintenance', False)]),
    ('sharing', 'Sharing', [
        ('iscsi', 'iSCSI Targets', 'loc', 'iscsi', False),
        ('nfs', 'NFS Exports', 'share', 'nfs', False),
        ('smb', 'SMB/CIFS', 'folder', 'smb', False),
        ('minidlna', 'DLNA Media', 'camera', 'minidlna', False)]),
    ('ai', 'AI Tools', [
        ('llamacpp', 'LLama.cpp', 'flame', 'llamacpp', False),
        ('gpu', 'GPU', 'cpu', 'gpu', False)]),
    ('lxd', 'LXD / Incus', [
        ('instances', 'Instances', 'mon', 'instances', False),
        ('images', 'Images', 'pkg', 'images', False),
        ('ctnetworks', 'Instance Networks', 'net', 'ctnetworks', False),
        ('portforward', 'Port Forward', 'swap', 'portforward', False)]),
    ('docker', 'Docker', [
        ('docker', 'Containers & Images', 'pkg', 'docker', False),
        ('compose', 'Compose Stacks', 'box3', 'compose', False)]),
    ('web', 'Web', [
        ('caddy', 'Caddy Proxy', 'link', 'caddy', True)]),
    ('dns', 'DNS', [
        ('dnshosts', 'DNS Overrides', 'glb', 'dnsmasq', False),
        ('dhcp', 'DHCP', 'swap', 'dnsmasq', False),
        ('dnsconfig', 'DNS Config', 'file', 'dnsmasq', True)]),
    ('system', 'System', [
        ('services', 'Services', 'toggle', None, False),
        ('tasks', 'Scheduled Tasks', 'cal', None, False),
        ('logs', 'Logs', 'log', None, False),
        ('network', 'Network', 'net', None, True),
        ('firewall', 'Firewall', 'shield', 'firewall', True),
        ('account', 'My Account', 'user', None, False),
        ('users', 'Users & Tokens', 'users', None, True),
        ('notifications', 'Notifications', 'bell', None, True),
        ('certificate', 'Certificate', 'cert', None, True),
        ('audit', 'Audit Log', 'list', None, True),
        ('modules', 'Modules', 'sli', None, True)]),
]


def test_nav_manifest_matches_pre30_sidebar(client):
    nav = client.get('/api/modules/nav').get_json()['nav']
    got = [(c['cat'], c['label'],
            [(i['page'], i['label'], i['icon'], i['module'], i['admin_only'])
             for i in c['items']])
           for c in nav['categories']]
    assert got == EXPECTED_NAV


def test_nav_manifest_modules_shape(client):
    body = client.get('/api/modules/nav').get_json()
    mods = {m['id']: m for m in body['modules']}
    assert set(mods) == app.MODULE_IDS
    # /api/modules' original keys are all present per entry, plus the extras
    for m in body['modules']:
        assert {'id', 'label', 'category', 'enabled', 'loaded', 'installed',
                'builtin', 'version', 'assets', 'dashboard_card',
                'ui_pages'} <= set(m)
        assert m['builtin'] is True
    # metrics: registered, default-off, no nav entry anywhere
    assert mods['metrics']['enabled'] is True     # load_disabled stubbed empty
    all_pages = {i['page'] for c in body['nav']['categories'] for i in c['items']}
    assert 'metrics' not in all_pages


def test_nav_endpoint_never_module_gated():
    assert registry.module_for_endpoint('registry.modules_nav_get') is None
