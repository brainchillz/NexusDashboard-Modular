async function page_tasks() {
  const r = await API.get('/api/tasks');
  const rows = (r.tasks || []).map(t => {
    const armed = t.timer_active
      ? '<span class="status-badge green">armed</span>'
      : '<span class="status-badge gray">off</span>';
    const res = t.running ? '<span class="status-badge">running…</span>'
      : t.last_run == null ? '<span class="status-badge gray">never run</span>'
      : t.ok ? '<span class="status-badge green">ok</span>'
      : `<span class="status-badge red">failed (${escapeHtml(t.last_result)})</span>`;
    return `<tr>
      <td><strong>${escapeHtml(t.label)}</strong><div class="card-sub">${escapeHtml(t.desc)}</div></td>
      <td>${armed}</td>
      <td>${res}</td>
      <td>${fmtTs(t.last_run)}</td>
      <td>${t.timer_active ? fmtTs(t.next_run) : '-'}</td>
      <td><button class="btn btn-sm" onclick="taskRun('${jsArg(t.id)}')">Run now</button></td>
    </tr>`;
  }).join('');
  $('page-content').innerHTML = `
    <h2>Scheduled Tasks</h2>
    <p class="help">systemd timers the dashboard manages. Each is <em>armed</em> only when its
      feature is configured; a disarmed task never runs. <strong>Run now</strong> triggers the job
      immediately (independent of its schedule).</p>
    <table class="table">
      <thead><tr><th>Task</th><th>Timer</th><th>Last result</th><th>Last run</th><th>Next run</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function page_logs() {
  const r = await API.get('/api/logs/sources');
  const opts = (r.sources || []).map(s =>
    `<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)} — ${escapeHtml(s.unit)}</option>`).join('');
  const prios = [['', 'All'], ['3', 'Error+'], ['4', 'Warning+'], ['6', 'Info+']]
    .map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  $('page-content').innerHTML = `
    <h2>Logs</h2>
    <p class="help">journald entries for dashboard-managed units. Read-only.</p>
    <div class="toolbar">
      <select id="log-source" class="form-control" style="max-width:360px">${opts}</select>
      <select id="log-priority" class="form-control" style="max-width:130px">${prios}</select>
      <select id="log-lines" class="form-control" style="max-width:110px">
        <option value="100">100</option><option value="200" selected>200</option>
        <option value="500">500</option><option value="1000">1000</option>
      </select>
      <input id="log-grep" class="form-control" style="max-width:220px" placeholder="filter text (optional)" autocomplete="off">
      <button class="btn btn-sm" onclick="logsRefresh()">Refresh</button>
    </div>
    <pre id="log-output" class="log-view">Select a source and press Refresh.</pre>`;
  const g = $('log-grep');
  if (g) g.addEventListener('keydown', e => { if (e.key === 'Enter') logsRefresh(); });
  logsRefresh();
}

async function page_services() {
  const [status, install] = await Promise.all([
    API.get('/api/status'),
    API.get('/api/install/status')
  ]);
  let rows = Object.entries(status).map(([k, v]) => `
    <tr>
      <td>${escapeHtml(v.name)}</td>
      <td>${escapeHtml(install[k]?.package || '-')}</td>
      <td><span class="status-badge ${install[k]?.installed ? 'green' : 'red'}">${install[k]?.installed ? 'Installed' : 'Missing'}</span></td>
      <td><span class="status-badge ${v.active === 'active' ? 'green' : v.active === 'inactive' ? 'yellow' : 'red'}">${v.active}</span></td>
      <td><span class="status-badge ${v.enabled === 'enabled' ? 'green' : 'gray'}">${v.enabled}</span></td>
      <td>
        <button class="btn btn-sm" onclick="svcAction('${k}','start')">Start</button>
        <button class="btn btn-sm btn-warning" onclick="svcAction('${k}','stop')">Stop</button>
        <button class="btn btn-sm" onclick="svcAction('${k}','restart')">Restart</button>
        <button class="btn btn-sm btn-outline" onclick="svcAction('${k}','enable')">Enable</button>
        <button class="btn btn-sm btn-outline" onclick="svcAction('${k}','disable')">Disable</button>
        <button class="btn btn-sm btn-outline" onclick="svcLogs('${k}')">Logs</button>
      </td>
    </tr>
  `).join('');
  let missing = Object.entries(install).filter(([,v]) => !v.installed).length;
  $('page-content').innerHTML = `
    <h2>Service Manager</h2>
    ${missing > 0 ? `<div class="alert alert-warning">${missing} service(s) not installed. Run <code>sudo ./install-prerequisites.sh</code> on the host to install them.</div>` : ''}
    <table class="table">
      <thead><tr><th>Service</th><th>Package</th><th>Installed</th><th>Status</th><th>Boot</th><th>Actions</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

async function page_account() {
  $('page-content').innerHTML = `
    <h2>My Account</h2>
    <p>Signed in as <strong>${escapeHtml(currentUser || 'admin')}</strong> (${escapeHtml(currentRole)}).</p>
    <button class="btn" onclick="changePassword()">Change My Password</button>`;
}

// "Users & Tokens": manage dashboard accounts and API tokens (admin only).
async function page_users() {
  if (currentRole !== 'admin') return adminOnlyPage('Users & Tokens');
  let users = [];
  try { users = await API.get('/api/users'); } catch(e) {}
  const urows = users.map(u => `<tr>
      <td>${escapeHtml(u.username)}</td>
      <td><span class="status-badge ${u.role === 'admin' ? 'green' : 'gray'}">${u.role}</span></td>
      <td>${u.smb ? '<span class="status-badge gray">SMB</span>' : '—'}</td>
      <td>
        <button class="btn btn-sm" onclick="userSetRole('${jsArg(u.username)}','${u.role === 'admin' ? 'readonly' : 'admin'}')">Make ${u.role === 'admin' ? 'read-only' : 'admin'}</button>
        <button class="btn btn-sm" onclick="userPassword('${jsArg(u.username)}')">Password</button>
        <button class="btn btn-sm btn-danger" onclick="userDelete('${jsArg(u.username)}')">Delete</button>
      </td>
    </tr>`).join('');
  let tokens = [];
  try { tokens = await API.get('/api/tokens'); } catch(e) {}
  const trows = tokens.map(t => `<tr>
      <td>${escapeHtml(t.name)}</td>
      <td><span class="status-badge ${t.role === 'admin' ? 'green' : 'gray'}">${t.role}</span></td>
      <td>${escapeHtml(t.created || '—')}</td>
      <td>${escapeHtml(t.last_used || 'never')}</td>
      <td><button class="btn btn-sm btn-danger" onclick="tokenDelete('${jsArg(t.id)}','${jsArg(t.name)}')">Revoke</button></td>
    </tr>`).join('');
  $('page-content').innerHTML = `
    <h2>Users &amp; Tokens</h2>
    <h3>Dashboard Users</h3>
    <div class="toolbar"><button class="btn" onclick="userCreate()">+ New User</button></div>
    <table class="table">
      <thead><tr><th>Username</th><th>Role</th><th>SMB</th><th>Actions</th></tr></thead>
      <tbody>${urows || '<tr><td colspan="4">No users</td></tr>'}</tbody>
    </table>
    <p class="help">Administrators can do anything; read-only users can view but not change. "SMB user" also
      creates a matching Samba account for file sharing.</p>
    <h3 style="margin-top:24px">API Tokens</h3>
    <div class="toolbar"><button class="btn" onclick="tokenCreate()">+ New Token</button></div>
    <table class="table">
      <thead><tr><th>Name</th><th>Role</th><th>Created</th><th>Last used</th><th>Actions</th></tr></thead>
      <tbody>${trows || '<tr><td colspan="5">No API tokens</td></tr>'}</tbody>
    </table>
    <p class="help">For automation/scripts. Send the token as <code>Authorization: Bearer &lt;token&gt;</code>
      (or <code>X-API-Token</code>). A token carries a role (admin or read-only) and is shown only once at creation.</p>`;
}

async function page_notifications() {
  if (currentRole !== 'admin') return adminOnlyPage('Notifications');
  let n = { email: {}, webhook: {}, active_alerts: {} };
  try { n = await API.get('/api/notifications'); } catch(e) {}
  const em = n.email || {}, wb = n.webhook || {};
  const active = Object.values(n.active_alerts || {});
  $('page-content').innerHTML = `
    <h2>Notifications</h2>
    ${active.length ? `<div class="alert alert-warning"><strong>Active alerts:</strong> ${active.map(escapeHtml).join(' · ')}</div>`
                    : '<div class="health-ok">✓ No active alerts</div>'}
    <div class="form-group"><label><input type="checkbox" id="nf-email-en" ${em.enabled ? 'checked' : ''}> Email (SMTP)</label></div>
    <div class="form-group" style="display:flex;gap:8px">
      <div style="flex:2"><label>SMTP host</label><input id="nf-host" class="form-control" value="${escapeHtml(em.host || '')}"></div>
      <div style="flex:1"><label>Port</label><input id="nf-port" class="form-control" value="${escapeHtml(em.port || 587)}"></div>
      <div style="flex:1"><label>Security</label><select id="nf-sec" class="form-control">
        <option value="starttls" ${em.security === 'starttls' ? 'selected' : ''}>STARTTLS</option>
        <option value="ssl" ${em.security === 'ssl' ? 'selected' : ''}>SSL/TLS</option>
        <option value="none" ${em.security === 'none' ? 'selected' : ''}>None</option></select></div>
    </div>
    <div class="form-group" style="display:flex;gap:8px">
      <div style="flex:1"><label>Username</label><input id="nf-user" class="form-control" autocomplete="off" value="${escapeHtml(em.username || '')}"></div>
      <div style="flex:1"><label>Password</label><input id="nf-pass" type="password" class="form-control" autocomplete="new-password" value="${em.password ? escapeHtml(em.password) : ''}" placeholder="(unchanged)"></div>
    </div>
    <div class="form-group" style="display:flex;gap:8px">
      <div style="flex:1"><label>From</label><input id="nf-from" class="form-control" value="${escapeHtml(em.from || '')}" placeholder="alerts@example.com"></div>
      <div style="flex:1"><label>To</label><input id="nf-to" class="form-control" value="${escapeHtml(em.to || '')}" placeholder="me@example.com"></div>
    </div>
    <div class="form-group"><label><input type="checkbox" id="nf-web-en" ${wb.enabled ? 'checked' : ''}> Webhook (HTTP POST JSON)</label></div>
    <div class="form-group"><label>Webhook URL</label><input id="nf-url" class="form-control" value="${escapeHtml(wb.url || '')}" placeholder="https://hooks.example.com/..."></div>
    <div class="toolbar"><button class="btn" onclick="notifSave()">Save</button>
      <button class="btn btn-outline" onclick="notifTest()">Send Test (saved config)</button></div>
    <div id="nf-result" class="help"></div>
    <p class="help">Alerts: degraded/faulted pool, pool ≥90% full, a stopped service, a SMART failure — sent once per
      condition (re-checked every 15 min, with a "resolved" notice when it clears). Enabling a channel turns on the background check.</p>`;
}

async function page_certificate() {
  if (currentRole !== 'admin') return adminOnlyPage('Certificate');
  const tls = await API.get('/api/tls/info');
  const badge = tls.self_signed
    ? '<span class="status-badge yellow">Self-signed</span>'
    : '<span class="status-badge green">Custom / CA-issued</span>';
  const certBlock = tls.present ? `
    <table class="table">
      <tbody>
        <tr><td>Type</td><td>${badge}</td></tr>
        <tr><td>Subject</td><td>${escapeHtml(tls.subject || '-')}</td></tr>
        <tr><td>Issuer</td><td>${escapeHtml(tls.issuer || '-')}</td></tr>
        <tr><td>Expires</td><td>${escapeHtml(tls.expires || '-')}</td></tr>
        <tr><td>File</td><td>${escapeHtml(tls.path || '-')}</td></tr>
      </tbody>
    </table>` : '<p>No certificate present.</p>';
  $('page-content').innerHTML = `
    <h2>TLS Certificate</h2>
    ${tls.tls_enabled ? '' : '<div class="alert alert-warning">TLS is disabled (DASHBOARD_TLS=0) — the dashboard is serving plain HTTP.</div>'}
    ${certBlock}
    <div class="toolbar"><button class="btn btn-outline" onclick="tlsRegenerate()">Regenerate self-signed</button></div>
    <h3>Install a custom certificate</h3>
    <p class="help">Paste a PEM certificate (include the full chain if applicable) and its private key. Changes take effect after a service restart.</p>
    <div class="form-group"><label>Certificate (PEM)</label><textarea id="tls-cert" class="form-control" rows="6" placeholder="-----BEGIN CERTIFICATE-----"></textarea></div>
    <div class="form-group"><label>Private Key (PEM)</label><textarea id="tls-key" class="form-control" rows="6" placeholder="-----BEGIN PRIVATE KEY-----"></textarea></div>
    <button class="btn" onclick="tlsUploadCert()">Save Certificate</button>`;
}

async function page_audit() {
  if (currentRole !== 'admin') return adminOnlyPage('Audit Log');
  $('page-content').innerHTML = `
    <h2>Audit Log</h2>
    <div class="toolbar"><button class="btn btn-sm" onclick="auditRefresh()">Refresh</button></div>
    <div id="audit-body"><p class="help">Loading…</p></div>
    <p class="help">Records every change (and login attempt): who, when, from where, and the result.
      Reads are not logged. Stored append-only in <code>audit.log</code>.</p>`;
  auditRefresh();
}

// ─── Modules ────────────────────────────────────────────
async function page_modules() {
  if (currentRole !== 'admin') return adminOnlyPage('Modules');
  const r = await API.get('/api/modules');
  const cats = {};
  (r.modules || []).forEach(m => { (cats[m.category] = cats[m.category] || []).push(m); });
  let html = `<h2>Modules</h2>
    <p class="help">Turn features on or off. Disabling a module hides it from the
      left-hand navigation for everyone — it does not delete any data or stop the
      underlying service, and you can re-enable it here at any time.</p>`;
  for (const [cat, list] of Object.entries(cats)) {
    html += `<div class="card"><h3>${escapeHtml(cat)}</h3><table class="table"><tbody>`;
    list.forEach(m => {
      html += `<tr>
        <td>${escapeHtml(m.label)}</td>
        <td style="text-align:right">
          <label class="switch">
            <input type="checkbox" ${m.enabled ? 'checked' : ''}
                   onchange="toggleModule('${jsArg(m.id)}', this.checked)">
            <span class="slider"></span>
          </label>
        </td></tr>`;
    });
    html += `</tbody></table></div>`;
  }
  $('page-content').innerHTML = html;
}

function userCreate() {
  openModal('New Dashboard User', `
    <div class="form-group"><label>Username</label><input id="nu-name" class="form-control" autocomplete="off"></div>
    <div class="form-group"><label>Password</label><input id="nu-pass" type="password" class="form-control" autocomplete="new-password"></div>
    <div class="form-group"><label>Role</label>
      <select id="nu-role" class="form-control"><option value="readonly">Read-only (view only)</option><option value="admin">Administrator (full access)</option></select></div>
    <div class="form-group"><label><input type="checkbox" id="nu-smb"> Also create a matching SMB user (same name &amp; password) for file shares</label></div>
    <button class="btn" onclick="userDoCreate()">Create User</button>`);
}
function userPassword(username) {
  openModal('Set password: ' + username, `
    <div class="form-group"><label>New password</label><input id="up-pass" type="password" class="form-control" autocomplete="new-password"></div>
    <button class="btn" onclick="userDoPassword('${jsArg(username)}')">Set Password</button>`);
}
function tokenCreate() {
  openModal('New API Token', `
    <div class="form-group"><label>Name (what it's for)</label>
      <input id="tk-name" class="form-control" placeholder="backup-script" autocomplete="off"></div>
    <div class="form-group"><label>Role</label>
      <select id="tk-role" class="form-control">
        <option value="readonly">Read-only (GET only)</option>
        <option value="admin">Administrator (full access)</option>
      </select></div>
    <button class="btn" onclick="tokenDoCreate()">Create Token</button>`);
}

