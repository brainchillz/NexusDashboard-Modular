"""Frontend global-scope guard.

The browser loads static/js/*.js as plain scripts sharing one global scope, so a
name declared in one file is visible in all of them. That makes a lost `let` on
a shared state variable invisible to every other check we run: the file still
parses, `node --check` passes, and in sloppy mode an ASSIGNMENT to an undeclared
name silently invents a global — so the page appears to work. It only breaks
when some path READS the name first, which is a ReferenceError that takes out
the whole page.

That is not hypothetical. `let _netCountdown = null;` was dropped when the
single-file app was split into per-page files; page_network() reads it on entry,
so the Network page threw "_netCountdown is not defined" for admins on every
node until it was reported from node5. The same thing happened again to
ai.js's llama state during the model-downloader work.
"""
import re
import glob
import os

JS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'static', 'js')

# Names the guard should tolerate while a known gap is open. Empty since 3.4.3:
# _setBridgeIfaces (called by addBridgeIface/removeBridgeIface, defined nowhere
# — it never existed, even pre-split) sat here until it was written on top of
# the networks PATCH route. Add to this set only with a comment saying why.
KNOWN_MISSING = set()


def _declared_and_used():
    text = '\n'.join(open(f).read() for f in sorted(glob.glob(os.path.join(JS_DIR, '*.js'))))
    declared = set()
    # `let a = 1, b = 2;` declares both names, so split the whole clause.
    for m in re.finditer(r'\b(?:let|const|var)\s+([^;\n]+)', text):
        for part in m.group(1).split(','):
            name = part.strip().split('=')[0].strip()
            if re.fullmatch(r'[A-Za-z_$][\w$]*', name):
                declared.add(name)
    declared |= set(re.findall(r'\bfunction\s+([A-Za-z_$][\w$]*)', text))
    used = set(re.findall(r'\b(_[A-Za-z][\w$]*)\b', text))
    # `obj._foo` / `'_foo'` are property accesses, not globals.
    used = {n for n in used if not re.search(r'[.\'"]' + re.escape(n), text)}
    return declared, used


def test_no_undeclared_module_globals():
    declared, used = _declared_and_used()
    missing = sorted(used - declared - KNOWN_MISSING)
    assert not missing, (
        'These underscore-prefixed globals are used by the frontend but declared '
        'nowhere. Reading one before it is assigned is a ReferenceError that '
        'kills the page: %s' % missing)


def test_known_missing_list_stays_honest():
    """An entry whose function now exists must be removed, so the set can never
    quietly mask a future regression of the same name."""
    declared, _used = _declared_and_used()
    still_missing = {n for n in KNOWN_MISSING if n not in declared}
    assert still_missing == KNOWN_MISSING, (
        'KNOWN_MISSING is stale — these now exist and should be removed from it: '
        '%s' % sorted(KNOWN_MISSING - still_missing))


def test_every_api_helper_method_used_is_defined():
    """`API.patch(...)` was called from containers.js for two PATCH routes while
    core.js defined only get/post/put/delete — a TypeError at click time that no
    parse check can see. Every API.<method>( used anywhere must be a method of
    the API object in core.js."""
    core = open(os.path.join(JS_DIR, 'core.js')).read()
    api_body = core.split('const API = {', 1)[1].split('\n};', 1)[0]
    defined = set(re.findall(r'^\s*(?:async\s+)?([a-zA-Z]\w*)\s*\(', api_body, re.M))
    text = '\n'.join(open(f).read() for f in sorted(glob.glob(os.path.join(JS_DIR, '*.js'))))
    used = set(re.findall(r'\bAPI\.([a-zA-Z]\w*)\s*\(', text))
    assert used - defined == set(), (
        'API.<method> calls with no such method on the API object in core.js: '
        '%s' % sorted(used - defined))
