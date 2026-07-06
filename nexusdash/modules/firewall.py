"""Host firewall (ufw) — simple inbound allow/deny control.

ufw only, by design: the goal is a plain "block or allow outside traffic in"
switch, and every node that wants it runs ufw. On a host without ufw (or
before the sudoers rule lands) the page reports available/status_known false
and everything degrades gracefully — same contract as Containers without LXD.

Lockout guard: changes that could cut off the port serving this UI (a deny
rule on it, or removing/replacing its last allow under a deny default) are
never refused outright — they come back needs_confirm with a warning and go
through when the caller re-submits with confirm. Enabling the firewall (or
defaulting to deny) auto-allows the port only when NO allow for it exists,
so a deliberately scoped or removed rule stays as the operator left it.
"""
import re
import shutil
from flask import Blueprint, jsonify, request

from ..core.config import DASHBOARD_PORT
from ..core.runcmd import run, err

bp = Blueprint('firewall', __name__)

# One `ufw status numbered` line: "[ 2] 22/tcp   ALLOW IN   1.2.3.0/24  # ssh"
RE_UFW_RULE = re.compile(
    r'^\[\s*(\d+)\]\s+(.+?)\s{2,}(ALLOW|DENY|REJECT|LIMIT)(?:\s+(IN|OUT|FWD))?'
    r'\s+(.*?)(?:\s+#\s?(.*))?$')
RE_UFW_DEFAULT = re.compile(r'^Default:\s*(\w+) \(incoming\), (\w+) \(outgoing\)', re.M)
RE_FW_SOURCE = re.compile(r'^[0-9a-fA-F:.]+(?:/[0-9]{1,3})?\Z')
RE_FW_COMMENT = re.compile(r'^[A-Za-z0-9 _.:-]{1,64}\Z')

FW_ACTIONS = ('allow', 'deny', 'reject', 'limit')


def _parse_ufw_numbered(text):
    rules = []
    for line in (text or '').splitlines():
        m = RE_UFW_RULE.match(line.rstrip())
        if not m:
            continue
        to, src = m.group(2).strip(), (m.group(5) or '').strip()
        rules.append({
            'number': int(m.group(1)),
            'to': to.replace(' (v6)', ''),
            'action': m.group(3),
            'direction': m.group(4) or 'IN',
            'from': src.replace(' (v6)', ''),
            'comment': (m.group(6) or '').strip(),
            'v6': '(v6)' in to or '(v6)' in src,
        })
    return rules


def _ufw_status():
    """Parsed ufw state. status_known stays False when ufw can't be queried
    (not installed, or no sudoers rule yet) so callers never mistake
    "couldn't ask" for "inactive"."""
    st = {'available': bool(shutil.which('ufw')), 'status_known': False,
          'active': False, 'defaults': {}, 'rules': []}
    if not st['available']:
        return st
    out, _, rc = run(['ufw', 'status', 'verbose'])
    if rc != 0:
        return st
    st['status_known'] = True
    st['active'] = out.strip().lower().startswith('status: active')
    m = RE_UFW_DEFAULT.search(out)
    if m:
        st['defaults'] = {'incoming': m.group(1), 'outgoing': m.group(2)}
    if st['active']:
        out, _, rc = run(['ufw', 'status', 'numbered'])
        if rc == 0:
            st['rules'] = _parse_ufw_numbered(out)
    return st


def _dashboard_rule_match(to):
    """Does a rule's To column cover the dashboard's own TCP port?"""
    return to in ('%d/tcp' % DASHBOARD_PORT, str(DASHBOARD_PORT))


def _covers_dashboard(rule):
    """Is this parsed rule an allow (or rate-limited allow) of the dashboard
    port?"""
    return rule['action'] in ('ALLOW', 'LIMIT') and _dashboard_rule_match(rule['to'])


def _delete_would_cut_dashboard(st, rule):
    """Would removing `rule` leave the dashboard port unreachable? True when
    incoming defaults to deny/reject and no OTHER allow of the same address
    family still covers the port (a v6 allow does not keep v4 clients in)."""
    if st['defaults'].get('incoming') not in ('deny', 'reject'):
        return False
    if not _covers_dashboard(rule):
        return False
    return not any(r['number'] != rule['number'] and r['v6'] == rule['v6']
                   and _covers_dashboard(r) for r in st['rules'])


def _needs_confirm(warning):
    """success:false + needs_confirm — the UI shows the warning and re-submits
    with confirm:true. HTTP 200 so plain callers still surface the message."""
    return jsonify({'success': False, 'needs_confirm': True, 'error': warning})


def _dashboard_port_has_allow():
    """Is there ANY added allow rule covering the dashboard port? Checked via
    `ufw show added` (works while inactive too). Source-restricted allows
    count — on an internet-facing node the operator may have deliberately
    limited the port to one address, and adding our broad allow on top would
    silently reopen it to the world."""
    out, _, rc = run(['ufw', 'show', 'added'])
    if rc != 0:
        return False
    pat = re.compile(r'\ballow\b(?!.*\bdeny\b).*(\bport\s+%d\b|\b%d(/tcp)?\b)'
                     % (DASHBOARD_PORT, DASHBOARD_PORT))
    return any(pat.search(line) for line in out.splitlines())


def _ensure_dashboard_allowed():
    """The hard lockout guard: the port serving this UI stays reachable.
    Skips when an allow for the port already exists (however scoped) so a
    deliberately source-restricted rule is never widened to Anywhere."""
    if _dashboard_port_has_allow():
        return
    run(['ufw', 'allow', '%d/tcp' % DASHBOARD_PORT, 'comment', 'Nexus Dashboard'])


@bp.route('/api/firewall')
def firewall_get():
    st = _ufw_status()
    st['dashboard_port'] = DASHBOARD_PORT
    return jsonify(st)


@bp.route('/api/firewall/enable', methods=['POST'])
def firewall_enable():
    data = request.get_json() or {}
    if not shutil.which('ufw'):
        return err('ufw is not installed on this host')
    if data.get('allow_dashboard', True):
        _ensure_dashboard_allowed()
    if data.get('allow_ssh', True):
        run(['ufw', 'allow', '22/tcp', 'comment', 'SSH'])
    out, errout, rc = run(['ufw', '--force', 'enable'])
    if rc != 0:
        return err((errout or out).strip() or 'ufw enable failed')
    return jsonify({'success': True})


@bp.route('/api/firewall/disable', methods=['POST'])
def firewall_disable():
    out, errout, rc = run(['ufw', 'disable'])
    if rc != 0:
        return err((errout or out).strip() or 'ufw disable failed')
    return jsonify({'success': True})


@bp.route('/api/firewall/policy', methods=['POST'])
def firewall_policy():
    pol = (request.get_json() or {}).get('incoming', '')
    if pol not in ('allow', 'deny', 'reject'):
        return err('Policy must be allow, deny or reject')
    if pol != 'allow':
        _ensure_dashboard_allowed()
    out, errout, rc = run(['ufw', 'default', pol, 'incoming'])
    if rc != 0:
        return err((errout or out).strip() or 'ufw failed')
    return jsonify({'success': True})


def _validate_rule(action, port, proto, source, comment):
    """Shared validation + lockout guard for rule adds. Returns an error
    string or None."""
    if action not in FW_ACTIONS:
        return 'Action must be one of: %s' % ', '.join(FW_ACTIONS)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return 'Port must be a number from 1 to 65535'
    if proto not in ('any', 'tcp', 'udp'):
        return 'Protocol must be tcp, udp or any'
    if source and not RE_FW_SOURCE.match(source):
        return 'Source must be an IP address or CIDR subnet'
    if comment and not RE_FW_COMMENT.match(comment):
        return 'Comment: letters, numbers, spaces and _.:- only (max 64)'
    return None


def _blocks_dashboard(action, port, proto):
    """Would this rule block the TCP port serving this UI? Not refused —
    routes turn it into a needs_confirm warning."""
    return (action in ('deny', 'reject') and port == DASHBOARD_PORT
            and proto in ('any', 'tcp'))


def _keeps_dashboard_open(action, port, proto, source):
    """Is this rule an unconditional allow of the dashboard port? (A
    source-scoped allow still risks cutting off a client outside the range,
    so it does not count here.)"""
    return (action in ('allow', 'limit') and port == DASHBOARD_PORT
            and proto in ('any', 'tcp') and not source)


def _rule_args(action, port, proto, source, comment):
    args = ['ufw', action]
    if proto != 'any':
        args += ['proto', proto]
    args += ['from', source or 'any', 'to', 'any', 'port', str(port)]
    if comment:
        args += ['comment', comment]
    return args


@bp.route('/api/firewall/rule', methods=['POST'])
def firewall_rule_add():
    data = request.get_json() or {}
    action = data.get('action', 'allow')
    proto = data.get('proto', 'any')
    source = (data.get('source') or '').strip()
    comment = (data.get('comment') or '').strip()
    try:
        port = int(data.get('port'))
    except (TypeError, ValueError):
        return err('Port must be a number from 1 to 65535')
    bad = _validate_rule(action, port, proto, source, comment)
    if bad:
        return err(bad)
    if _blocks_dashboard(action, port, proto) and not data.get('confirm'):
        return _needs_confirm(
            'Port %d serves this dashboard — a %s rule on it can cut you off '
            '(this connection comes from %s). Add it anyway?'
            % (DASHBOARD_PORT, action, request.remote_addr))
    out, errout, rc = run(_rule_args(action, port, proto, source, comment))
    if rc != 0:
        return err((errout or out).strip() or 'ufw failed')
    return jsonify({'success': True, 'detail': out.strip()})


@bp.route('/api/firewall/rule/delete', methods=['POST'])
def firewall_rule_delete():
    """Delete by number, but only after re-reading the live table and checking
    the rule at that number is still the one the client saw — ufw renumbers on
    every change, so a blind numeric delete could remove the wrong rule."""
    data = request.get_json() or {}
    try:
        number = int(data.get('number'))
    except (TypeError, ValueError):
        return err('Rule number required')
    expect = data.get('expect') or {}
    st = _ufw_status()
    if not st['status_known']:
        return err('Could not read current rules')
    cur = next((r for r in st['rules'] if r['number'] == number), None)
    if cur is None:
        return err('Rule %d not found — refresh and retry' % number, 409)
    for k in ('to', 'action', 'from'):
        if k in expect and expect[k] != cur[k]:
            return err('Rules changed since the page loaded — refresh and retry', 409)
    if _delete_would_cut_dashboard(st, cur) and not data.get('confirm'):
        return _needs_confirm(
            'This is the last%s allow rule for port %d, which serves this '
            'dashboard — with incoming traffic denied by default, deleting it '
            'can lock you out of this page (this connection comes from %s). '
            'Delete it anyway?'
            % (' IPv6' if cur['v6'] else '', DASHBOARD_PORT, request.remote_addr))
    out, errout, rc = run(['ufw', '--force', 'delete', str(number)])
    if rc != 0:
        return err((errout or out).strip() or 'ufw delete failed')
    return jsonify({'success': True})


RE_TO_PORT = re.compile(r'^(\d+)(?:/(tcp|udp))?\Z')


def _readd_parsed(cur):
    """Best-effort rollback: re-add a rule from its parsed table row (used
    when an edit's re-add step fails after the delete already happened)."""
    m = RE_TO_PORT.match(cur['to'])
    if not m:
        return False
    src = '' if cur['from'].startswith('Anywhere') else cur['from']
    _, _, rc = run(_rule_args(cur['action'].lower(), int(m.group(1)),
                              m.group(2) or 'any', src, cur['comment']))
    return rc == 0


@bp.route('/api/firewall/rule/update', methods=['POST'])
def firewall_rule_update():
    """Edit a rule: verified numbered delete + re-add. ufw has no in-place
    edit, and `insert` positions don't map cleanly across the v4/v6 sections,
    so the edited rule moves to the end of the list."""
    data = request.get_json() or {}
    try:
        number = int(data.get('number'))
    except (TypeError, ValueError):
        return err('Rule number required')
    rule = data.get('rule') or {}
    action = rule.get('action', 'allow')
    proto = rule.get('proto', 'any')
    source = (rule.get('source') or '').strip()
    comment = (rule.get('comment') or '').strip()
    try:
        port = int(rule.get('port'))
    except (TypeError, ValueError):
        return err('Port must be a number from 1 to 65535')
    bad = _validate_rule(action, port, proto, source, comment)
    if bad:
        return err(bad)
    expect = data.get('expect') or {}
    st = _ufw_status()
    if not st['status_known']:
        return err('Could not read current rules')
    cur = next((r for r in st['rules'] if r['number'] == number), None)
    if cur is None:
        return err('Rule %d not found — refresh and retry' % number, 409)
    for k in ('to', 'action', 'from'):
        if k in expect and expect[k] != cur[k]:
            return err('Rules changed since the page loaded — refresh and retry', 409)
    if not data.get('confirm'):
        if _blocks_dashboard(action, port, proto):
            return _needs_confirm(
                'Port %d serves this dashboard — a %s rule on it can cut you '
                'off (this connection comes from %s). Apply it anyway?'
                % (DASHBOARD_PORT, action, request.remote_addr))
        if (_delete_would_cut_dashboard(st, cur)
                and not _keeps_dashboard_open(action, port, proto, source)):
            return _needs_confirm(
                'This is the last%s allow rule for port %d, which serves this '
                'dashboard. If the new rule does not match your connection '
                '(it comes from %s), you can lock yourself out of this page. '
                'Apply it anyway?'
                % (' IPv6' if cur['v6'] else '', DASHBOARD_PORT,
                   request.remote_addr))
    out, errout, rc = run(['ufw', '--force', 'delete', str(number)])
    if rc != 0:
        return err((errout or out).strip() or 'ufw delete failed')
    out, errout, rc = run(_rule_args(action, port, proto, source, comment))
    if rc != 0:
        restored = _readd_parsed(cur)
        return err('Applying the new rule failed: %s — the old rule was %s'
                   % ((errout or out).strip() or 'ufw failed',
                      're-added' if restored else 'REMOVED and could not be '
                      're-added; check the rules list'))
    return jsonify({'success': True, 'detail': out.strip()})


UFW_CONF = '/etc/ufw/ufw.conf'


def _ufw_boot_enabled():
    """ENABLED=yes in ufw.conf — the firewall is supposed to be on. The file
    is world-readable (0644 stock), so no sudo needed."""
    try:
        with open(UFW_CONF) as f:
            return any(line.strip() == 'ENABLED=yes' for line in f)
    except OSError:
        return False


def _firewall_alerts():
    """Alert ONLY on the mismatch (configured on at boot, currently off).
    A merely-inactive ufw is the norm on a trusted LAN — alerting on that
    would light up every fleet node the day this module ships."""
    st = _ufw_status()
    if (st['available'] and st['status_known'] and not st['active']
            and _ufw_boot_enabled()):
        return [{'key': 'firewall_inactive',
                 'message': 'Firewall (ufw) is enabled at boot but not active'}]
    return []


# ─── Module descriptor (consumed by core.registry at create_app) ───────
MODULE = {'id': 'firewall', 'label': 'Firewall', 'category': 'System',
          'blueprint': bp, 'alerts': _firewall_alerts}
