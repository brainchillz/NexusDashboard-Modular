"""Regression tests for the 2026-07-29 shared-core auth fixes.

test_guards.py already pins _user_role's fail-safe default; these cover the two
behaviors that default alone does not: a deleted-user session must be rejected
(not resolved), and login must cost one password hash on every path so response
time cannot enumerate usernames.
"""
import app
from nexusdash.core import auth


def test_deleted_user_session_is_rejected_not_promoted(monkeypatch):
    """A live session whose user has been removed from the store must resolve to
    unauthenticated, never to admin."""
    monkeypatch.setattr(auth, '_users', lambda: {})          # account deleted
    app.app.secret_key = app.app.secret_key or 'test-secret'  # sessions need a key
    with app.app.test_request_context():
        from flask import session
        session['user'] = 'ghost'
        name, role = auth._resolve_identity()
    assert name is None and role is None
    assert auth._user_role(None) == 'readonly'               # never admin


def test_login_hashes_once_per_path(monkeypatch):
    """Unknown user and known-user-wrong-password each cost exactly one password
    hash — no timing signal distinguishes a real username from a fake one."""
    calls = {'n': 0}
    real = auth.check_password_hash
    monkeypatch.setattr(auth, 'check_password_hash',
                        lambda h, p: (calls.__setitem__('n', calls['n'] + 1), real(h, p))[1])
    monkeypatch.setattr(auth, 'load_config',
                        lambda: {'users': {'admin': {'password': auth.generate_password_hash('right'),
                                                      'role': 'admin'}}})
    c = app.app.test_client()
    calls['n'] = 0
    c.post('/api/login', json={'username': 'ghost', 'password': 'x'})       # unknown
    assert calls['n'] == 1
    calls['n'] = 0
    c.post('/api/login', json={'username': 'admin', 'password': 'wrong'})   # known, wrong
    assert calls['n'] == 1
