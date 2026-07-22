# --- nexus-dashboard shared post-remove body (POSIX sh) -----------------------
# Embedded into the deb postrm and the rpm %postun. Re-reads unit files after the
# package's units are gone. Deliberately does NOT delete the service user, the
# app-dir state (auth.json, modules.json, history.db, certs), or the log dir —
# operator data survives a package removal; purge those by hand if wanted.
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
