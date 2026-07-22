# --- nexus-dashboard shared post-install body (POSIX sh) ----------------------
# Embedded verbatim into the deb postinst and the rpm %post. Idempotent: safe on
# install AND upgrade. Mirrors install.sh's activation steps (the file payload —
# app tree, venv, units, helpers, sudoers — is already laid down by the package).
APPDIR=/opt/nexus-dashboard
SVCUSER=dashboard

# 1. Service user (system account, no login, home = app dir).
if ! id -u "$SVCUSER" >/dev/null 2>&1; then
    useradd -r -s /usr/sbin/nologin -M -d "$APPDIR" "$SVCUSER"
fi

# 2. Socket-group membership for the container/docker modules (no sudo needed);
#    harmless where a daemon/group is absent — the module just reports it down.
for _g in lxd incus-admin docker; do
    if getent group "$_g" >/dev/null 2>&1; then usermod -aG "$_g" "$SVCUSER" || true; fi
done

# 3. Seed the default-off module state — ONLY if absent, so an upgrade or a
#    re-install never clobbers the operator's Modules-page toggles.
if [ ! -e "$APPDIR/modules.json" ]; then
    cat > "$APPDIR/modules.json" <<'MODULESJSON'
{"disabled": ["caddy", "compose", "ctnetworks", "docker", "gpu", "images", "instances", "llamacpp", "portforward"], "enabled": []}
MODULESJSON
fi

# 4. dnsmasq module conf-dir drop-in — only when dnsmasq is present; the module
#    is off by default, so the render dir stays empty (a no-op) until enabled.
if command -v dnsmasq >/dev/null 2>&1 && [ ! -e /etc/dnsmasq.d/zz-nexus-dashboard.conf ]; then
    mkdir -p /etc/dnsmasq.d
    printf 'conf-dir=%s/dnsmasq/render/dnsmasq.d,*.conf\n' "$APPDIR" \
        > /etc/dnsmasq.d/zz-nexus-dashboard.conf
fi

# 5. Ownership: the app tree, the bundled venv, and state go to the service user.
mkdir -p /var/log/nexus-dashboard
chown -R "$SVCUSER":"$SVCUSER" "$APPDIR" /var/log/nexus-dashboard

# 6. Sudoers sanity — fail the install loudly if the shipped policy won't parse
#    (a broken /etc/sudoers.d file can lock out sudo).
if command -v visudo >/dev/null 2>&1; then
    visudo -cf /etc/sudoers.d/nexus-dashboard >/dev/null \
        || { echo "nexus-dashboard: shipped sudoers failed validation" >&2; exit 1; }
fi

# 7. systemd: register units, turn on the history sampler + the dashboard itself.
#    The feature timers (autosnap/replicate/alerts/maintenance) stay disabled —
#    the app enables each when its feature is configured.
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
    systemctl enable --now nexus-dashboard-history.timer >/dev/null 2>&1 || true
    systemctl enable nexus-dashboard.service >/dev/null 2>&1 || true
    systemctl start nexus-dashboard.service || true
fi

echo "nexus-dashboard installed — https://<this-host>:8443 (self-signed TLS)."
echo "First-start admin password is written to the log; retrieve it with:"
echo "  sudo grep -A2 'initial admin account' /var/log/nexus-dashboard/app.log"
