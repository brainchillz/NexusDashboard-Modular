# --- nexus-dashboard shared pre-remove body (POSIX sh) ------------------------
# Embedded into the deb prerm and the rpm %preun. The caller sets $NDX_MODE to
# "remove" (final removal) or "upgrade". On upgrade we leave the running service
# alone — the new package's post-install restarts it. On removal we stop and
# disable the dashboard and every timer.
if [ "${NDX_MODE:-remove}" = remove ] && command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now nexus-dashboard.service >/dev/null 2>&1 || true
    for _t in history autosnap replicate alerts maintenance; do
        systemctl disable --now "nexus-dashboard-${_t}.timer" >/dev/null 2>&1 || true
    done
fi
