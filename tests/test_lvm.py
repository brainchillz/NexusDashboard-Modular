"""LVM module — the argv the extend endpoints build, and the silent-no-op
guard. All lvextend output is faked; no LVM or root needed to run these.

Regression origin (2026-08-10, node3): the GUI's extend field accepted only a
bare '100%FREE', which lvextend reads as "SET the size to 100% of the FREE
space" rather than "ADD it". On a VG whose free space equalled the LV's
current size that resolved to no change at all — and lvextend still exited 0
with "successfully resized", so the dashboard reported success for a no-op.
"""
import pytest

import app


# Real output from node3's `lvextend --test -l 100%FREE -r ubuntu-vg/ubuntu-lv`
# (116G VG, 58G LV, 58G free — free space happened to equal the LV size).
LVEXTEND_NOOP = """  New size (14872 extents) matches existing size (14872 extents).
  File system ext4 found on ubuntu-vg/ubuntu-lv mounted at /.
  Size of logical volume ubuntu-vg/ubuntu-lv unchanged from 58.09 GiB (14872 extents).
  Logical volume ubuntu-vg/ubuntu-lv successfully resized.
"""

LVEXTEND_OK = """  Size of logical volume ubuntu-vg/ubuntu-lv changed from 58.09 GiB (14872 extents) to <116.19 GiB (29744 extents).
  File system ext4 found on ubuntu-vg/ubuntu-lv mounted at /.
  Extending file system ext4 to <116.19 GiB (124756721664 bytes) on /dev/mapper/ubuntu--vg-ubuntu--lv...
  Logical volume ubuntu-vg/ubuntu-lv successfully resized.
"""


def _fake_run(stdout='', rc=0):
    """run() stub that records every argv it is handed."""
    calls = []

    def fake(args, **kw):
        calls.append(list(args))
        return (stdout, '', rc)

    return fake, calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# ─── Size parsing / argv ─────────────────────────────────────────────────

@pytest.mark.parametrize('size', ['+100%FREE', '+50%VG', '100%FREE', '50%VG'])
def test_extend_accepts_percent_with_and_without_plus(client, monkeypatch, size):
    """'+N%' must reach lvextend verbatim — the '+' is what makes it ADD."""
    fake, calls = _fake_run(LVEXTEND_OK)
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/lvm/lv/ubuntu-vg/ubuntu-lv/extend', json={'size': size})
    assert r.status_code == 200 and r.get_json()['success']
    assert calls == [['lvextend', '-l', size, 'ubuntu-vg/ubuntu-lv']]


def test_extend_percent_plus_survives_with_resize_fs(client, monkeypatch):
    fake, calls = _fake_run(LVEXTEND_OK)
    monkeypatch.setattr(app, 'run', fake)
    client.post('/api/lvm/lv/ubuntu-vg/ubuntu-lv/extend',
                json={'size': '+100%FREE', 'resize_fs': True})
    assert calls == [['lvextend', '-l', '+100%FREE', '-r', 'ubuntu-vg/ubuntu-lv']]


def test_extend_absolute_size_uses_dash_L(client, monkeypatch):
    fake, calls = _fake_run(LVEXTEND_OK)
    monkeypatch.setattr(app, 'run', fake)
    client.post('/api/lvm/lv/ubuntu-vg/ubuntu-lv/extend', json={'size': '+10G'})
    assert calls == [['lvextend', '-L', '+10G', 'ubuntu-vg/ubuntu-lv']]


@pytest.mark.parametrize('size', ['', 'all', '100%', '1000%FREE', '+%FREE', '10G; rm -rf /'])
def test_extend_rejects_junk_sizes(client, monkeypatch, size):
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/lvm/lv/ubuntu-vg/ubuntu-lv/extend', json={'size': size})
    assert r.status_code == 400 and not r.get_json()['success']
    assert calls == []                       # nothing ran


def test_lv_create_still_refuses_a_leading_plus(client, monkeypatch):
    """'+100%FREE' is meaningless to lvcreate — create keeps the strict regex."""
    fake, calls = _fake_run()
    monkeypatch.setattr(app, 'run', fake)
    r = client.post('/api/lvm/lv',
                    json={'vg': 'ubuntu-vg', 'name': 'new', 'size': '+100%FREE'})
    assert r.status_code == 400 and calls == []
    # ...while the bare form still builds the create argv it always did.
    r = client.post('/api/lvm/lv',
                    json={'vg': 'ubuntu-vg', 'name': 'new', 'size': '100%FREE'})
    assert r.status_code == 200
    assert calls == [['lvcreate', '-l', '100%FREE', '-n', 'new', 'ubuntu-vg']]


# ─── The silent no-op ────────────────────────────────────────────────────

def test_extend_flags_a_zero_change_resize(client, monkeypatch):
    """rc=0 + "matches existing size" is a no-op, not a completed extend."""
    fake, _ = _fake_run(LVEXTEND_NOOP)
    monkeypatch.setattr(app, 'run', fake)
    j = client.post('/api/lvm/lv/ubuntu-vg/ubuntu-lv/extend',
                    json={'size': '100%FREE'}).get_json()
    assert j['success']                      # the command really did exit 0
    assert 'No change' in j['warning']
    assert '+100%FREE' in j['warning']       # tells the user the fix


def test_extend_that_grows_carries_no_warning(client, monkeypatch):
    fake, _ = _fake_run(LVEXTEND_OK)
    monkeypatch.setattr(app, 'run', fake)
    j = client.post('/api/lvm/lv/ubuntu-vg/ubuntu-lv/extend',
                    json={'size': '+100%FREE'}).get_json()
    assert j['success'] and 'warning' not in j


def test_extend_failure_is_untouched_by_the_noop_check(client, monkeypatch):
    fake, _ = _fake_run('  Insufficient free space', rc=5)
    monkeypatch.setattr(app, 'run', fake)
    j = client.post('/api/lvm/lv/ubuntu-vg/ubuntu-lv/extend',
                    json={'size': '+100%FREE'}).get_json()
    assert not j['success'] and 'warning' not in j
