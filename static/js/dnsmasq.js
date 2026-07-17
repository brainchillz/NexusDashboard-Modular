// DNS & DHCP module — three pages: DNS Overrides (dnshosts), DHCP (dhcp),
// DNS Config (dnsconfig). Ported from the standalone DNSMAQ-MGR appliance;
// all API paths are namespaced under /api/dnsmasq/. Uses core.js helpers
// (API, $, escapeHtml, jsArg, openModal, closeModal, sparkline).
const DMAPI = '/api/dnsmasq';
let _dmDns = null, _dmDhcp = null, _dmNb = null;

function dmSwitch(checked, onchange, disabled) {
  return `<label class="switch"><input type="checkbox" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''} onchange="${onchange}"><span class="slider"></span></label>`;
}
function dmBadge(on) { return `<span class="status-badge ${on ? 'green' : 'gray'}">${on ? 'enabled' : 'disabled'}</span>`; }
function dmDur(sec) {
  if (sec == null) return 'infinite';
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function dmNotify(r) {
  if (r && r.service_ok === false)
    alert('Saved, but dnsmasq did not come back cleanly: ' + (r.service_detail || 'check DNS Config → logs.'));
}
// POST returning the parsed body even on error (for the DHCP-conflict 409 flow).
async function dmPost(path, body) {
  const r = await fetch(DMAPI + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
  });
  if (r.status === 401) { onUnauthorized(); }
  let j = {}; try { j = await r.json(); } catch (e) {}
  return { ok: r.ok, status: r.status, json: j };
}

// ─── DNS Overrides page ─────────────────────────────────────────────
async function page_dnshosts() {
  const [d, s] = await Promise.all([API.get(DMApiDns()), API.get(DMApiSettings())]);
  _dmDns = d;
  const admin = currentRole === 'admin';
  const rows = (coll, cols) => (d[coll] || []).map(cols).join('');
  const hostR = h => `<tr><td><code>${escapeHtml(h.name)}</code></td><td>${escapeHtml(h.a || '-')}</td>
    <td>${escapeHtml(h.aaaa || '-')}</td><td>${dmBadge(h.enabled)}</td><td>${escapeHtml(h.comment || '')}</td>
    <td>${admin ? `<button class="btn btn-sm btn-outline" onclick="dmHostModal('${jsArg(h.id)}')">Edit</button>
      <button class="btn btn-sm btn-danger" onclick="dmDnsDel('hosts','${jsArg(h.id)}','${jsArg(h.name)}')">Delete</button>` : ''}</td></tr>`;
  const cnameR = c => `<tr><td><code>${escapeHtml(c.alias)}</code></td><td><code>${escapeHtml(c.target)}</code></td>
    <td>${dmBadge(c.enabled)}</td><td>${admin ? `<button class="btn btn-sm btn-outline" onclick="dmCnameModal('${jsArg(c.id)}')">Edit</button>
      <button class="btn btn-sm btn-danger" onclick="dmDnsDel('cnames','${jsArg(c.id)}','${jsArg(c.alias)}')">Delete</button>` : ''}</td></tr>`;
  const addrR = a => `<tr><td><code>${escapeHtml(a.domain)}</code></td><td>${escapeHtml(a.ip)}</td>
    <td>${dmBadge(a.enabled)}</td><td>${admin ? `<button class="btn btn-sm btn-outline" onclick="dmAddrModal('${jsArg(a.id)}')">Edit</button>
      <button class="btn btn-sm btn-danger" onclick="dmDnsDel('addresses','${jsArg(a.id)}','${jsArg(a.domain)}')">Delete</button>` : ''}</td></tr>`;
  const fwdR = f => `<tr><td><code>${escapeHtml(f.domain)}</code></td><td>${escapeHtml(f.upstream)}</td>
    <td>${dmBadge(f.enabled)}</td><td>${admin ? `<button class="btn btn-sm btn-outline" onclick="dmFwdModal('${jsArg(f.id)}')">Edit</button>
      <button class="btn btn-sm btn-danger" onclick="dmDnsDel('forwards','${jsArg(f.id)}','${jsArg(f.domain)}')">Delete</button>` : ''}</td></tr>`;

  $('page-content').innerHTML = `
    <h2>DNS Overrides</h2>
    <h3>Host Records <span class="help">(name &rarr; A/AAAA, the dnsmasq hosts file)</span></h3>
    ${admin ? `<div class="toolbar"><button class="btn btn-sm" onclick="dmHostModal()">+ Add host record</button>
      <button class="btn btn-sm btn-outline" onclick="dmImportModal()">&#8681; Import hosts file</button></div>` : ''}
    <table class="table"><thead><tr><th>Name</th><th>A</th><th>AAAA</th><th>State</th><th>Comment</th><th></th></tr></thead>
      <tbody>${rows('hosts', hostR) || '<tr><td colspan="6">No host records</td></tr>'}</tbody></table>

    <h3 style="margin-top:24px">CNAMEs</h3>
    ${admin ? `<div class="toolbar"><button class="btn btn-sm" onclick="dmCnameModal()">+ Add CNAME</button></div>` : ''}
    <table class="table"><thead><tr><th>Alias</th><th>Target</th><th>State</th><th></th></tr></thead>
      <tbody>${rows('cnames', cnameR) || '<tr><td colspan="4">No CNAMEs</td></tr>'}</tbody></table>

    <h3 style="margin-top:24px">Domain Overrides <span class="help">(address=/domain/ip &mdash; whole domain to one IP; 0.0.0.0 blocks)</span></h3>
    ${admin ? `<div class="toolbar"><button class="btn btn-sm" onclick="dmAddrModal()">+ Add domain override</button></div>` : ''}
    <table class="table"><thead><tr><th>Domain</th><th>IP</th><th>State</th><th></th></tr></thead>
      <tbody>${rows('addresses', addrR) || '<tr><td colspan="4">No domain overrides</td></tr>'}</tbody></table>

    <h3 style="margin-top:24px">Domain Forwards <span class="help">(server=/domain/upstream)</span></h3>
    ${admin ? `<div class="toolbar"><button class="btn btn-sm" onclick="dmFwdModal()">+ Add forward</button></div>` : ''}
    <table class="table"><thead><tr><th>Domain</th><th>Upstream</th><th>State</th><th></th></tr></thead>
      <tbody>${rows('forwards', fwdR) || '<tr><td colspan="4">No domain forwards</td></tr>'}</tbody></table>

    <h3 style="margin-top:24px">Upstream Servers</h3>
    <div class="form-group" style="max-width:480px"><label>Default resolvers (one per line, IP or IP#port)</label>
      <textarea id="dm-ups" class="form-control" rows="3" ${admin ? '' : 'disabled'}>${escapeHtml((s.upstreams || []).join('\n'))}</textarea></div>
    ${admin ? `<button class="btn" onclick="dmSaveUpstreams()">Save upstreams</button>` : ''}`;
}
function DMApiDns() { return DMAPI + '/dns'; }
function DMApiSettings() { return DMAPI + '/settings'; }
function _dmRec(coll, id) { return (_dmDns[coll] || []).find(r => r.id === id) || {}; }
function _dmCommon(r) {
  return `<div class="form-group"><label>Comment</label><input id="dm-comment" class="form-control" value="${escapeHtml(r.comment || '')}"></div>
    <label class="checkitem" style="padding-left:0"><input id="dm-enabled" type="checkbox" ${r.enabled !== false ? 'checked' : ''}> Enabled</label>`;
}
function dmHostModal(id) {
  const r = id ? _dmRec('hosts', id) : {};
  openModal(id ? 'Edit host record' : 'Add host record', `
    <div class="form-group"><label>Hostname</label><input id="dm-name" class="form-control" value="${escapeHtml(r.name || '')}" placeholder="nas or nas.lan"></div>
    <div class="form-group"><label>IPv4 (A)</label><input id="dm-a" class="form-control" value="${escapeHtml(r.a || '')}" placeholder="10.0.0.5"></div>
    <div class="form-group"><label>IPv6 (AAAA)</label><input id="dm-aaaa" class="form-control" value="${escapeHtml(r.aaaa || '')}" placeholder="optional"></div>
    ${_dmCommon(r)}
    <button class="btn" onclick="dmDnsSave('hosts','${jsArg(id || '')}',{name:$('dm-name').value.trim(),a:$('dm-a').value.trim(),aaaa:$('dm-aaaa').value.trim()})">${id ? 'Save' : 'Add'}</button>`);
}
function dmCnameModal(id) {
  const r = id ? _dmRec('cnames', id) : {};
  openModal(id ? 'Edit CNAME' : 'Add CNAME', `
    <div class="form-group"><label>Alias</label><input id="dm-alias" class="form-control" value="${escapeHtml(r.alias || '')}" placeholder="www.lan"></div>
    <div class="form-group"><label>Target</label><input id="dm-target" class="form-control" value="${escapeHtml(r.target || '')}" placeholder="nas.lan"></div>
    ${_dmCommon(r)}
    <button class="btn" onclick="dmDnsSave('cnames','${jsArg(id || '')}',{alias:$('dm-alias').value.trim(),target:$('dm-target').value.trim()})">${id ? 'Save' : 'Add'}</button>`);
}
function dmAddrModal(id) {
  const r = id ? _dmRec('addresses', id) : {};
  openModal(id ? 'Edit domain override' : 'Add domain override', `
    <div class="form-group"><label>Domain</label><input id="dm-domain" class="form-control" value="${escapeHtml(r.domain || '')}" placeholder="ads.example.com"></div>
    <div class="form-group"><label>IP (0.0.0.0 to block)</label><input id="dm-ip" class="form-control" value="${escapeHtml(r.ip || '')}"></div>
    ${_dmCommon(r)}
    <button class="btn" onclick="dmDnsSave('addresses','${jsArg(id || '')}',{domain:$('dm-domain').value.trim(),ip:$('dm-ip').value.trim()})">${id ? 'Save' : 'Add'}</button>`);
}
function dmFwdModal(id) {
  const r = id ? _dmRec('forwards', id) : {};
  openModal(id ? 'Edit forward' : 'Add forward', `
    <div class="form-group"><label>Domain</label><input id="dm-domain" class="form-control" value="${escapeHtml(r.domain || '')}" placeholder="corp.example.com"></div>
    <div class="form-group"><label>Upstream (IP or IP#port)</label><input id="dm-upstream" class="form-control" value="${escapeHtml(r.upstream || '')}"></div>
    ${_dmCommon(r)}
    <button class="btn" onclick="dmDnsSave('forwards','${jsArg(id || '')}',{domain:$('dm-domain').value.trim(),upstream:$('dm-upstream').value.trim()})">${id ? 'Save' : 'Add'}</button>`);
}
async function dmDnsSave(coll, id, fields) {
  fields.comment = $('dm-comment').value; fields.enabled = $('dm-enabled').checked;
  try { const r = await API.post(`${DMAPI}/dns/${coll}${id ? '/' + encodeURIComponent(id) : ''}`, fields); dmNotify(r); closeModal(); page_dnshosts(); }
  catch (e) { alert(e.message); }
}
async function dmDnsDel(coll, id, name) {
  if (!confirm(`Delete "${name}"?`)) return;
  try { const r = await API.delete(`${DMAPI}/dns/${coll}/${encodeURIComponent(id)}`); dmNotify(r); page_dnshosts(); }
  catch (e) { alert(e.message); }
}
async function dmSaveUpstreams() {
  const ups = $('dm-ups').value.split('\n').map(x => x.trim()).filter(Boolean);
  try { const r = await API.post(DMApiSettings(), { upstreams: ups }); dmNotify(r); page_dnshosts(); }
  catch (e) { alert(e.message); }
}
function dmImportModal() {
  openModal('Import hosts file', `
    <p class="help">Standard unix hosts lines: <code>IP name [alias …]</code>. IPv4&rarr;A, IPv6&rarr;AAAA.</p>
    <div class="form-group"><input type="file" id="dm-file" class="form-control" accept=".txt,.hosts,text/plain" onchange="dmImportRead(this)"></div>
    <div class="form-group"><textarea id="dm-text" class="form-control" rows="9" spellcheck="false" placeholder="10.0.0.5 nas nas.lan"></textarea></div>
    <label class="checkitem" style="padding-left:0"><input id="dm-skip" type="checkbox" checked> Skip boilerplate (localhost, ip6-allnodes…)</label>
    <label class="checkitem" style="padding-left:0"><input id="dm-replace" type="checkbox"> Replace ALL existing host records</label>
    <div class="toolbar" style="margin-top:10px"><button class="btn" onclick="dmImportGo()">Import</button></div>
    <div id="dm-import-result"></div>`, { wide: true });
}
function dmImportRead(input) {
  const f = input.files && input.files[0]; if (!f) return;
  const rd = new FileReader(); rd.onload = () => { $('dm-text').value = rd.result; }; rd.readAsText(f);
}
async function dmImportGo() {
  const text = $('dm-text').value; if (!text.trim()) { alert('Paste or choose a hosts file'); return; }
  const replace = $('dm-replace').checked;
  if (replace && !confirm('Replace ALL existing host records?')) return;
  $('dm-import-result').innerHTML = '<p class="help">Importing…</p>';
  try {
    const r = await API.post(`${DMAPI}/dns/import`, { text, skip_boilerplate: $('dm-skip').checked, replace });
    dmNotify(r);
    $('dm-import-result').innerHTML = `<div class="health-ok">✓ ${r.added} added, ${r.updated} updated, ${r.unchanged} unchanged${r.skipped ? ', ' + r.skipped + ' skipped' : ''}${r.invalid ? ', ' + r.invalid + ' invalid' : ''} · applied via ${r.action}</div>
      <div class="toolbar" style="margin-top:8px"><button class="btn btn-sm" onclick="closeModal();page_dnshosts()">Done</button></div>`;
  } catch (e) { $('dm-import-result').innerHTML = `<div class="alert alert-warning">${escapeHtml(e.message)}</div>`; }
}

// ─── DHCP page ──────────────────────────────────────────────────────
const DM_OPT_PRESETS = [['option:router', 'Default gateway (3)'], ['option:dns-server', 'DNS servers (6)'],
  ['option:ntp-server', 'NTP servers (42)'], ['option:domain-name', 'Domain name (15)'],
  ['option:tftp-server', 'TFTP server (66)'], ['option:bootfile-name', 'Boot file (67)']];
async function page_dhcp() {
  const [d, st, leases] = await Promise.all([
    API.get(DMApiDhcp()), API.get(DMApiStatus()), API.get(DMApiDhcp() + '/leases').catch(() => ({ leases: [] }))]);
  _dmDhcp = d; const admin = currentRole === 'admin';
  const boot = d.boot || {};
  const rangeR = r => `<tr><td>${r.tag ? `<span class="badge-type">${escapeHtml(r.tag)}</span>` : (r.interface ? `<code>${escapeHtml(r.interface)}</code>` : '-')}</td>
    <td><code>${escapeHtml(r.start)} – ${escapeHtml(r.end)}</code></td><td>${escapeHtml(r.netmask || 'auto')}</td><td>${escapeHtml(r.lease)}</td>
    <td>${dmBadge(r.enabled)}</td><td>${admin ? `<button class="btn btn-sm btn-outline" onclick="dmRangeModal('${jsArg(r.id)}')">Edit</button>
      <button class="btn btn-sm btn-danger" onclick="dmDhcpDel('ranges','${jsArg(r.id)}','${jsArg(r.start)}')">Delete</button>` : ''}</td></tr>`;
  const statR = s => `<tr><td><code>${escapeHtml(s.mac)}</code></td><td>${escapeHtml(s.ip)}</td><td>${escapeHtml(s.hostname || '-')}</td>
    <td>${s.tag ? `<span class="badge-type">${escapeHtml(s.tag)}</span>` : '-'}</td><td>${dmBadge(s.enabled)}</td>
    <td>${admin ? `<button class="btn btn-sm btn-outline" onclick="dmStaticModal('${jsArg(s.id)}')">Edit</button>
      <button class="btn btn-sm btn-danger" onclick="dmDhcpDel('static_leases','${jsArg(s.id)}','${jsArg(s.mac)}')">Delete</button>` : ''}</td></tr>`;
  const optR = o => `<tr><td>${o.tag ? `<span class="badge-type">${escapeHtml(o.tag)}</span>` : '<span class="help">all</span>'}</td>
    <td><code>${escapeHtml(o.option)}</code></td><td>${escapeHtml(o.value || '-')}</td><td>${dmBadge(o.enabled)}</td>
    <td>${admin ? `<button class="btn btn-sm btn-outline" onclick="dmOptModal('${jsArg(o.id)}')">Edit</button>
      <button class="btn btn-sm btn-danger" onclick="dmDhcpDel('options','${jsArg(o.id)}','${jsArg(o.option)}')">Delete</button>` : ''}</td></tr>`;
  const leaseR = l => `<tr><td><code>${escapeHtml(l.mac)}</code></td><td>${escapeHtml(l.ip)}</td><td>${escapeHtml(l.hostname || '-')}</td>
    <td>${l.expiry ? dmDur(l.expires_in) : 'infinite'}</td><td>${l.static ? '<span class="status-badge green">static</span>' : '<span class="status-badge gray">dynamic</span>'}</td>
    <td>${admin && !l.static ? `<button class="btn btn-sm" onclick="dmReserve('${jsArg(l.mac)}','${jsArg(l.ip)}','${jsArg(l.hostname || '')}')">Reserve</button>` : ''}</td></tr>`;

  $('page-content').innerHTML = `
    <h2>DHCP</h2>
    ${st.dhcp_enabled ? '' : `<div class="alert alert-info">DHCP is <strong>disabled</strong>. Config is kept but not served.
      ${admin ? '<a href="#" onclick="dmToggle(\'dhcp_enabled\',true);return false">Enable DHCP</a> (on the DNS Config page)' : ''}</div>`}
    <h3>Pools / Ranges</h3>
    ${admin ? `<div class="toolbar"><button class="btn btn-sm" onclick="dmRangeModal()">+ Add range</button></div>` : ''}
    <table class="table"><thead><tr><th>Tag / Interface</th><th>Range</th><th>Netmask</th><th>Lease</th><th>State</th><th></th></tr></thead>
      <tbody>${(d.ranges || []).map(rangeR).join('') || '<tr><td colspan="6">No DHCP ranges</td></tr>'}</tbody></table>

    <h3 style="margin-top:24px">Static Leases</h3>
    ${admin ? `<div class="toolbar"><button class="btn btn-sm" onclick="dmStaticModal()">+ Add static lease</button></div>` : ''}
    <table class="table"><thead><tr><th>MAC</th><th>IP</th><th>Hostname</th><th>Tag</th><th>State</th><th></th></tr></thead>
      <tbody>${(d.static_leases || []).map(statR).join('') || '<tr><td colspan="6">No static leases</td></tr>'}</tbody></table>

    <h3 style="margin-top:24px">Options</h3>
    ${admin ? `<div class="toolbar"><button class="btn btn-sm" onclick="dmOptModal()">+ Add option</button></div>` : ''}
    <table class="table"><thead><tr><th>Tag</th><th>Option</th><th>Value</th><th>State</th><th></th></tr></thead>
      <tbody>${(d.options || []).map(optR).join('') || '<tr><td colspan="5">No options</td></tr>'}</tbody></table>

    <h3 style="margin-top:24px">Network Boot (external server)</h3>
    <div class="card" style="max-width:640px">
      <p class="help">Point PXE clients at a separate boot server. This module only sets the DHCP boot option
        (<code>dhcp-boot</code>) — it does not run a TFTP server.</p>
      <div class="form-group"><label>Boot filename</label><input id="dm-boot-file" class="form-control" value="${escapeHtml(boot.filename || '')}" placeholder="pxelinux.0 / ipxe.efi" ${admin ? '' : 'disabled'}></div>
      <div class="form-group"><label>Boot server (IP or hostname)</label><input id="dm-boot-srv" class="form-control" value="${escapeHtml(boot.server || '')}" placeholder="10.0.0.5" ${admin ? '' : 'disabled'}></div>
      ${admin ? `<button class="btn btn-sm" onclick="dmSaveBoot()">Save boot options</button>` : ''}
    </div>

    <h3 style="margin-top:24px">Live Leases <span class="help">(${(leases.leases || []).length} active)</span></h3>
    <table class="table"><thead><tr><th>MAC</th><th>IP</th><th>Hostname</th><th>Expires</th><th>Type</th><th></th></tr></thead>
      <tbody>${(leases.leases || []).map(leaseR).join('') || '<tr><td colspan="6">No active leases</td></tr>'}</tbody></table>`;
}
function DMApiDhcp() { return DMAPI + '/dhcp'; }
function DMApiStatus() { return DMAPI + '/status'; }
function _dmDrec(coll, id) { return (_dmDhcp[coll] || []).find(r => r.id === id) || {}; }
function _dmDCommon(r) {
  return `<div class="form-group"><label>Comment</label><input id="dh-comment" class="form-control" value="${escapeHtml(r.comment || '')}"></div>
    <label class="checkitem" style="padding-left:0"><input id="dh-enabled" type="checkbox" ${r.enabled !== false ? 'checked' : ''}> Enabled</label>`;
}
function dmRangeModal(id) {
  const r = id ? _dmDrec('ranges', id) : {};
  openModal(id ? 'Edit range' : 'Add DHCP range', `
    <div class="form-group"><label>Start</label><input id="dh-start" class="form-control" value="${escapeHtml(r.start || '')}" placeholder="10.0.0.100"></div>
    <div class="form-group"><label>End</label><input id="dh-end" class="form-control" value="${escapeHtml(r.end || '')}" placeholder="10.0.0.199"></div>
    <div class="form-group"><label>Netmask (optional)</label><input id="dh-netmask" class="form-control" value="${escapeHtml(r.netmask || '')}" placeholder="255.255.255.0"></div>
    <div class="form-group"><label>Lease</label><input id="dh-lease" class="form-control" value="${escapeHtml(r.lease || '12h')}" placeholder="12h / 90m / infinite"></div>
    <div class="form-group"><label>Tag (optional)</label><input id="dh-tag" class="form-control" value="${escapeHtml(r.tag || '')}"></div>
    <div class="form-group"><label>Interface (optional — cannot combine with a tag)</label><input id="dh-iface" class="form-control" value="${escapeHtml(r.interface || '')}" placeholder="eth0"></div>
    ${_dmDCommon(r)}
    <button class="btn" onclick="dmDhcpSave('ranges','${jsArg(id || '')}',{start:$('dh-start').value.trim(),end:$('dh-end').value.trim(),netmask:$('dh-netmask').value.trim(),lease:$('dh-lease').value.trim(),tag:$('dh-tag').value.trim(),interface:$('dh-iface').value.trim()})">${id ? 'Save' : 'Add'}</button>`);
}
function dmStaticModal(id, preset) {
  const r = id ? _dmDrec('static_leases', id) : (preset || {});
  openModal(id ? 'Edit static lease' : 'Add static lease', `
    <div class="form-group"><label>MAC</label><input id="dh-mac" class="form-control" value="${escapeHtml(r.mac || '')}" placeholder="aa:bb:cc:dd:ee:ff"></div>
    <div class="form-group"><label>IPv4</label><input id="dh-ip" class="form-control" value="${escapeHtml(r.ip || '')}"></div>
    <div class="form-group"><label>Hostname (optional)</label><input id="dh-hostname" class="form-control" value="${escapeHtml(r.hostname || '')}"></div>
    <div class="form-group"><label>Tag (optional)</label><input id="dh-tag" class="form-control" value="${escapeHtml(r.tag || '')}"></div>
    ${_dmDCommon(r)}
    <button class="btn" onclick="dmDhcpSave('static_leases','${jsArg(id || '')}',{mac:$('dh-mac').value.trim(),ip:$('dh-ip').value.trim(),hostname:$('dh-hostname').value.trim(),tag:$('dh-tag').value.trim()})">${id ? 'Save' : 'Add'}</button>`);
}
function dmOptModal(id) {
  const r = id ? _dmDrec('options', id) : {};
  const presets = DM_OPT_PRESETS.map(([v, l]) => `<option value="${escapeHtml(v)}" ${r.option === v ? 'selected' : ''}>${escapeHtml(l)}</option>`).join('');
  openModal(id ? 'Edit option' : 'Add DHCP option', `
    <div class="form-group"><label>Common options</label><select class="form-control" onchange="if(this.value)$('dh-option').value=this.value">
      <option value="">— pick or type below —</option>${presets}</select></div>
    <div class="form-group"><label>Option (number or option:name)</label><input id="dh-option" class="form-control" value="${escapeHtml(r.option || '')}" placeholder="option:router"></div>
    <div class="form-group"><label>Value</label><input id="dh-value" class="form-control" value="${escapeHtml(r.value || '')}" placeholder="10.0.0.1"></div>
    <div class="form-group"><label>Tag (optional)</label><input id="dh-tag" class="form-control" value="${escapeHtml(r.tag || '')}"></div>
    ${_dmDCommon(r)}
    <button class="btn" onclick="dmDhcpSave('options','${jsArg(id || '')}',{option:$('dh-option').value.trim(),value:$('dh-value').value.trim(),tag:$('dh-tag').value.trim()})">${id ? 'Save' : 'Add'}</button>`);
}
async function dmDhcpSave(coll, id, fields) {
  fields.comment = $('dh-comment').value; fields.enabled = $('dh-enabled').checked;
  try { const r = await API.post(`${DMApiDhcp()}/${coll}${id ? '/' + encodeURIComponent(id) : ''}`, fields); dmNotify(r); closeModal(); page_dhcp(); }
  catch (e) { alert(e.message); }
}
async function dmDhcpDel(coll, id, name) {
  if (!confirm(`Delete "${name}"?`)) return;
  try { const r = await API.delete(`${DMApiDhcp()}/${coll}/${encodeURIComponent(id)}`); dmNotify(r); page_dhcp(); }
  catch (e) { alert(e.message); }
}
function dmReserve(mac, ip, hostname) { dmStaticModal(null, { mac, ip, hostname }); }
async function dmSaveBoot() {
  try { const r = await API.post(`${DMApiDhcp()}/boot`, { filename: $('dm-boot-file').value.trim(), server: $('dm-boot-srv').value.trim() }); dmNotify(r); page_dhcp(); }
  catch (e) { alert(e.message); }
}

// ─── DNS Config page ────────────────────────────────────────────────
let _dmCfg = {}, _dmCfgActive = null;
async function page_dnsconfig() {
  if (currentRole !== 'admin') { $('page-content').innerHTML = '<h2>DNS Config</h2><div class="alert alert-warning">Administrator access required.</div>'; return; }
  const [st, s, cfg, cur] = await Promise.all([
    API.get(DMApiStatus()), API.get(DMApiSettings()), API.get(DMApiConfig()), API.get(DMApiStats()).catch(() => null)]);
  _dmCfg = cfg.files || {}; const names = Object.keys(_dmCfg);
  if (!_dmCfgActive || !names.includes(_dmCfgActive)) _dmCfgActive = names[0];
  const dns = cur && cur.dns;
  const degr = !st.installed ? '<div class="alert alert-warning"><strong>dnsmasq is not installed on this node.</strong> Install it (<code>apt install dnsmasq</code>); the module manages an existing dnsmasq.</div>'
    : !st.dropin_present ? '<div class="alert alert-warning"><strong>The conf-dir drop-in is missing.</strong> Fresh installs carry it; existing nodes need it added by hand (see the install docs). Config edits are blocked until then.</div>' : '';
  const flag = (id, label, val, help) => `<label class="checkitem" style="padding-left:0" title="${escapeHtml(help || '')}"><input id="${id}" type="checkbox" ${val ? 'checked' : ''}> ${label}</label>`;

  $('page-content').innerHTML = `
    <h2>DNS Config</h2>
    ${degr}
    <div class="cards">
      <div class="card"><div class="card-head"><span class="status-dot ${st.running ? 'green' : 'red'}"></span>dnsmasq</div>
        <div class="card-value" style="font-size:1.3em">${st.running ? 'running' : 'stopped'}</div>
        <div class="card-sub">${st.version ? 'v' + escapeHtml(st.version) : (st.installed ? 'installed' : 'not installed')}</div>
        <div class="toolbar" style="margin-top:8px"><button class="btn btn-sm btn-outline" onclick="dmRestart()">Restart</button>
          <button class="btn btn-sm btn-outline" onclick="dmLogs()">Logs</button></div></div>
      <div class="card"><div class="card-head">Features</div>
        <div style="display:flex;flex-direction:column;gap:10px;margin-top:6px">
          <div style="display:flex;justify-content:space-between;align-items:center"><span>DNS server</span>${dmSwitch(st.dns_enabled, "dmToggle('dns_enabled',this.checked)")}</div>
          <div style="display:flex;justify-content:space-between;align-items:center"><span>DHCP server</span>${dmSwitch(st.dhcp_enabled, "dmToggle('dhcp_enabled',this.checked)")}</div>
        </div></div>
      ${dns ? `<div class="card card-link" onclick="showPage('dhcp')"><div class="card-head">DNS cache</div>
        <div class="card-value">${dns.hit_ratio != null ? dns.hit_ratio : '-'}<span class="card-unit">% hit</span></div>
        <div class="card-sub">${dns.cachesize} slots · ${dns.hits} hits / ${dns.misses} misses</div><div id="dm-spark-hits"></div></div>` : ''}
    </div>

    <h3>DNS &amp; Network</h3>
    <div class="card" style="max-width:640px">
      <div class="form-group"><label>Local domain</label><input id="dm-domain" class="form-control" value="${escapeHtml(s.domain || '')}" placeholder="lan"></div>
      <div class="form-group"><label>Listen interfaces (one per line, empty = all)</label><textarea id="dm-ifaces" class="form-control" rows="2">${escapeHtml((s.interfaces || []).join('\n'))}</textarea></div>
      <div class="form-group"><label>Extra listen addresses (one per line)</label><textarea id="dm-addrs" class="form-control" rows="2">${escapeHtml((s.listen_addresses || []).join('\n'))}</textarea></div>
      <div class="form-group"><label>Cache size</label><input id="dm-cache" class="form-control" type="number" value="${s.cache_size}"></div>
      ${flag('dm-expand', 'Expand hosts (append domain to bare names)', s.expand_hosts)}
      ${flag('dm-bind', 'Bind interfaces (coexist with other resolvers)', s.bind_interfaces)}
      ${flag('dm-noresolv', 'Ignore /etc/resolv.conf (use only configured upstreams)', s.no_resolv)}
      ${flag('dm-domneed', 'Domain needed', s.domain_needed)}
      ${flag('dm-bogus', 'Bogus-priv', s.bogus_priv)}
      ${flag('dm-dnssec', 'DNSSEC validation', s.dnssec)}
      ${flag('dm-auth', 'DHCP authoritative', s.dhcp_authoritative)}
      ${flag('dm-logq', 'Log DNS queries', s.log_queries)}
      ${flag('dm-logd', 'Log DHCP', s.log_dhcp)}
      <div class="toolbar" style="margin-top:10px"><button class="btn" onclick="dmSaveSettings()">Save &amp; Apply</button></div>
    </div>

    <h3 style="margin-top:24px">Rendered config</h3>
    <div class="toolbar" id="dm-cfg-tabs">${names.map(n => `<button class="btn btn-sm ${n === _dmCfgActive ? '' : 'btn-outline'}" onclick="dmCfgShow('${jsArg(n)}')">${escapeHtml(n)}</button>`).join(' ')}</div>
    <pre class="raw-output" id="dm-cfg-view" style="max-height:340px;overflow:auto"></pre>
    <h3 style="margin-top:16px">Extra Options <span class="help">(raw dnsmasq directives → 90-extra.conf)</span></h3>
    <div class="form-group"><textarea id="dm-extra" class="form-control" rows="5" spellcheck="false">${escapeHtml(s.extra_options || '')}</textarea></div>
    <div class="toolbar"><button class="btn" onclick="dmSaveExtra()">Save &amp; Apply</button>
      <button class="btn btn-outline" onclick="dmValidate()">Validate</button>
      <button class="btn btn-outline btn-warning" onclick="dmForceApply()">Force re-render + restart</button></div>
    <div id="dm-cfg-result"></div>`;
  dmCfgShow(_dmCfgActive);
  try { const h = await API.get('/api/history?metric=dns_hits&since=86400'); const el = $('dm-spark-hits'); if (el) el.innerHTML = sparkline(h.points); } catch (e) {}
}
function DMApiConfig() { return DMAPI + '/config'; }
function DMApiStats() { return DMAPI + '/stats'; }
function dmCfgShow(name) {
  _dmCfgActive = name; const v = $('dm-cfg-view'); if (v) v.textContent = _dmCfg[name] || '';
  document.querySelectorAll('#dm-cfg-tabs .btn').forEach(b => b.classList.toggle('btn-outline', b.textContent !== name));
}
async function dmToggle(key, enabled, force) {
  const res = await dmPost('/settings/toggles', { [key]: enabled, force: !!force });
  if (res.status === 409 && res.json.conflict) {
    const list = (res.json.servers || []).map(s => `${s.server} (offered ${s.offer_ip})`).join(', ');
    if (confirm(`⚠ Another DHCP server is already active:\n\n    ${list}\n\nRunning two DHCP servers on one LAN causes conflicts. Enable anyway?`)) return dmToggle(key, enabled, true);
  } else if (!res.ok) { alert(res.json.error || 'Failed'); }
  else { dmNotify(res.json); if (res.json.probe_note) console.warn('probe:', res.json.probe_note); }
  if (typeof page_dnsconfig === 'function' && document.querySelector('.nav-list a.active[data-page="dnsconfig"]')) page_dnsconfig();
  else if (document.querySelector('.nav-list a.active[data-page="dhcp"]')) page_dhcp();
}
async function dmSaveSettings() {
  const body = {
    domain: $('dm-domain').value.trim(),
    interfaces: $('dm-ifaces').value.split('\n').map(x => x.trim()).filter(Boolean),
    listen_addresses: $('dm-addrs').value.split('\n').map(x => x.trim()).filter(Boolean),
    cache_size: parseInt($('dm-cache').value) || 0,
    expand_hosts: $('dm-expand').checked, bind_interfaces: $('dm-bind').checked, no_resolv: $('dm-noresolv').checked,
    domain_needed: $('dm-domneed').checked, bogus_priv: $('dm-bogus').checked, dnssec: $('dm-dnssec').checked,
    dhcp_authoritative: $('dm-auth').checked, log_queries: $('dm-logq').checked, log_dhcp: $('dm-logd').checked,
  };
  try { const r = await API.post(DMApiSettings(), body); dmNotify(r); alert('Settings applied.'); page_dnsconfig(); }
  catch (e) { alert(e.message); }
}
async function dmSaveExtra() {
  try { const r = await API.post(DMApiSettings(), { extra_options: $('dm-extra').value }); dmNotify(r); page_dnsconfig(); }
  catch (e) { $('dm-cfg-result').innerHTML = `<div class="alert alert-warning"><strong>Rejected:</strong> ${escapeHtml(e.message)}</div>`; }
}
async function dmValidate() {
  $('dm-cfg-result').innerHTML = '<p class="help">Validating…</p>';
  try {
    const r = await API.post(DMApiStatus().replace('/status', '/validate'), {});
    $('dm-cfg-result').innerHTML = r.valid ? `<div class="health-ok">✓ ${escapeHtml(r.output)}${r.pending_action !== 'none' ? ` · pending ${escapeHtml(r.pending_action)}` : ''}</div>`
      : `<div class="alert alert-warning"><strong>Invalid:</strong> ${escapeHtml(r.output)}</div>`;
  } catch (e) { $('dm-cfg-result').innerHTML = `<div class="alert alert-warning">${escapeHtml(e.message)}</div>`; }
}
async function dmForceApply() {
  if (!confirm('Re-render every config file and restart dnsmasq?')) return;
  try { const r = await API.post(DMApiStatus().replace('/status', '/apply'), {}); dmNotify(r); page_dnsconfig(); }
  catch (e) { alert(e.message); }
}
async function dmRestart() {
  if (!confirm('Restart dnsmasq now?')) return;
  try { await API.post(DMApiStatus().replace('/status', '/restart'), {}); } catch (e) { alert(e.message); }
  page_dnsconfig();
}
async function dmLogs() {
  try { const r = await API.get(DMApiStatus().replace('/status', '/logs')); openModal('dnsmasq logs', `<pre class="raw-output" style="max-height:500px;overflow:auto">${escapeHtml(r.logs || 'No logs')}</pre>`); }
  catch (e) { alert(e.message); }
}
