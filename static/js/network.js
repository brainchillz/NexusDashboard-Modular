async function page_network() {
  if (currentRole !== 'admin') return adminOnlyPage('Network');
  if (_netCountdown) { clearInterval(_netCountdown); _netCountdown = null; }
  const n = await API.get('/api/network');
  netState = { interfaces: n.interfaces || [], config: n.config || { ethernets: {}, bridges: {} },
               gateway: n.gateway || '', dns: n.dns || [] };
  netIfaceList = netState.interfaces.map(i => i.name);
  const rows = (n.interfaces || []).map(i => `<tr>
      <td><code>${escapeHtml(i.name)}</code></td>
      <td>${escapeHtml(i.type)}</td>
      <td><span class="status-badge ${i.state === 'up' ? 'green' : 'gray'}">${escapeHtml(i.state || '?')}</span></td>
      <td>${(i.addresses || []).map(escapeHtml).join('<br>') || '—'}</td>
      <td><span class="help"><code>${escapeHtml(i.mac || '')}</code></span></td>
      <td><button class="btn btn-sm" onclick="netIfaceModal('${jsArg(i.name)}')">Configure</button></td>
    </tr>`).join('');
  const p = n.pending;
  const banner = netPendingBanner(p);
  $('page-content').innerHTML = `
    <h2>Network</h2>
    ${banner}
    <div class="alert alert-info"><strong>How IP changes work:</strong> the new address is added <em>alongside</em> the
      current one — your session is never dropped. Verify the dashboard on the new address (the link logs you straight in),
      then click <strong>Finalize</strong> to remove the old one. Do nothing and the new address is cleaned up automatically —
      you can't get locked out.</div>
    <h3>Hostname</h3>
    <div style="display:flex;gap:8px;max-width:560px">
      <div class="form-group" style="flex:1"><label>Hostname</label><input id="net-host" class="form-control" value="${escapeHtml(n.hostname || '')}"></div>
      <div class="form-group" style="flex:1"><label>Domain</label><input id="net-domain" class="form-control" value="${escapeHtml(n.domain || '')}"></div>
    </div>
    <div class="toolbar"><button class="btn" onclick="netSaveHostname()">Save Hostname</button></div>
    <p class="help">FQDN <code>${escapeHtml(n.fqdn || '')}</code> · gateway <code>${escapeHtml(n.gateway || '—')}</code>
      · DNS <code>${(n.dns || []).map(escapeHtml).join(', ') || '—'}</code></p>
    <h3 style="margin-top:24px">Interfaces</h3>
    <table class="table">
      <thead><tr><th>Interface</th><th>Type</th><th>State</th><th>Addresses</th><th>MAC</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6">No interfaces</td></tr>'}</tbody>
    </table>
    <div class="toolbar"><button class="btn" onclick="netBridgeModal()">+ Create Bridge</button></div>`;
  // During the finalize phase, heartbeat-confirm from this (new-address) page so
  // the server knows the committed config is reachable; otherwise it rolls back.
  if (p && p.phase === 'finalizing') netHeartbeat(p.token, p.window);
}

// Renders the pending-change banner for whichever phase we're in.
function netPendingBanner(p) {
  if (!p) return '';
  if (p.phase === 'finalizing') {
    return `<div class="alert alert-warning">
      <strong>Finalizing…</strong> keep this tab open while we confirm the new configuration is reachable.
      Auto-rolls back to the previous config in <strong><span id="net-count">${p.window}</span>s</strong> if it can't be confirmed.</div>`;
  }
  // phase === 'dual'
  const where = p.new_addr
    ? `Reach the dashboard at <code>${escapeHtml(p.new_addr)}</code>`
    : `Find the interface's new (DHCP-assigned) address`;
  let actions;
  if (cameFromHandoff) {
    // We're on the new address already — finalizing here is safe.
    actions = `<button class="btn btn-sm" onclick="netFinalize('${jsArg(p.token)}')">Finalize — remove old address</button>`;
  } else {
    const link = p.new_url
      ? `<a class="btn btn-sm" href="${escapeHtml(p.new_url)}">Open ${escapeHtml(p.new_addr || 'new address')} &amp; finalize &rarr;</a>`
      : '';
    actions = `${link}
      <button class="btn btn-sm btn-outline" onclick="netFinalize('${jsArg(p.token)}')"
        title="Only safe if you are NOT connected through the interface you just changed">Finalize from here</button>`;
  }
  return `<div class="alert alert-info">
    <strong>New address is live alongside the old one</strong> — ${escapeHtml(p.desc || '')}.
    Your current connection is unaffected; nothing is removed until you finalize${cameFromHandoff ? '' : `, and the new address auto-clears after ${Math.round(p.window / 60)} min if you walk away`}.
    ${where} to finish.
    <div class="toolbar" style="margin-top:8px">${actions}
      <button class="btn btn-sm btn-outline" onclick="netRevertNow()">Revert now</button></div></div>`;
}

// Poll the confirm endpoint (same-origin on the new address) until it succeeds.
function netHeartbeat(token, window) {
  if (_netCountdown) { clearInterval(_netCountdown); _netCountdown = null; }
  let left = window;
  const tick = async () => {
    const el = $('net-count'); if (el) el.textContent = Math.max(0, left);
    try {
      const r = await API.post('/api/network/confirm', { token });
      if (r && r.success) {
        clearInterval(_netCountdown); _netCountdown = null;
        cameFromHandoff = false;
        alert('Network change finalized and confirmed.');
        page_network();
        return;
      }
    } catch (e) { /* unreachable from here — let the server roll back */ }
    left -= 2;
    if (left <= 0) {
      clearInterval(_netCountdown); _netCountdown = null;
      setTimeout(() => { if (isAuthed) page_network(); }, 2000);
    }
  };
  tick();
  _netCountdown = setInterval(tick, 2000);
}
let netIfaceList = [];
let netState = { interfaces: [], config: { ethernets: {}, bridges: {} }, gateway: '', dns: [] };

// One CIDR address input row (the × removes it). Multiple rows = several IPs on
// one interface; the collector below gathers them into addresses[].
function _netAddrRow(prefix, value = '') {
  return `<div class="${prefix}-addr-row" style="display:flex;gap:6px;margin-bottom:4px">
    <input class="form-control ${prefix}-addr" value="${escapeHtml(value)}" placeholder="192.168.1.50/24">
    <button type="button" class="btn btn-sm btn-outline" title="Remove" onclick="this.parentNode.remove()">×</button>
  </div>`;
}
function netAddAddr(prefix) {
  document.getElementById(prefix + '-addrs').insertAdjacentHTML('beforeend', _netAddrRow(prefix));
}
function _netStaticFields(prefix, opts = {}) {
  const addrs = (opts.addrs && opts.addrs.length) ? opts.addrs : [''];
  return `<div id="${prefix}-static" style="display:${opts.show ? 'block' : 'none'}">
    <div class="form-group"><label>Addresses (CIDR) — add more than one to bind several IPs to this interface</label>
      <div id="${prefix}-addrs">${addrs.map(a => _netAddrRow(prefix, a)).join('')}</div>
      <button type="button" class="btn btn-sm" style="margin-top:4px" onclick="netAddAddr('${prefix}')">+ Add address</button></div>
    <div class="form-group"><label>Gateway (optional — one default for the host)</label><input id="${prefix}-gw" class="form-control" value="${escapeHtml(opts.gw || '')}" placeholder="192.168.1.1"></div>
    <div class="form-group"><label>DNS servers (comma-separated, optional)</label><input id="${prefix}-dns" class="form-control" value="${escapeHtml(opts.dns || '')}" placeholder="1.1.1.1, 8.8.8.8"></div>
  </div>`;
}
function _netStaticBody(prefix) {
  const mode = $(prefix + '-mode').value;
  const body = { mode };
  if (mode === 'static') {
    body.addresses = Array.from(document.querySelectorAll('.' + prefix + '-addr'))
      .map(i => i.value.trim()).filter(Boolean);
    body.gateway = $(prefix + '-gw').value.trim();
    body.nameservers = $(prefix + '-dns').value.split(',').map(s => s.trim()).filter(Boolean);
  }
  return body;
}

function netIfaceModal(name) {
  const live = (netState.interfaces || []).find(i => i.name === name) || {};
  const managed = (netState.config.ethernets || {})[name];
  // Prefer the dashboard-managed config (exact); otherwise reflect the live
  // state so the dialog opens showing the interface's CURRENT settings.
  let mode, addrs = [], gw = '', dns = '';
  if (managed) {
    mode = managed.dhcp4 ? 'dhcp' : 'static';
    addrs = managed.addresses || [];
    gw = managed.gateway || '';
    dns = (managed.nameservers || []).join(', ');
  } else {
    mode = live.dhcp ? 'dhcp' : ((live.addresses || []).length ? 'static' : 'dhcp');
    addrs = live.addresses || [];
    gw = live.gateway || netState.gateway || '';
    dns = (netState.dns || []).filter(d => !d.startsWith('127.')).join(', ');  // skip the resolved stub
  }
  const curLabel = live.dhcp ? 'DHCP' : ((live.addresses || []).length ? 'static' : 'not configured');
  const isStatic = mode === 'static';
  openModal('Configure ' + name, `
    <p class="help">Currently: <strong>${curLabel}</strong>${(live.addresses || []).length ? ' · ' + live.addresses.map(escapeHtml).join(', ') : ''}${managed ? ' · managed by dashboard' : ''}</p>
    <div class="form-group"><label>Mode</label>
      <select id="ni-mode" class="form-control" onchange="document.getElementById('ni-static').style.display=this.value==='static'?'block':'none'">
        <option value="dhcp" ${mode === 'dhcp' ? 'selected' : ''}>DHCP (automatic)</option>
        <option value="static" ${mode === 'static' ? 'selected' : ''}>Static</option>
      </select></div>
    ${_netStaticFields('ni', { addrs, gw, dns, show: isStatic })}
    <div class="alert alert-info">The new address is <strong>added alongside</strong> the current one — this session won't
      drop. You'll then verify it on the new address and click Finalize to remove the old one. (For DHCP, applying can take a
      few seconds while a lease is obtained.)</div>
    <button class="btn" onclick="netSaveIface('${jsArg(name)}')">Apply</button>`);
}
async function netSaveIface(name) {
  const body = { iface: name, ..._netStaticBody('ni') };
  closeModal();
  try {
    const r = await API.post('/api/network/interface', body);
    if (!r.success) { alert(r.error || 'Failed'); return; }
    page_network();
  } catch(e) {
    alert('Change applied. Your current connection should be unaffected (the new address is added alongside the old one).');
    page_network();
  }
}

async function netBridgeModal() {
  const opts = netIfaceList.map(n => `<label style="display:block"><input type="checkbox" class="nb-member" value="${escapeHtml(n)}"> ${escapeHtml(n)}</label>`).join('');
  openModal('Create Bridge', `
    <div class="form-group"><label>Bridge name</label><input id="nb-name" class="form-control" placeholder="br0"></div>
    <div class="form-group"><label>Member interfaces</label>${opts || '<p class="help">No interfaces</p>'}</div>
    <div class="form-group"><label>Mode</label>
      <select id="nb-mode" class="form-control" onchange="document.getElementById('nb-static').style.display=this.value==='static'?'block':'none'">
        <option value="dhcp">DHCP (automatic)</option>
        <option value="static">Static</option>
      </select></div>
    ${_netStaticFields('nb')}
    <div class="alert alert-warning">Enslaving the NIC you're connected through into a bridge <strong>will reset that
      connection</strong> (a bridge can't keep the old address alongside). Reconnect on the bridge's address — the handoff
      link logs you in — then Finalize. If you don't, it auto-reverts to the previous config.</div>
    <button class="btn" onclick="netSaveBridge()">Create Bridge</button>`);
}
async function netSaveBridge() {
  const name = $('nb-name').value.trim();
  const members = Array.from(document.querySelectorAll('.nb-member:checked')).map(c => c.value);
  if (!name) { alert('Bridge name required'); return; }
  const body = { name, interfaces: members, ..._netStaticBody('nb') };
  closeModal();
  try {
    const r = await API.post('/api/network/bridge', body);
    if (!r.success) { alert(r.error || 'Failed'); return; }
    page_network();
  } catch(e) {
    alert('Bridge applied. If your connection dropped, reconnect on the bridge address (it auto-reverts if not finalized).');
  }
}

async function netSaveHostname() {
  const hostname = $('net-host').value.trim(), domain = $('net-domain').value.trim();
  if (!hostname) { alert('Hostname required'); return; }
  try {
    const r = await API.post('/api/network/hostname', { hostname, domain });
    if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; }
    alert('Hostname set to ' + (r.fqdn || hostname) + '. The sidebar updates on next login.');
    page_network();
  } catch(e) { alert(e.message); }
}

async function netFinalize(token) {
  try {
    const r = await API.post('/api/network/finalize', { token });
    if (r && !r.success) { alert(r.error || 'Finalize failed'); return; }
    // Now in the finalize phase: page_network() will show the heartbeat banner
    // and confirm from here. If finalizing cut THIS session off, the heartbeat
    // can't reach the server and the change rolls back automatically.
    page_network();
  } catch(e) {
    alert('Finalize sent. If this page can still reach the dashboard it confirms automatically; otherwise the change rolls back shortly.');
  }
}
async function netRevertNow() {
  if (!confirm('Revert to the previous network configuration now?')) return;
  try { await API.post('/api/network/revert', {}); } catch(e) {}
  cameFromHandoff = false;
  page_network();
}

