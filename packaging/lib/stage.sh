#!/usr/bin/env bash
# stage.sh — assemble the package payload tree under a DESTDIR.
#
# The systemd units, /usr/local/sbin helpers, and the sudoers policy are
# EXTRACTED VERBATIM from the repo's install.sh (its single-quoted heredocs use
# literal fresh-install paths), so install.sh stays the one source of truth and
# the packages can never silently drift from it. The application runtime tree is
# copied from $SRC (a SANITIZED export — see packaging/sanitize.sh). The Python
# venv is NOT built here; the per-distro build image creates it in-container so
# it matches the target's Python (see packaging/build.sh).
#
# Usage: stage.sh <SRC_TREE> <DESTDIR> <INSTALL_SH>
set -euo pipefail

SRC="${1:?src tree}"; DEST="${2:?destdir}"; INSTALL_SH="${3:?install.sh path}"
APPDIR=opt/nexus-dashboard

command -v install >/dev/null || { echo "coreutils 'install' required" >&2; exit 1; }

# --- extract one heredoc body from install.sh -------------------------------
# extract_block "<lhs as it appears after 'cat > '>" MARKER out_rel mode
extract_block() {
    local lhs="$1" marker="$2" out="$3" mode="$4" start
    start=$(grep -nF "cat > $lhs << '$marker'" "$INSTALL_SH" | head -1 | cut -d: -f1)
    [ -n "$start" ] || { echo "stage: heredoc not found: cat > $lhs << '$marker'" >&2; exit 1; }
    mkdir -p "$DEST/$(dirname "$out")"
    awk -v s="$start" -v m="$marker" 'NR>s { if ($0==m) exit; print }' "$INSTALL_SH" > "$DEST/$out"
    [ -s "$DEST/$out" ] || { echo "stage: extracted empty body for $out" >&2; exit 1; }
    chmod "$mode" "$DEST/$out"
}

echo "stage: sudoers policy"
extract_block '$SUDOERS_FILE' SUDOERS etc/sudoers.d/nexus-dashboard 0440

echo "stage: /usr/local/sbin helpers"
# install.sh var name -> installed helper basename
declare -A HELPERS=(
    ['$LOCATE_HELPER']=nexus-dashboard-locate-read
    ['$SESSIONS_HELPER']=nexus-dashboard-iscsi-sessions
    ['$SNAPFS_HELPER']=nexus-dashboard-snap-fs
    ['$NETPLAN_HELPER']=nexus-dashboard-netplan
    ['$CADDY_HELPER']=nexus-dashboard-caddy
    ['$MOUNT_HELPER']=nexus-dashboard-mount
    ['$MODEL_FETCH_HELPER']=nexus-dashboard-model-fetch
    ['$DLNA_RESCAN_HELPER']=nexus-dashboard-dlna-rescan
    ['$DLNA_STATS_HELPER']=nexus-dashboard-dlna-stats
)
for var in "${!HELPERS[@]}"; do
    extract_block "\"$var\"" HELPER "usr/local/sbin/${HELPERS[$var]}" 0755
done

echo "stage: systemd units"
# Packaged units live in /usr/lib/systemd/system (vendor dir), not the admin
# /etc dir install.sh writes to. Marker is SERVICE for services, TIMER for timers.
for u in nexus-dashboard.service \
         nexus-dashboard-autosnap.service nexus-dashboard-autosnap.timer \
         nexus-dashboard-replicate.service nexus-dashboard-replicate.timer \
         nexus-dashboard-alerts.service    nexus-dashboard-alerts.timer \
         nexus-dashboard-maintenance.service nexus-dashboard-maintenance.timer \
         nexus-dashboard-history.service   nexus-dashboard-history.timer; do
    case "$u" in *.timer) m=TIMER ;; *) m=SERVICE ;; esac
    extract_block "/etc/systemd/system/$u" "$m" "usr/lib/systemd/system/$u" 0644
done

echo "stage: application runtime tree -> /$APPDIR"
mkdir -p "$DEST/$APPDIR"
cp -a "$SRC/app.py" "$SRC/nexusdash" "$SRC/templates" "$SRC/static" "$DEST/$APPDIR/"
cp -a "$SRC/requirements.txt" "$DEST/$APPDIR/"
# State/log dirs the app expects (ownership is fixed up in postinstall).
mkdir -p "$DEST/$APPDIR/dnsmasq/render/dnsmasq.d" \
         "$DEST/$APPDIR/dnsmasq/render/hosts.d" \
         "$DEST/$APPDIR/dnsmasq/state" \
         "$DEST/$APPDIR/dnsmasq/leases" \
         "$DEST/var/log/nexus-dashboard"

echo "stage: done -> $DEST"
