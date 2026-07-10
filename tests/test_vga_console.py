"""Graphical (SPICE/VGA) console — the VM framebuffer bridge.

Covers the page-route gating (admin + instances-toggle + name validation), the
`type:vga` console-op parser, and the rendered page's wiring. The websocket pump
itself is a thin raw-byte relay over a live daemon socket (settled by the spike);
the gating and op-open logic are the parts worth unit-testing.
"""
import json

import pytest

import app
from nexusdash.modules.containers import console


@pytest.fixture
def admin_client(monkeypatch):
    # Authenticated admin identity without touching auth.json (same pattern as
    # tests/test_registry.py's client fixture).
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    app.app.config['TESTING'] = True
    return app.app.test_client()


def _enable(monkeypatch):
    monkeypatch.setattr(console, 'load_disabled_modules', lambda: set())


# ─── page-route gating ──────────────────────────────────────────────────
def test_vga_page_admin_ok(admin_client, monkeypatch):
    _enable(monkeypatch)
    r = admin_client.get('/console/vga/VmTest')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert '/ws/vga/' in body
    assert '/static/vendor/spice/src/main.js' in body
    assert 'VmTest' in body


def test_vga_page_requires_admin(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('v', 'viewer'))
    _enable(monkeypatch)
    app.app.config['TESTING'] = True
    r = app.app.test_client().get('/console/vga/VmTest')
    assert r.status_code == 403


def test_vga_page_module_disabled(admin_client, monkeypatch):
    monkeypatch.setattr(console, 'load_disabled_modules', lambda: {'instances'})
    r = admin_client.get('/console/vga/VmTest')
    assert r.status_code == 404
    assert 'disabled' in r.get_data(as_text=True)


def test_vga_page_invalid_name(admin_client, monkeypatch):
    _enable(monkeypatch)
    # underscore fails RE_INSTANCE but still routes as a bare path segment.
    r = admin_client.get('/console/vga/bad_name')
    assert r.status_code == 400


# ─── type:vga op parsing ────────────────────────────────────────────────
def test_open_vga_console_parses_op_and_fd(monkeypatch):
    resp = {'type': 'async',
            'operation': '/1.0/operations/abcd-1234/',
            'metadata': {'metadata': {'fds': {'0': 'SECRET0', 'control': 'SECRETC'}}}}
    monkeypatch.setattr(console, 'lxd_raw',
                        lambda m, p, b: (202, json.dumps(resp).encode()))
    op, fd0 = console._open_vga_console('VmTest')
    assert op == 'abcd-1234'
    assert fd0 == 'SECRET0'


def test_open_vga_console_raises_on_error(monkeypatch):
    resp = {'type': 'error', 'error': 'Instance is not a virtual machine',
            'error_code': 400}
    monkeypatch.setattr(console, 'lxd_raw',
                        lambda m, p, b: (400, json.dumps(resp).encode()))
    with pytest.raises(console.LxdError):
        console._open_vga_console('someCT')


# ─── rendered page wiring / escaping ────────────────────────────────────
def test_render_vga_page_wires_name_into_ws():
    html = console._render_vga_page('my-vm')
    assert 'const NAME = "my-vm";' in html
    assert "/ws/vga/' + encodeURIComponent(NAME)" in html
