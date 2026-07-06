"""CLI subcommands (invoked as `python app.py <command>`).

Stage 0 carries set-password; the timer tick commands (autosnap-tick,
replicate-tick, alerts-tick, maintenance-tick, history-tick) move here with
their modules in Stage 1 and are dispatched from COMMANDS so systemd units
keep working unchanged.
"""
import os

from werkzeug.security import generate_password_hash

from .core.auth import (RE_USERNAME, MIN_PASSWORD_LEN, ensure_bootstrap,
                        save_config)


def cli_set_password(argv):
    import getpass
    user = argv[2] if len(argv) > 2 else 'admin'
    if not RE_USERNAME.match(user):
        print('Invalid username')
        return 1
    pw = os.environ.get('DASHBOARD_ADMIN_PASSWORD')
    if not pw:
        pw = getpass.getpass(f'New password for {user}: ')
        if pw != getpass.getpass('Confirm password: '):
            print('Passwords do not match')
            return 1
    if len(pw) < MIN_PASSWORD_LEN:
        print(f'Password must be at least {MIN_PASSWORD_LEN} characters')
        return 1
    cfg = ensure_bootstrap()
    users = cfg.setdefault('users', {})
    rec = users[user] if isinstance(users.get(user), dict) else {'role': 'admin', 'smb': False}
    rec['password'] = generate_password_hash(pw)
    rec.pop('must_change', None)  # operator set it explicitly — no forced change
    users[user] = rec
    save_config(cfg)
    print(f'Password updated for {user}')
    return 0


# command name -> callable() or callable(argv) -> exit code. Core commands are
# listed here; MODULE-owned tick commands (autosnap/replicate/maintenance) come
# from the registry (each module's descriptor carries its cli dict), so the
# systemd-facing names (`python app.py <name>-tick`) keep working unchanged.
def _tick(module_path, func_name):
    def _run(argv=None):
        import importlib
        mod = importlib.import_module(module_path, package=__package__)
        return getattr(mod, func_name)()
    return _run


COMMANDS = {
    'set-password': cli_set_password,
    'alerts-tick': _tick('.core.alerts', 'cli_alerts_tick'),
    'history-tick': _tick('.core.history', 'cli_history_tick'),
}


def dispatch(argv):
    """Return an exit code if argv names a CLI subcommand, else None. Module
    commands require the app to be built (create_app registers descriptors) —
    app.py builds it before dispatching."""
    import inspect
    from .core import registry
    commands = dict(COMMANDS)
    commands.update(registry.cli_commands())
    if len(argv) > 1 and argv[1] in commands:
        fn = commands[argv[1]]
        # Pass argv only to commands that accept it (a tick raising TypeError
        # internally must not be retried without args).
        if len(inspect.signature(fn).parameters) >= 1:
            rc = fn(argv)
        else:
            rc = fn()
        # A matched command must ALWAYS yield an exit code: app.py starts the
        # web server when dispatch returns None, so a tick that forgets its
        # `return 0` would fall through into a second (rogue) server.
        return 0 if rc is None else rc
    return None
