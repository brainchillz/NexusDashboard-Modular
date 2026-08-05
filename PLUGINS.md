# Nexus Dashboard plugins

Since 3.0.0 the dashboard loads operator plugins from a directory — drop a
folder in, restart the service once, and the plugin registers exactly like a
built-in module: it appears on the **Modules** page (disabled until you
enable it), its endpoints ride the same login/RBAC/audit/disable machinery,
and its pages join the sidebar.

There are two tiers. Most needs fit the **declarative tier** (one YAML file,
no code); the **Python tier** has full module powers. A plugin directory is
one tier or the other — both files present is a load error.

```
<app dir>/plugins/            # e.g. /opt/nexus-dashboard/plugins/
  my-plugin/
    plugin.yaml   OR  plugin.py
    static/                   # optional: js/css (python tier), iframe content
    README.md                 # recommended: what it does, any sudoers lines
```

Install = copy the directory + restart the dashboard service. Uninstall =
remove it + restart. There is deliberately **no upload/edit API**: plugin
files are installed by root over SSH, never through the web UI. Directory
name = plugin id: `^[a-z][a-z0-9-]{1,31}$`, and it may not collide with a
built-in module id. Broken plugins never prevent boot — they show on the
Modules page as *load failed* with detail in `GET /api/plugins` (admin).

The worked examples in `examples/plugins/` are the fastest way in:
`hello-world` (minimal declarative) and `wireguard` (service-backed, sudo,
degradation).

---

## The declarative tier (plugin.yaml)

```yaml
schema: 1                 # must be 1
id: my-plugin             # must equal the directory name
label: My Plugin          # Modules page + nav label (max 40 chars)
category: Tools           # sidebar group; new names create a new group
category_order: 90        # optional; 90 = between DNS and System
min_app_version: "3.0.0"  # required; plugin is refused on older dashboards
version: "1.0"            # optional, shown to the operator

service:                  # optional — one systemd unit this plugin fronts:
  unit: my-daemon         #   appears in the Service Manager and /api/status,
  name: My Daemon         #   never raises health alerts (alert=False)
  binary: /usr/bin/mydaemon   # optional: used for the "installed" check
dashboard_card: true      # optional; needs service: — up/down card on the
                          # dashboard front page

pages:                    # 1..8 pages; each becomes a sidebar entry
  - id: my-plugin         # page id == plugin id keeps the bare id;
    label: My Plugin      # other pages get ids like "my-plugin-extras"
    icon: pkg             # optional: a stock sprite icon name
    widgets: [...]        # 1..12 widgets, rendered in order
```

### Widgets

Common optional keys on every widget: `title`, `admin_only` (hidden from
read-only users AND enforced server-side), `refresh_seconds` (re-poll while
the page is open; minimum 5).

**`markdown`** — static text. `content` supports paragraphs, `**bold**`,
`` `code` ``. Always HTML-escaped; there is no raw-HTML widget, by design.

**`service_status`** — status card for `unit` (defaults to
`service.unit`) with Start/Stop/Restart for admins. Uses the dashboard's
existing service machinery — no sudoers needed.

**`command_table`** — the workhorse: runs the declared `command` (argv
list) server-side and renders stdout as a table.

```yaml
- type: command_table
  command: [wg, show, wg0, dump]   # 1..32 argv elements, run exactly as
  sudo: true                       # written; default false; the ONLY sudo
  timeout: 10                      # seconds, 1..60 (default 15)
  parse:
    mode: tsv                      # whitespace (default) | tsv | lines | json_lines
    skip_lines: 1                  # default 0
    max_rows: 200                  # default 200, max 500
  columns:                         # 1..12
    - {title: Endpoint, index: 2}                       # index for split modes
    - {title: Last seen, index: 4, transform: epoch_ago}
    - {title: Received, index: 5, transform: human_bytes}
    # json_lines mode selects by key instead: {title: Name, key: name}
```

Parse-mode honesty: `whitespace` shears on embedded spaces (fine for
`df -P`); `tsv` is for tab-separated-by-contract tools; `lines` is the raw
one-column escape hatch; `json_lines` parses one JSON object per line.
There is no regex mode. Transforms are the closed set `none | epoch_ago |
human_bytes`.

**`action_button`** — POST-runs the declared `command`. Always admin-only
(the dashboard's central RBAC). `confirm: true` (default) asks first;
`danger: true` styles it red; output/errors open in a modal.

**`log_tail`** — last `lines` (1..1000, default 100) of `journalctl -u
<unit>`. Rides the dashboard's existing journalctl grant.

**`link` / `iframe`** — an outbound link, or an embedded page
(`src`, `height` 100..2000). `http(s)` URLs only.

### Security model (read this)

- **Only argv literally written in the manifest ever executes.** No user
  input reaches a command line — widgets are addressed by index, and there
  are no declared parameters in schema v1. (A v2 design — closed literal
  enums substituted as whole argv elements — exists but is deliberately
  unbuilt until real demand.) Need N variants? Declare N widgets.
- `command[0]` may never be a shell (`sh`, `bash`, …) or a privilege tool
  (`sudo`, `su`, `doas`, `env`, `nsenter`); `sudo: true` is the only sudo
  path.
- **Sudoers are yours to grant, by hand.** The dashboard never installs
  plugin sudoers. Fixed argv means you can pin the exact command:

  ```
  dashboard ALL=(ALL) NOPASSWD: /usr/bin/wg show wg0 dump
  ```

  Exact pins contain no wildcards, so they parse identically on classic
  sudo and sudo-rs (Ubuntu 26.04+). If you write a helper script taking
  variable arguments (Python tier), use the two-line pattern — sudo-rs
  rejects wildcards embedded inside an argument word:

  ```
  dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/myplugin-helper
  dashboard ALL=(ALL) NOPASSWD: /usr/local/sbin/myplugin-helper *
  ```

  Always `visudo -cf` the file before installing it. Without a grant, a
  `sudo: true` widget degrades to a clean error card — nothing breaks.
- Keep the plugin directory root-owned (`root:root`, files 0644). The
  loader refuses world-writable plugin files outright.
- Command stdout is capped (256 KB) and timeouts are hard-capped (60 s) so
  a wedged command can't pile up server threads.
- Never render secrets: know your tool's output (see the wireguard
  example's handling of `wg show ... dump` key fields).

---

## The Python tier (plugin.py)

A Python plugin is **arbitrary code running in-process as the dashboard's
service user** — installing one is an act of full trust, equivalent to
editing the app. There is no sandbox. The loader adds guard rails only:
plugins always start disabled, world-writable files are refused, a crash at
import becomes a *load failed* stub instead of a boot failure.

`plugin.py` exposes a `MODULE` descriptor, same contract as built-ins:

```python
from flask import Blueprint, jsonify

bp = Blueprint('my-plugin', __name__)   # bp name MUST equal the plugin id

@bp.route('/api/my-plugin/things')
def things():
    return jsonify({'things': []})

MODULE = {
    'id': 'my-plugin',                  # must equal the directory name
    'label': 'My Plugin',
    'category': 'Tools',
    'blueprint': bp,
    'version': '1.0',
    # optional contributions (all merged by the registry):
    'nav': {'cat': 'tools', 'cat_order': 90, 'pages': [
        {'id': 'my-plugin', 'label': 'My Plugin', 'icon': 'pkg'}]},
    'assets': {'js': ['plugin.js']},    # served from static/, cache-busted
    'services': {'my-plugin': {'name': 'My Daemon', 'service': 'my-daemon',
                               'pkg': None, 'binary': '/usr/bin/mydaemon',
                               'alert': False}},
    'summary': lambda: {...},           # block under your id in /api/summary
    'alerts': lambda: [...],            # [{key, message}]
    'history': lambda: [...],           # [(metric, label, value)] per tick
    'history_metrics': {'my_metric'},   # allowlist names for the above
    'metrics': lambda: [...],           # Prometheus exposition lines
    'tasks': [...],                     # systemd timer records
    'cli': {'my-tick': fn},             # python app.py my-tick
    'websockets': lambda sock: ...,     # register ws routes; RE-CHECK your
                                        # module toggle per connection
}
```

Frontend: ship `static/plugin.js` declaring globals named by convention —
`page_<pageid>()` renders your page into `#page-content`;
`dashcard_<id>(ctx)` returns a dashboard card. Prefix everything else with
your plugin id to avoid clobbering globals. Escape all dynamic text with
`escapeHtml(...)`; reuse the existing classes (`table`, `card`, `btn`,
`status-badge`) and CSS variables so your page follows the theme.

### Blessed SDK surface

Stable within the 3.x series (everything else in `nexusdash.*` is internal
and may change in any release):

| Import | What |
|---|---|
| `nexusdash.core.runcmd.run / run_safe / err` | argv-list command execution (never a shell), JSON error helper |
| `nexusdash.core.config.APP_DIR / APP_VERSION / write_json_atomic` | app paths, version, atomic JSON state writes |
| `nexusdash.core.validators.RE_*` | the shared `\Z`-anchored input validators — validate every route argument |
| `nexusdash.core.services.SYSTEM_SERVICES / resolve_service` | the merged service table |
| `nexusdash.core.registry.load_disabled_modules / module_hooks` | toggle state (websocket handlers re-check per connection) |
| `nexusdash.core.auth._is_admin` | admin check for GET routes (mutations are admin-only centrally) |

Conventions your code is expected to follow: argument-list `run()` (no
shell, `sudo -n`), `\Z`-anchored allowlist validation on every input,
`write_json_atomic` for state. Keep plugin state inside your own directory
(`plugins/<id>/state.json`) — it is untracked and survives upgrades.

Testing: plugins are not merged into the `app.*` test facade; patch via
`sys.modules['nexusdash_plugins.<id>']`.

## Versioning

`schema: 1` manifests keep working across 3.x; minor releases may add new
widget types and new *optional* keys but never change existing meanings.
`min_app_version` is compared against the dashboard version at load and the
plugin is refused (visibly, on the Modules page) when too old.
