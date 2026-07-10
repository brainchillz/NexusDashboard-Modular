// ─── Caddy reverse proxy (front door) ───────────────────
async function page_caddy() {
  if (currentRole !== 'admin') return adminOnlyPage('Caddy Proxy');
  const c = await API.get('/api/caddy');
  if (!c.available) {
    $('page-content').innerHTML = `<h2>Caddy Proxy</h2>
      <div class="alert alert-info">Caddy is not installed on this host, so there is nothing to manage here.
        Install it (<code>apt install caddy</code>) and reload this page.</div>`;
    return;
  }
  const badge = c.active
    ? '<span class="status-badge green">active</span>'
    : '<span class="status-badge red">inactive</span>';
  caddySitesCache = c.sites || [];
  caddyCertsCache = c.certs || [];
  const certRows = caddyCertsCache.map((t, i) => `<tr>
      <td><code>${escapeHtml(t.cert)}</code><br><code>${escapeHtml(t.key)}</code></td>
      <td>${t.subject ? escapeHtml(t.subject) : '<span class="help">details unreadable</span>'}</td>
      <td>${t.expires ? escapeHtml(t.expires) : ''}</td>
      <td>${c.editable ? `<button class="btn btn-sm btn-outline" onclick="caddyCertModal(${i})">Replace</button>` : ''}</td>
    </tr>`).join('');
  const rows = caddySitesCache.map((s, i) => `<tr>
      <td>${s.addresses.map(a => `<code>${escapeHtml(a)}</code>`).join(', ')}</td>
      <td>${s.upstream ? `<code>${escapeHtml(s.upstream)}</code>` : '<span class="help">custom configuration</span>'}
        ${s.skip_tls_verify ? ' <span class="help">(TLS verify off)</span>' : ''}</td>
      <td>${s.simple ? `<button class="btn btn-sm btn-outline" onclick="caddySiteModal(${i})">Edit</button> ` : ''}
        <button class="btn btn-sm btn-outline" onclick="caddyDeleteSite('${jsArg(s.addresses[0])}')">Delete</button></td>
    </tr>`).join('');
  $('page-content').innerHTML = `
    <h2>Caddy Proxy ${badge} ${c.version ? `<span class="help">${escapeHtml(c.version)}</span>` : ''}</h2>
    <p class="help">Reverse-proxy front door: one Caddy on :80/:443 terminates TLS and routes each hostname to a
      backend — adding an app is one route plus its DNS record. Proxied requests carry
      <code>X-Forwarded-For</code>, so backends that trust the proxy log real client addresses.</p>
    ${c.editable ? '' : `<div class="alert alert-warning">Changes are disabled: the root-owned caddy helper is not
      installed on this node (fresh installs ship it; older nodes need the helper and its sudoers line added by
      hand). Routes are shown read-only.</div>`}
    ${c.file_readable ? `
    <h3 style="margin-top:24px">Routes</h3>
    <table class="table">
      <thead><tr><th>Hostname</th><th>Backend</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="3">No site blocks yet — add a route to publish an app.</td></tr>'}</tbody>
    </table>
    <div class="toolbar">
      ${c.editable ? '<button class="btn" onclick="caddySiteModal()">+ Add Route</button> ' : ''}
      <button class="btn btn-outline" onclick="caddyFileModal()">Edit Caddyfile</button>
    </div>
    ${certRows ? `
    <h3 style="margin-top:24px">TLS Certificates</h3>
    <p class="help">Certificate/key pairs referenced by <code>tls</code> directives. Replace a pair at renewal —
      the new pair is checked (valid PEM, key matches certificate) before anything is written, then Caddy reloads.</p>
    <table class="table">
      <thead><tr><th>Files</th><th>Subject</th><th>Expires</th><th></th></tr></thead>
      <tbody>${certRows}</tbody>
    </table>` : ''}`
    : `<div class="alert alert-warning"><code>${escapeHtml(c.caddyfile)}</code> is not readable by the dashboard
        user — the stock packaging is world-readable; restore that (or relax its group) to manage routes here.</div>`}`;
}

let caddySitesCache = [];
let caddyCertsCache = [];
let caddyEditingHost = null;   // original hostname when the modal is editing

// Published TCP ports of running docker containers, for the backend picker.
// Container-network IPs are deliberately NOT offered: they change whenever a
// stack is recreated, so a route pointed at one breaks silently.
async function caddyDockerBackends() {
  try {
    const cts = await API.get('/api/docker/containers');
    const opts = [];
    (Array.isArray(cts) ? cts : []).forEach(c => {
      if (c.state !== 'running') return;
      (c.ports || []).forEach(p => {
        const m = p.match(/^(?:(\d+\.\d+\.\d+\.\d+):)?(\d+)->(\d+)\/tcp$/);
        if (!m) return;
        const host = (m[1] && m[1] !== '0.0.0.0') ? m[1] : '127.0.0.1';
        opts.push({ value: `${host}:${m[2]}`,
                    label: `${c.name} — ${host}:${m[2]} (container :${m[3]})` });
      });
    });
    return opts.sort((a, b) => a.label.localeCompare(b.label));
  } catch (e) { return []; }   // docker absent/disabled — no picker
}

async function caddySiteModal(idx) {
  const s = idx !== undefined ? caddySitesCache[idx] : null;
  caddyEditingHost = s ? s.addresses[0] : null;
  const backends = await caddyDockerBackends();
  const pick = backends.length ? `
    <div class="form-group"><label>…or pick a published Docker port</label>
      <select class="form-control" onchange="if(this.value)$('caddy-upstream').value=this.value">
        <option value="">— running containers —</option>
        ${backends.map(b => `<option value="${escapeHtml(b.value)}">${escapeHtml(b.label)}</option>`).join('')}
      </select></div>` : '';
  const curTls = s && s.tls_cert ? `${s.tls_cert} ${s.tls_key}` : '';
  const tlsSel = `
    <div class="form-group"><label>TLS certificate</label>
      <select id="caddy-tls" class="form-control">
        <option value="">Automatic (Let's Encrypt / caddy-managed)</option>
        ${caddyCertsCache.map(t => `<option value="${escapeHtml(`${t.cert} ${t.key}`)}"
          ${curTls === `${t.cert} ${t.key}` ? 'selected' : ''}>${escapeHtml(t.subject || t.cert)}</option>`).join('')}
      </select></div>`;
  openModal(s ? `Edit Route — ${s.addresses[0]}` : 'Add Route', `
    <div class="form-group"><label>Hostname (needs a DNS record pointing here; add :port to serve a
      non-standard port, e.g. <code>host.example.com:8000</code>)</label>
      <input id="caddy-host" class="form-control" placeholder="app.example.com"
        value="${s ? escapeHtml(s.addresses[0]) : ''}"></div>
    <div class="form-group"><label>Backend (host:port the app listens on)</label>
      <input id="caddy-upstream" class="form-control" placeholder="127.0.0.1:8080"
        value="${s ? escapeHtml(s.upstream) : ''}"></div>
    ${pick}
    ${tlsSel}
    <div class="form-group"><label><input type="checkbox" id="caddy-skip-verify"
        ${s && s.skip_tls_verify ? 'checked' : ''}>
      Backend serves HTTPS with a self-signed certificate (skip upstream TLS verification)</label></div>
    <button class="btn" onclick="caddySubmitSite()">${s ? 'Save Changes' : 'Add Route'}</button>`);
}

async function caddySubmitSite() {
  const tlsPair = $('caddy-tls').value.split(' ');
  const site = {
    host: $('caddy-host').value.trim(),
    upstream: $('caddy-upstream').value.trim(),
    skip_tls_verify: $('caddy-skip-verify').checked,
    tls_cert: tlsPair.length === 2 ? tlsPair[0] : '',
    tls_key: tlsPair.length === 2 ? tlsPair[1] : '',
  };
  if (!site.host || !site.upstream) { alert('Hostname and backend required'); return; }
  const body = caddyEditingHost ? { host: caddyEditingHost, new: site } : site;
  try {
    const r = await API.post(caddyEditingHost ? '/api/caddy/site/update' : '/api/caddy/site', body);
    if (!r.success) { alert(r.error || 'Failed'); return; }
    closeModal();
  } catch (e) { alert(e.message); return; }
  page_caddy();
}

async function caddyDeleteSite(host) {
  if (!confirm(`Delete the route for ${host}? Its hostname stops being served immediately.`)) return;
  try {
    const r = await API.post('/api/caddy/site/delete', { host });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_caddy();
}

function caddyCertModal(idx) {
  const t = caddyCertsCache[idx];
  openModal(`Replace Certificate — ${t.subject || t.cert}`, `
    <p class="help">Paste the new PEM pair. It is verified first (valid certificate, key matches),
      written to <code>${escapeHtml(t.cert)}</code> / <code>${escapeHtml(t.key)}</code> with the same
      owner and permissions, then Caddy reloads. The previous pair is backed up under <code>/run</code>.</p>
    <div class="form-group"><label>Certificate (PEM, include the CA chain)</label>
      <textarea id="caddy-cert-pem" class="form-control mono" rows="8"
        placeholder="-----BEGIN CERTIFICATE-----"></textarea></div>
    <div class="form-group"><label>Private key (PEM)</label>
      <textarea id="caddy-key-pem" class="form-control mono" rows="6"
        placeholder="-----BEGIN PRIVATE KEY-----"></textarea></div>
    <button class="btn" onclick="caddySubmitCert(${idx})">Verify &amp; Replace</button>`);
}

async function caddySubmitCert(idx) {
  const t = caddyCertsCache[idx];
  try {
    const r = await API.post('/api/caddy/cert', {
      cert_path: t.cert, key_path: t.key,
      cert: $('caddy-cert-pem').value.trim(), key: $('caddy-key-pem').value.trim(),
    });
    if (!r.success) { alert(r.error || 'Failed'); return; }
    closeModal();
  } catch (e) { alert(e.message); return; }
  page_caddy();
}

async function caddyFileModal() {
  let r;
  try { r = await API.get('/api/caddy/file'); } catch (e) { alert(e.message); return; }
  if (r.error) { alert(r.error); return; }
  openModal(`Caddyfile — ${r.file}`, `
    ${r.editable ? '<p class="help">Saving validates with <code>caddy validate</code> first — a rejected config never touches the live file — then reloads the service.</p>'
                 : '<p class="help">Read-only: the caddy helper is not installed on this node.</p>'}
    <textarea id="caddy-file-content" class="form-control mono" rows="20"
      style="white-space:pre;overflow-x:auto" ${r.editable ? '' : 'readonly'}>${escapeHtml(r.content)}</textarea>
    ${r.editable ? '<button class="btn" style="margin-top:10px" onclick="caddySaveFile()">Validate &amp; Save</button>' : ''}`);
}

async function caddySaveFile() {
  try {
    const r = await API.post('/api/caddy/file', { content: $('caddy-file-content').value });
    if (!r.success) { alert(r.error || 'Failed'); return; }
    closeModal();
  } catch (e) { alert(e.message); return; }
  page_caddy();
}
