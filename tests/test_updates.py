"""updates module: pure parsers, cached-state endpoints, disable gating.

The check itself shells out (apt-get -s / dnf check-update) from a background
thread, which never starts under TESTING — endpoints here only ever see the
module-level cache, so tests are command-free and deterministic.
"""
import pytest

import app
from nexusdash.modules import updates as upd


APT_SIM = '''\
NOTE: This is only a simulation!
Inst libssl3t64 [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-security [amd64])
Conf libssl3t64 (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-security [amd64])
Inst base-files [13ubuntu10.1] (13ubuntu10.2 Ubuntu:24.04/noble-updates [amd64])
Inst new-dep (1.2-1 Ubuntu:24.04/noble [amd64])
Remv old-cruft [0.9]
'''

DNF_CHECK = '''\
kernel.x86_64                     5.14.0-503.14.1.el9_5            baseos
openssl.x86_64                    1:3.2.2-6.el9_5                  baseos
zfs.x86_64                        2.1.15-1.el9                     zfs

Obsoleting Packages
grub2-tools.x86_64                1:2.06-77.el9                    baseos
    grub2-tools.x86_64            1:2.06-70.el9                    @baseos
'''

DNF_SECURITY = '''\
kernel.x86_64                     5.14.0-503.14.1.el9_5            baseos
openssl.x86_64                    1:3.2.2-6.el9_5                  baseos
'''


def test_parse_apt_dist_upgrade():
    rows = upd.parse_apt_dist_upgrade(APT_SIM)
    assert [r['name'] for r in rows] == ['libssl3t64', 'base-files', 'new-dep']
    ssl = rows[0]
    assert ssl['current'] == '3.0.13-0ubuntu3.4'
    assert ssl['candidate'] == '3.0.13-0ubuntu3.5'
    assert ssl['security'] is True
    assert rows[1]['security'] is False
    # A fresh dependency has no installed version.
    assert rows[2]['current'] == ''


def test_parse_apt_empty():
    assert upd.parse_apt_dist_upgrade('') == []
    assert upd.parse_apt_dist_upgrade('NOTE: This is only a simulation!\n') == []


def test_parse_dnf_check_update():
    rows = upd.parse_dnf_check_update(DNF_CHECK)
    # The Obsoleting Packages section (and its continuations) must not count.
    assert [r['name'] for r in rows] == ['kernel.x86_64', 'openssl.x86_64',
                                        'zfs.x86_64']
    assert rows[0]['candidate'] == '5.14.0-503.14.1.el9_5'
    assert rows[0]['origin'] == 'baseos'


def test_check_rhel_marks_security(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if '--security' in args:
            return DNF_SECURITY, '', 100
        return DNF_CHECK, '', 100

    monkeypatch.setattr(upd, 'run', fake_run)
    rows, error = upd._check_rhel()
    assert error is None
    sec = {r['name'] for r in rows if r['security']}
    assert sec == {'kernel.x86_64', 'openssl.x86_64'}
    assert len(calls) == 2


def test_check_rhel_no_updates(monkeypatch):
    # exit 0 = nothing pending; the --security pass must be skipped.
    calls = []
    monkeypatch.setattr(upd, 'run',
                        lambda args, **kw: (calls.append(args) or ('', '', 0)))
    rows, error = upd._check_rhel()
    assert rows == [] and error is None
    assert len(calls) == 1


def test_check_debian_error(monkeypatch):
    monkeypatch.setattr(upd, 'run',
                        lambda args, **kw: ('', 'E: could not open lock', 100))
    rows, error = upd._check_debian()
    assert rows is None and 'lock' in error


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


def test_api_updates_serves_cache(client, monkeypatch):
    monkeypatch.setitem(upd._state, 'available', 3)
    monkeypatch.setitem(upd._state, 'security', 1)
    monkeypatch.setitem(upd._state, 'checked', 1700000000)
    r = client.get('/api/updates')
    assert r.status_code == 200
    j = r.get_json()
    assert j['available'] == 3 and j['security'] == 1
    assert j['manager'] in ('apt', 'dnf')


def test_summary_carries_updates_block(client, monkeypatch):
    monkeypatch.setitem(upd._state, 'available', 2)
    monkeypatch.setitem(upd._state, 'security', 2)
    monkeypatch.setattr(app, 'run', lambda *a, **k: ('', '', 1))
    monkeypatch.setattr(app, '_unit_present', lambda unit: False)
    j = client.get('/api/summary').get_json()
    assert j['updates']['available'] == 2
    assert j['updates']['security'] == 2
    # counts only on the front page — the package list stays on /api/updates
    assert 'packages' not in j['updates']


def test_api_updates_disabled_module_403(client, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'updates'})
    r = client.get('/api/updates')
    assert r.status_code == 403
    assert 'disabled' in r.get_json()['error']


def test_check_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('viewer', 'readonly'))
    r = client.post('/api/updates/check')
    assert r.status_code == 403


# ─── Apply (phase 2) ───────────────────────────────────────────────────

def test_reboot_likely_heuristic():
    assert upd._reboot_likely('linux-image-6.8.0-45-generic')
    assert upd._reboot_likely('kernel.x86_64')
    assert upd._reboot_likely('libssl3t64')
    assert upd._reboot_likely('systemd-resolved')
    assert not upd._reboot_likely('vim')
    assert not upd._reboot_likely('caddy')


def test_apt_progress_step():
    assert upd._apt_progress_step('Unpacking libssl3t64 (3.0.13) over (3.0.12) ...')
    assert upd._apt_progress_step('Setting up base-files (13ubuntu10.2) ...')
    assert not upd._apt_progress_step('Reading package lists...')
    assert not upd._apt_progress_step('Preparing to unpack .../libssl3t64.deb ...')


def test_dnf_progress():
    assert upd._dnf_progress('  Upgrading        : kernel-5.14.0   3/52') == (3, 52)
    assert upd._dnf_progress('  Verifying        : openssl-3.2.2   52/52') == (52, 52)
    assert upd._dnf_progress('Running transaction check') is None


def _reset_apply():
    upd._apply.update({'running': False, 'rc': None, 'started': None,
                       'finished': None, 'log': [], 'done': 0, 'total': 0,
                       'reboot_required': None})


def test_apply_thread_streams_and_finishes(monkeypatch):
    class FakeProc:
        stdout = ['Reading package lists...\n',
                  'Unpacking foo (1.2) over (1.1) ...\n',
                  'Setting up foo (1.2) ...\n']

        def wait(self):
            return 0

        def kill(self):
            pass

    saved = dict(upd._apply)
    try:
        monkeypatch.setattr(upd.subprocess, 'Popen', lambda *a, **k: FakeProc())
        monkeypatch.setattr(upd, '_refresh', lambda: None)
        monkeypatch.setattr(upd, '_reboot_required', lambda: True)
        _reset_apply()
        upd._apply.update({'running': True, 'total': 2})
        upd._apply_thread()
        assert upd._apply['rc'] == 0
        assert upd._apply['done'] == 2          # Unpacking + Setting up
        assert upd._apply['reboot_required'] is True
        assert upd._apply['running'] is False
        assert upd._apply['finished'] is not None
        assert 'Setting up foo (1.2) ...' in upd._apply['log']
    finally:
        upd._apply.clear()
        upd._apply.update(saved)


def test_apply_refused_without_helper(client, monkeypatch):
    monkeypatch.setattr(upd, 'UPDATES_HELPER', '/nonexistent/helper')
    r = client.post('/api/updates/apply')
    assert r.status_code == 400
    assert 'helper' in r.get_json()['error']


def test_apply_refused_when_nothing_pending(client, monkeypatch):
    monkeypatch.setattr(upd, 'UPDATES_HELPER', __file__)   # exists
    monkeypatch.setitem(upd._state, 'available', 0)
    monkeypatch.setitem(upd._state, 'checking', False)
    r = client.post('/api/updates/apply')
    assert r.status_code == 400
    assert 'nothing to apply' in r.get_json()['error']


def test_apply_refused_while_running(client, monkeypatch):
    monkeypatch.setattr(upd, 'UPDATES_HELPER', __file__)
    monkeypatch.setitem(upd._state, 'available', 3)
    monkeypatch.setitem(upd._state, 'checking', False)
    monkeypatch.setitem(upd._apply, 'running', True)
    r = client.post('/api/updates/apply')
    assert r.status_code == 409


def test_get_updates_carries_apply_block(client, monkeypatch):
    monkeypatch.setattr(upd, 'UPDATES_HELPER', __file__)
    r = client.get('/api/updates').get_json()
    assert r['apply_available'] is True
    assert set(r['apply']) >= {'running', 'rc', 'done', 'total',
                               'log', 'reboot_required'}


def test_apply_requires_admin(client, monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('viewer', 'readonly'))
    r = client.post('/api/updates/apply')
    assert r.status_code == 403
