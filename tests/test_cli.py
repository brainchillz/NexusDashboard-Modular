"""CLI dispatch — a matched command must always yield an exit code.

app.py starts the web SERVER when dispatch() returns None ("no command
matched"). cli_alerts_tick's missing `return 0` made every alerts-tick since
the 2.0.0 cutover fall through into a second server that died on the bound
port (and would have BECOME the server had the real one been down). These
tests pin both the dispatch hardening and the tick's exit codes.
"""
import pytest

import app
from nexusdash import cli


def test_dispatch_coerces_none_to_zero(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, 'fake-tick', lambda: None)
    assert cli.dispatch(['app.py', 'fake-tick']) == 0


def test_dispatch_passes_through_real_exit_codes(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, 'fake-tick', lambda: 3)
    assert cli.dispatch(['app.py', 'fake-tick']) == 3
    monkeypatch.setitem(cli.COMMANDS, 'fake-argv', lambda argv: None)
    assert cli.dispatch(['app.py', 'fake-argv']) == 0


def test_dispatch_unmatched_returns_none():
    # None here is the "run the server" signal — only for NO command.
    assert cli.dispatch(['app.py']) is None
    assert cli.dispatch(['app.py', 'no-such-command']) is None


@pytest.mark.parametrize('name', ['alerts-tick', 'history-tick'])
def test_tick_commands_exist(name):
    cmds = dict(cli.COMMANDS)
    from nexusdash.core import registry
    cmds.update(registry.cli_commands())
    assert name in cmds


def test_alerts_tick_returns_zero(monkeypatch):
    monkeypatch.setattr(app, 'load_notifications', lambda: {'state': {}})
    monkeypatch.setattr(app, '_compute_alerts', lambda: [])
    monkeypatch.setattr(app, 'save_notifications', lambda cfg: None)
    assert app.cli_alerts_tick() == 0
