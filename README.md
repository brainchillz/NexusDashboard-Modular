# Nexus Dashboard
<img width="1472" height="1118" alt="Screenshot 2026-07-04 at 10 12 21 PM" src="https://github.com/user-attachments/assets/05c107b5-c003-4fa9-ab37-d5a5d372616c" />

A single modular web dashboard for a whole home-lab fleet: **storage** (ZFS,
LVM, MD RAID, disks), **sharing** (iSCSI, NFS, SMB, DLNA), **AI tools**
(llama.cpp, GPU), **containers & VMs** (LXD/Incus), and **system management**
(network/netplan, host firewall (ufw), services, logs, scheduled tasks,
alerting, metrics, history) — one app, one login, one audit trail per node.

This is the merger of the single-file *Storage/Nexus Dashboard* and the
*Nexus Containers* (LXD) console into one package-structured Flask app.
Per-node **module toggles** decide what each server exposes: a storage node
shows storage+sharing, an AI node shows llama/GPU, an LXD host adds the
Containers pages — same codebase everywhere. Disabled modules are **hard
disabled**: their API routes refuse immediately and disappear entirely at the
next restart.

## Highlights

- **Simple auth** — username/password sessions + API bearer tokens with
  admin/read-only RBAC and per-IP lockout. No client certificates anywhere
  (LXD/Incus is reached over its local Unix socket, which needs only group
  membership).
- **Defensive by construction** — every system command is an argument list
  (`shell=False`) behind pinned sudoers or root-owned wrappers; every
  user-supplied name is allowlist-validated; all config writes are atomic;
  every mutation is audit-logged from one choke point.
- **Fleet-aware** — `/api/version` + `/api/me` capabilities feed the
  NexusController for enroll/skew-detection/auto-classification; Prometheus
  `/metrics`; bounded on-disk history with forecasts.
- **No build step** — vanilla-JS SPA split per category, xterm.js console for
  containers and VM serial, installable PWA, dark (burnt-orange-on-grey) and
  light themes.
- **Firewall without foot-guns** — the Firewall page drives ufw for simple
  inbound allow/deny, but can never block the port serving the dashboard
  itself: it is auto-allowed when enabling or defaulting to deny (without ever
  widening an existing source-restricted rule), deny rules against it are
  refused, and rule deletes are re-verified against the live table first.

## Install

```bash
git clone https://github.com/brainchillz/NexusDashboard-Modular.git
cd NexusDashboard-Modular
sudo ./install-prerequisites.sh        # Debian/Ubuntu packages (single source of truth)
sudo ./install.sh                      # user, venv, sudoers, wrappers, timers, service
# RHEL/Rocky: use install-prerequisites-rhel.sh + install-rhel.sh
```

Serves **HTTPS on 8443** (self-signed by default). First-run admin password is
printed to the service log:

```bash
journalctl -u nexus-dashboard | grep -A2 'initial admin account'
# or set one:
sudo -u dashboard /opt/nexus-dashboard/venv/bin/python /opt/nexus-dashboard/app.py set-password admin
```

Fresh installs are named **nexus-dashboard** throughout (`/opt/nexus-dashboard`,
`nexus-dashboard.service` + timers, `/usr/local/sbin/nexus-dashboard-*` helpers).
Nodes upgraded in place from the pre-merge apps keep their original names
(`storage-dashboard` / `llama-dashboard`); the app follows the
`DASHBOARD_UNIT_PREFIX` env var its unit file sets (default `storage-dashboard`).

If a host already runs LXD, Incus or Docker, the installer adds the service
user to the socket group and the Containers/Docker pages light up; otherwise
they simply report the daemon unreachable (or disable the modules on the
Modules page). The Docker pages manage containers, images, volumes and
networks straight over `/var/run/docker.sock` — create (with auto-pull),
lifecycle, logs, live stats, an in-browser shell (bash/sh via exec) — plus
compose stacks: projects already on the host are discovered from their
compose labels and get stack-level up/down/restart/pull/logs, while stacks
created in the UI are stored under the app and validated with
`docker compose config` before they are ever kept.

## Architecture (short version)

```
app.py                  # entrypoint + compatibility facade (import app …)
nexusdash/
  core/                 # auth/RBAC/tokens, audit, TLS, registry, aggregators
  modules/              # disks zfs lvm mdraid schedules replication maintenance
                        # iscsi nfs smb minidlna llama gpu firewall docker network logs
  modules/containers/   # LXD/Incus: instances, images, networks, port-forward, console
static/js/*.js          # per-category frontend, no build step
```

Each module registers a descriptor (blueprint + nav entry + optional
summary/alerts/metrics/history/CLI hooks); the registry derives navigation,
capabilities and the hard-disable enforcement from those.

## Tests

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest tests/ -q     # 279 tests, no root/hardware needed
```

## Lineage

Replaces two earlier internal projects, now frozen: a single-file
storage/sharing/AI dashboard (its API kept byte-identical here) and a
standalone LXD web console.
