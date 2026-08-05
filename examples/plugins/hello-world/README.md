# hello-world — reference declarative plugin

The smallest useful example of the dashboard's declarative plugin tier: one
`plugin.yaml`, no code. It adds an **Examples › Hello World** page with a
text card, a live `df -P` table, and a link.

## Install

```sh
sudo cp -r hello-world /opt/nexus-dashboard/plugins/
sudo systemctl restart nexus-dashboard      # or your unit name
```

Then enable **Hello World** on the dashboard's Modules page (plugins always
start disabled). No sudoers changes are needed — `df` runs unprivileged
(`sudo: false`).

## What to learn from it

- `command_table` runs the *declared argv only*, server-side; the frontend
  never sends commands.
- `parse: {mode: whitespace}` is the simplest parser and shears columns on
  embedded spaces — fine for `df -P`, wrong for e.g. `ls -l` names. Prefer
  `tsv` or `json_lines` when the tool offers machine-readable output.
- `refresh_seconds` re-polls the widget while the page is open.

See `PLUGINS.md` in the repository root for the full schema, and the
`wireguard` example for a service-backed plugin with sudo and degradation
behavior.
