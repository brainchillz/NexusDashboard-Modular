"""Host power endpoints (core, /api/system/reboot|shutdown).

Under TESTING the scheduler refuses to arm (scheduled:false) — a test run
must never power-cycle the machine it runs on; the response contract is
still fully exercised.
"""
import pytest

import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


def test_reboot_endpoint(client):
    r = client.post('/api/system/reboot')
    assert r.status_code == 200
    j = r.get_json()
    assert j['success'] is True and j['action'] == 'reboot'
    assert j['scheduled'] is False   # TESTING guard held


def test_shutdown_endpoint(client):
    r = client.post('/api/system/shutdown')
    assert r.status_code == 200
    j = r.get_json()
    assert j['success'] is True and j['action'] == 'shutdown'
    assert j['scheduled'] is False


def test_power_requires_admin(client, monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('viewer', 'readonly'))
    assert client.post('/api/system/reboot').status_code == 403
    assert client.post('/api/system/shutdown').status_code == 403


def test_power_is_post_only(client):
    assert client.get('/api/system/reboot').status_code == 405
    assert client.get('/api/system/shutdown').status_code == 405
