"""Filesystems row (3.7.0): one box per real, persistent filesystem — a ZFS
pool once (usable figures), a disk filesystem, a network mount. tmpfs,
overlay, squashfs (snaps), loop images, bind duplicates, read-only and the
container/run/snap trees are left out. /proc/mounts is faked."""
import pytest

import app
from nexusdash.core import summary as summ, history as hist

MOUNTS = """/dev/mapper/vg-root / ext4 rw,relatime 0 0
/dev/nvme0n1p2 /boot ext4 rw,relatime 0 0
/dev/nvme0n1p1 /boot/efi vfat rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid 0 0
tmpfs /tmp tmpfs rw 0 0
overlay /var/lib/docker/overlay2/abc/merged overlay rw 0 0
/dev/loop3 /snap/core/1 squashfs ro 0 0
/dev/loop9 /mnt/image ext4 rw 0 0
SPINNING/apt /SPINNING/apt zfs rw,xattr 0 0
SPINNING /SPINNING zfs rw,xattr 0 0
FLASH /FLASH zfs rw,xattr 0 0
FLASH/vm /FLASH/vm zfs rw 0 0
node2:/export/llm /mnt/llm nfs4 rw,hard 0 0
/dev/mapper/vg-root /home/bind ext4 rw,relatime 0 0
/dev/sdz1 /mnt/readonly ext4 ro 0 0
/dev/sdy1 /media/with\\040space xfs rw 0 0
"""
POOLS = {'SPINNING': {'size': 100, 'alloc': 40, 'free': 60, 'health': 'ONLINE'},
         'FLASH': {'size': 10, 'alloc': 9, 'free': 1, 'health': 'ONLINE'}}


def test_select_mounts_is_an_allowlist_with_one_box_per_pool():
    sel = summ._select_mounts(summ._parse_mount_table(MOUNTS))
    assert sel == [
        ('/dev/mapper/vg-root', '/', 'ext4', 'disk'),
        ('/dev/nvme0n1p2', '/boot', 'ext4', 'disk'),
        ('/dev/nvme0n1p1', '/boot/efi', 'vfat', 'disk'),
        ('SPINNING', '/SPINNING', 'zfs', 'zfs'),            # child seen first, root mount still wins
        ('FLASH', '/FLASH', 'zfs', 'zfs'),
        ('node2:/export/llm', '/mnt/llm', 'nfs4', 'remote'),
        ('/dev/sdy1', '/media/with space', 'xfs', 'disk')]  # octal-escaped space decoded


def test_mount_usage_rows(monkeypatch):
    monkeypatch.setattr(summ, '_statvfs_usage', lambda path, timeout=None:
                        None if path == '/mnt/llm' else (1000, 250, 700, 26))
    rows = {r['mount']: r for r in summ._mount_usage(MOUNTS, POOLS)}
    assert rows['/']['pct'] == 26 and rows['/']['used'] == 250 and rows['/']['kind'] == 'disk'
    assert rows['/SPINNING'] == {'mount': '/SPINNING', 'source': 'SPINNING', 'fstype': 'zfs', 'kind': 'zfs',
                                 'total': 100, 'used': 40, 'avail': 60, 'pct': 40}
    assert rows['/FLASH']['pct'] == 90
    assert rows['/mnt/llm']['pct'] is None and rows['/mnt/llm']['error'] == 'unreachable' and rows['/mnt/llm']['kind'] == 'remote'
    assert '/tmp' not in rows and '/snap/core/1' not in rows and '/mnt/image' not in rows and '/home/bind' not in rows
    assert summ._mount_usage('', {}) == []


def test_statvfs_usage_real_root():
    u = summ._statvfs_usage('/')
    assert u and u[0] > 0 and 0 <= u[3] <= 100


def test_remote_statvfs_times_out(monkeypatch):
    import time
    def slow(path):
        time.sleep(3)
        raise AssertionError('never')
    monkeypatch.setattr(summ.os, 'statvfs', slow)
    t0 = time.time()
    assert summ._statvfs_usage('/mnt/x', timeout=0.2) is None
    assert time.time() - t0 < 1.5


def test_resources_route_and_history(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(app, '_host_temps', lambda *a, **k: [])
    monkeypatch.setattr(summ, '_mount_usage', lambda *a, **k: [{'mount': '/', 'source': 'x', 'fstype': 'ext4', 'kind': 'disk',
                                                                'total': 10, 'used': 5, 'avail': 5, 'pct': 50}])
    app.app.config['TESTING'] = True
    r = app.app.test_client().get('/api/system/resources').get_json()
    assert r['mounts'][0]['pct'] == 50
    monkeypatch.setattr(hist, '_mount_usage', summ._mount_usage)
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    monkeypatch.setattr(app, '_system_resources', lambda: {})
    rows = {(m, l): v for m, l, v in hist._history_sample()}
    assert rows[('fs_pct', '/')] == 50 and 'fs_pct' in app.HISTORY_METRICS
