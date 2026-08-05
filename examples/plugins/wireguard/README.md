# wireguard — reference declarative plugin (service-backed)

A WireGuard status page for the dashboard: tunnel service card with
start/stop/restart, live peers table from `wg show wg0 dump`, a restart
button, and the unit's journal — from one `plugin.yaml`.

## Install

```sh
sudo cp -r wireguard /opt/nexus-dashboard/plugins/
sudo systemctl restart nexus-dashboard      # or your unit name
```

Enable **WireGuard** on the Modules page (plugins always start disabled).

## The one sudoers line

The peers table declares `sudo: true`, and the dashboard's service user has
no grant for `wg` until you add one. Because declarative plugins run only
the argv written in the manifest, you can pin the grant to the EXACT
command — no wildcards, which also means it parses identically on classic
sudo and sudo-rs:

```
# /etc/sudoers.d/nexus-dashboard-wireguard  (mode 0440)
dashboard ALL=(ALL) NOPASSWD: /usr/bin/wg show wg0 dump
```

Always validate before installing: `visudo -cf <file>`. The service card,
restart button, and journal tail need nothing — they ride the dashboard's
existing blanket `systemctl`/`journalctl` grants.

## Degradation behavior (all intentional)

- **No sudoers grant yet** — the Peers card shows sudo's "a password is
  required" error cleanly; everything else works.
- **wg not installed** — the Peers card shows "Command not found"; the
  service card shows the unit as missing/inactive.
- **Tunnel down** — service card red, peers table empty, journal explains.

## Secrets

`wg show <if> dump` prints the interface **private key** on line 1 and each
peer's **preshared key** in field 1. The manifest skips the interface line
(`skip_lines: 1`) and never maps a column to field 1. Keep it that way when
adapting this file.

## Adapting to another interface

Replace `wg0` in the four places it appears (service unit ×2 via the unit
name, the `wg show` argv, and your sudoers line).
