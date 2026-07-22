# Packaging

Reproducible `.deb` and `.rpm` packages for Nexus Dashboard, built from this
(already-public, already-sanitized) source tree.

| Target        | Base image      | Python | Artifact                                             |
|---------------|-----------------|--------|------------------------------------------------------|
| `ubuntu2404`  | `ubuntu:24.04`  | 3.12   | `nexus-dashboard_<v>-1~ubuntu24.04_amd64.deb`        |
| `ubuntu2604`  | `ubuntu:26.04`  | 3.14   | `nexus-dashboard_<v>-1~ubuntu26.04_amd64.deb`        |
| `rocky9`      | `rockylinux:9`  | 3.9    | `nexus-dashboard-<v>-1.el9.x86_64.rpm`               |

Each package bundles a self-contained Python virtualenv at
`/opt/nexus-dashboard/venv` built **in a matching container** so it fits that
release's interpreter, plus the systemd units, `/usr/local/sbin` helpers and the
sudoers policy. It installs and activates exactly what `install.sh` does — the
units/helpers/sudoers are **extracted verbatim from `install.sh`** at build time
(single source of truth, no fork), and the maintainer scripts run the same
activation steps (service user, group joins, default-off `modules.json`, history
timer, dashboard on :8443).

## Build

```bash
packaging/build.sh                  # all three targets -> dist/
packaging/build.sh -t ubuntu2404    # one target
packaging/build.sh -r v2.2.0        # pin a source tag/ref
```

Requires `docker`. Builds run in throwaway containers; nothing is installed on
the host. Version defaults to `APP_VERSION` in `nexusdash/core/config.py`.

**Source of truth.** By default `build.sh` clones the source from the public
repository and builds from that, so every artifact is provably built from public
code — no private identifier can enter a package. A defense-in-depth gate
re-checks the tree before any container runs. (`--source local` builds from the
current checkout instead; intended only for local development.)

## Reproducibility

Deterministic **inputs**: pinned dependency lock (`requirements.lock`, resolved
on the oldest target so every pin runs on all three Pythons), the source
commit's `SOURCE_DATE_EPOCH`, normalized mtimes, `dpkg-deb --root-owner-group`
and rpm's `clamp_mtime_to_source_date_epoch`. Given the same `ref` + lock + base
image, the package **contents are functionally identical**. Note: a pip-built
venv is not guaranteed byte-for-byte identical across rebuilds (RECORD/metadata
ordering, pip internals) — that is a known limitation of bundling a venv, not a
build bug. For byte-stable artifacts, pin base images by digest and archive the
`dist/` output of the release build.

## Layout

```
packaging/
  build.sh              orchestrator: clone sanitized source -> per-target container build -> dist/
  requirements.lock     pinned deps for reproducible venvs (all three Pythons)
  lib/
    stage.sh            extract units/helpers/sudoers from install.sh + lay down the tree
    postinstall.sh      shared post-install body (deb postinst / rpm %post)
    preremove.sh        shared pre-remove body  (deb prerm   / rpm %preun)
    postremove.sh       shared post-remove body (deb postrm  / rpm %postun)
  deb/build-deb.sh      in-container .deb builder
  rpm/build-rpm.sh      in-container .rpm builder (generates the spec)
```

Tagging, checksums, and publishing releases to the forges — plus refreshing the
public source itself — are maintainer steps covered in the install docs.
