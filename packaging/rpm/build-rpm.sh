#!/usr/bin/env bash
# build-rpm.sh — build the nexus-dashboard .rpm. Runs INSIDE the Rocky/RHEL 9
# container (so the bundled venv matches its Python 3.9). Invoked by
# packaging/build.sh via `docker run`.
#
# Env in: VERSION RELEASE DISTTAG SOURCE_DATE_EPOCH
# Mounts:  /repo (ro)  /src (ro, sanitized tree)  /out (rw)
set -euo pipefail

: "${VERSION:?}" "${RELEASE:?}" "${DISTTAG:?}" "${SOURCE_DATE_EPOCH:?}"
REPO=/repo SRC=/src OUT=/out
export SOURCE_DATE_EPOCH

echo ":: dnf build deps"
dnf -y -q install rpm-build python3 python3-pip >/dev/null

STAGE=$(mktemp -d)/root
echo ":: stage payload tree"
bash "$REPO/packaging/lib/stage.sh" "$SRC" "$STAGE" "$REPO/install.sh"

echo ":: build bundled venv (Python 3.9) at the canonical path"
python3 -m venv "$STAGE/opt/nexus-dashboard/venv"
"$STAGE/opt/nexus-dashboard/venv/bin/pip" install --no-cache-dir -q --upgrade pip wheel >/dev/null
if [ -f "$REPO/packaging/requirements.lock" ]; then
    "$STAGE/opt/nexus-dashboard/venv/bin/pip" install --no-cache-dir --no-compile -q -r "$REPO/packaging/requirements.lock"
else
    "$STAGE/opt/nexus-dashboard/venv/bin/pip" install --no-cache-dir --no-compile -q -r "$STAGE/opt/nexus-dashboard/requirements.txt"
fi
find "$STAGE/opt/nexus-dashboard/venv" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

echo ":: generate spec (embeds the shared maintainer bodies)"
TOP=$(mktemp -d); mkdir -p "$TOP"/{SPECS,RPMS}
SPEC="$TOP/SPECS/nexus-dashboard.spec"
{
cat <<SPEC
Name:           nexus-dashboard
Version:        ${VERSION}
Release:        ${RELEASE}.${DISTTAG}
Summary:        Modular storage/sharing/containers/AI dashboard
License:        Proprietary
URL:            https://example.lan
BuildArch:      x86_64
Requires:       python3, sudo, systemd, shadow-utils
# Optional module backends (degrade gracefully when absent):
Recommends:     firewalld
AutoReqProv:    no

%description
A single Flask web app for managing a storage / sharing / container / AI node:
ZFS, disks, LVM, MD RAID, NFS/SMB/iSCSI shares, LXD/Incus and Docker, a host
firewall, a Caddy reverse proxy and optional DNS/DHCP - each a toggleable
module. Ships a self-contained Python virtualenv and systemd units; serves
HTTPS on port 8443 (self-signed by default).

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}
cp -a ${STAGE}/. %{buildroot}/

%post
SPEC
cat "$REPO/packaging/lib/postinstall.sh"
cat <<'SPEC'

%preun
NDX_MODE=upgrade; [ "$1" = 0 ] && NDX_MODE=remove
SPEC
cat "$REPO/packaging/lib/preremove.sh"
cat <<'SPEC'

%postun
SPEC
cat "$REPO/packaging/lib/postremove.sh"
cat <<'SPEC'

%files
/opt/nexus-dashboard
/usr/local/sbin/nexus-dashboard-*
/usr/lib/systemd/system/nexus-dashboard*.service
/usr/lib/systemd/system/nexus-dashboard*.timer
%dir /var/log/nexus-dashboard
%config(noreplace) /etc/sudoers.d/nexus-dashboard
SPEC
} > "$SPEC"

echo ":: rpmbuild"
rpmbuild -bb \
    --define "_topdir $TOP" \
    --define "_buildhost reproducible" \
    --define "clamp_mtime_to_source_date_epoch 1" \
    --define "use_source_date_epoch_as_buildtime 1" \
    --define "source_date_epoch_changelog 1" \
    "$SPEC"

RPM=$(find "$TOP/RPMS" -name '*.rpm' | head -1)
cp "$RPM" "$OUT/"
echo ":: built $(basename "$RPM")"
rpm -qip "$OUT/$(basename "$RPM")" 2>/dev/null | sed -n '1,10p'
