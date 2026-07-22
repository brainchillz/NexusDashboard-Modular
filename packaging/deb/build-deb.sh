#!/usr/bin/env bash
# build-deb.sh — build the nexus-dashboard .deb. Runs INSIDE the target Ubuntu
# container (so the bundled venv matches that release's Python). Invoked by
# packaging/build.sh via `docker run`.
#
# Env in: VERSION RELEASE DISTTAG SOURCE_DATE_EPOCH
# Mounts:  /repo (ro, the source tree)   /src (ro, sanitized runtime tree)
#          /out (rw, artifact dir)
set -euo pipefail

: "${VERSION:?}" "${RELEASE:?}" "${DISTTAG:?}" "${SOURCE_DATE_EPOCH:?}"
REPO=/repo SRC=/src OUT=/out
export DEBIAN_FRONTEND=noninteractive

echo ":: apt build deps"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3-venv python3-pip dpkg-dev ca-certificates >/dev/null

PYX=$(python3 -c 'import sys;print("python3.%d"%sys.version_info[1])')   # e.g. python3.12
echo ":: target interpreter: $PYX"

WORK=$(mktemp -d); DEST=$WORK/pkg
echo ":: stage payload tree"
bash "$REPO/packaging/lib/stage.sh" "$SRC" "$DEST" "$REPO/install.sh"

echo ":: build bundled venv at the canonical path"
python3 -m venv "$DEST/opt/nexus-dashboard/venv"
"$DEST/opt/nexus-dashboard/venv/bin/pip" install --no-cache-dir -q --upgrade pip wheel
if [ -f "$REPO/packaging/requirements.lock" ]; then
    "$DEST/opt/nexus-dashboard/venv/bin/pip" install --no-cache-dir --no-compile -q \
        -r "$REPO/packaging/requirements.lock"
else
    "$DEST/opt/nexus-dashboard/venv/bin/pip" install --no-cache-dir --no-compile -q \
        -r "$DEST/opt/nexus-dashboard/requirements.txt"
fi
# Drop pip's build cruft for a smaller, more deterministic tree.
find "$DEST/opt/nexus-dashboard/venv" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

echo ":: DEBIAN control + maintainer scripts"
mkdir -p "$DEST/DEBIAN"
INSTALLED_KB=$(du -sk "$DEST/opt" "$DEST/usr" | awk '{s+=$1} END{print s}')
cat > "$DEST/DEBIAN/control" <<EOF
Package: nexus-dashboard
Version: ${VERSION}-${RELEASE}~${DISTTAG}
Architecture: amd64
Maintainer: Nexus Dashboard <packages@example.lan>
Installed-Size: ${INSTALLED_KB}
Depends: ${PYX}, sudo, systemd, coreutils, passwd
Recommends: ufw
Suggests: zfsutils-linux, samba, nfs-kernel-server, targetcli-fb, minidlna, caddy, dnsmasq, smartmontools, lvm2, mdadm
Section: admin
Priority: optional
Homepage: https://example.lan
Description: Nexus Dashboard — modular storage/sharing/containers/AI dashboard
 A single Flask web app for managing a storage / sharing / container / AI node:
 ZFS, disks, LVM, MD RAID, NFS/SMB/iSCSI shares, LXD/Incus and Docker, a host
 firewall, a Caddy reverse proxy and optional DNS/DHCP — each a toggleable
 module. Ships a self-contained Python virtualenv and systemd units; serves
 HTTPS on port 8443 (self-signed by default).
EOF

# The sudoers file is a genuine conffile — an admin who hand-edits it must not
# have it silently clobbered on upgrade.
echo "/etc/sudoers.d/nexus-dashboard" > "$DEST/DEBIAN/conffiles"

emit() { { echo '#!/bin/sh'; echo 'set -e'; cat; } ; }
emit < "$REPO/packaging/lib/postinstall.sh"  > "$DEST/DEBIAN/postinst"
{ echo '#!/bin/sh'; echo 'set -e'
  echo 'NDX_MODE=remove; [ "$1" = upgrade ] && NDX_MODE=upgrade'
  cat "$REPO/packaging/lib/preremove.sh"; } > "$DEST/DEBIAN/prerm"
emit < "$REPO/packaging/lib/postremove.sh"   > "$DEST/DEBIAN/postrm"
chmod 0755 "$DEST/DEBIAN/postinst" "$DEST/DEBIAN/prerm" "$DEST/DEBIAN/postrm"

echo ":: reproducible dpkg-deb build"
# Deterministic mtimes across the whole tree, then a reproducible archive.
find "$DEST" -print0 | xargs -0 touch --no-dereference --date="@${SOURCE_DATE_EPOCH}"
OUTFILE="$OUT/nexus-dashboard_${VERSION}-${RELEASE}~${DISTTAG}_amd64.deb"
dpkg-deb --root-owner-group --uniform-compression -Zxz --build "$DEST" "$OUTFILE"
echo ":: built $OUTFILE"
dpkg-deb --info "$OUTFILE" | sed -n '1,12p'
