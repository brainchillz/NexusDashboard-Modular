// Nexus Containers pages — ported from the LXD-Console frontend.
// Shared scaffolding (API, modal, router, theme, auth) lives in core.js.
let serverInfo = {};
function statusBadge(status) {
  const s = (status || '').toLowerCase();
  const cls = s === 'running' ? 'green' : (s === 'stopped' ? 'gray' : (s === 'frozen' ? 'yellow' : 'gray'));
  return `<span class="status-badge ${cls}">${escapeHtml(status || '?')}</span>`;
}
function typeBadge(t) {
  return `<span class="badge-type">${t === 'virtual-machine' ? 'VM' : 'CT'}</span>`;
}

// ─── Modal ──────────────────────────────────────────────
function instancesTable(insts) {
  if (!insts.length) return `<p class="help">No instances yet. ${currentRole==='admin' ? '<a href="#" onclick="openCreate();return false">Create one</a>.' : ''}</p>`;
  const rows = insts.map(i => {
    const running = (i.status || '').toLowerCase() === 'running';
    const ips = (i.ipv4 || []).join(', ') || '—';
    const nm = jsArg(i.name);
    const isVM = i.type === 'virtual-machine';
    let actions = '';
    if (currentRole === 'admin') {
      actions = running
        ? `<button class="btn btn-sm" onclick="openConsole('${nm}')">Console</button>
           ${isVM ? `<button class="btn btn-sm" onclick="openGraphical('${nm}')">Graphical</button>` : ''}
           <button class="btn btn-sm" onclick="instAction('${nm}','restart',true)">Restart</button>
           <button class="btn btn-sm btn-warning" onclick="instAction('${nm}','stop',true)">Stop</button>`
        : `<button class="btn btn-sm btn-success" onclick="instAction('${nm}','start',false)">Start</button>`;
      actions += ` <button class="btn btn-sm" onclick="changeNetwork('${nm}')">Network</button>`;
      actions += ` <button class="btn btn-sm btn-danger" onclick="deleteInstance('${nm}',${running})">Delete</button>`;
    }
    return `<tr>
      <td><a href="#" onclick="openInstance('${nm}');return false" class="mono">${escapeHtml(i.name)}</a> ${typeBadge(i.type)}</td>
      <td>${statusBadge(i.status)}</td>
      <td>${escapeHtml((i.os||'') + (i.release ? ' ' + i.release : '')) || '—'}</td>
      <td class="mono">${escapeHtml(ips)}</td>
      <td>${running ? fmtBytesIEC(i.memory) : '—'}</td>
      <td style="white-space:nowrap">${actions}</td>
    </tr>`;
  }).join('');
  return `<table class="table"><thead><tr>
    <th>Name</th><th>Status</th><th>OS</th><th>IPv4</th><th>Memory</th><th>Actions</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

async function page_instances() {
  const insts = await API.get('/api/instances');
  const btns = currentRole === 'admin'
    ? `<div style="display:flex;gap:8px"><button class="btn" onclick="openImport()">Import</button><button class="btn" onclick="openCreate()">Create Instance</button></div>` : '';
  $('page-content').innerHTML = `
    <div class="page-header"><h2>Instances</h2>${btns}</div>
    ${instancesTable(insts)}`;
}
function openImport() {
  openModal('Import Instance', `
    <p class="help">Upload a backup archive previously created with Export (a <span class="mono">.tar.gz</span>).</p>
    <div class="form-group"><label>Backup file</label><input id="im-file" type="file" class="form-control" accept=".tar.gz,.tgz,application/gzip"></div>
    <div class="form-group"><label>New name (optional)</label><input id="im-name" class="form-control mono" placeholder="leave blank to keep original"></div>
    <div id="im-error" class="error" style="display:none"></div>
    <button class="btn" onclick="submitImport()">Import</button>`);
}
async function submitImport() {
  const errEl = $('im-error'); errEl.style.display = 'none';
  const f = $('im-file').files[0];
  if (!f) { errEl.textContent = 'Choose a file'; errEl.style.display = 'block'; return; }
  const name = ($('im-name').value||'').trim();
  const btn = event.target; btn.disabled = true; btn.textContent = 'Uploading…';
  try {
    const r = await fetch(`/api/instances/import${name?('?name='+encodeURIComponent(name)):''}`, { method: 'POST', body: f });
    const j = await r.json();
    if (!r.ok || !j.success) throw new Error(j.error || 'Import failed');
    closeModal(); refreshCurrent();
  } catch (e) { errEl.textContent = e.message; errEl.style.display = 'block'; btn.disabled = false; btn.textContent = 'Import'; }
}

async function instAction(name, action, force) {
  try {
    await API.put(`/api/instances/${encodeURIComponent(name)}/state`, { action, force: !!force });
    refreshCurrent();
  } catch (e) { alert(e.message); }
}

async function deleteInstance(name, running) {
  if (!confirm(`Delete instance "${name}"?${running ? ' It is running and will be force-stopped.' : ''}`)) return;
  try {
    await API.delete(`/api/instances/${encodeURIComponent(name)}${running ? '?force=1' : ''}`);
    refreshCurrent();
  } catch (e) { alert(e.message); }
}

function refreshCurrent() {
  const active = document.querySelector('.nav-list a.active');
  renderPage(active ? active.dataset.page : 'instances');
}

async function openInstance(name) {
  let d;
  try { d = await API.get(`/api/instances/${encodeURIComponent(name)}`); } catch (e) { alert(e.message); return; }
  const admin = currentRole === 'admin';
  const cfg = d.config || {};
  const cfgRows = Object.keys(cfg).sort().filter(k => !k.startsWith('volatile.'))
    .map(k => `<tr><td class="mono">${escapeHtml(k)}</td><td class="mono">${escapeHtml(String(cfg[k]))}</td></tr>`).join('')
    || '<tr><td colspan="2" class="help">none</td></tr>';
  const devRows = Object.entries(d.devices || {})
    .map(([n, dev]) => `<tr><td class="mono">${escapeHtml(n)}</td><td class="mono">${escapeHtml(JSON.stringify(dev))}</td></tr>`).join('')
    || '<tr><td colspan="2" class="help">inherited from profile</td></tr>';
  const eth0 = (d.devices && d.devices.eth0) || (d.expanded_devices && d.expanded_devices.eth0) || {};
  const curNet = eth0.network || eth0.parent || '(profile default)';
  const netLine = `<p><strong>Network (eth0):</strong> <span class="mono">${escapeHtml(curNet)}</span>
    ${admin ? ` <button class="btn btn-sm" onclick="changeNetwork('${jsArg(name)}')">Change network</button>` : ''}</p>`;
  const limits = `${cfg['limits.cpu']||'—'} CPU · ${cfg['limits.memory']||'—'} mem`;
  const vmRunning = d.type === 'virtual-machine' && (d.status || '').toLowerCase() === 'running';
  const actions = admin ? `<div style="margin:8px 0;display:flex;gap:6px;flex-wrap:wrap">
      ${vmRunning ? `<button class="btn btn-sm" onclick="openGraphical('${jsArg(name)}')">Graphical console</button>` : ''}
      <button class="btn btn-sm" onclick="editLimits('${jsArg(name)}')">Edit limits</button>
      <button class="btn btn-sm" onclick="openProxyAdd('${jsArg(name)}')">Add port forward</button>
      <button class="btn btn-sm" onclick="renameInstance('${jsArg(name)}')">Rename</button>
      <button class="btn btn-sm" onclick="copyInstance('${jsArg(name)}')">Copy</button>
      <button class="btn btn-sm" onclick="exportInstance('${jsArg(name)}')">Export</button>
    </div>` : '';
  const snapRows = (d.snapshots||[]).map(s => `<tr><td class="mono">${escapeHtml(s.name)}</td>
      <td>${escapeHtml((s.created_at||'').replace('T',' ').slice(0,16))}</td>
      <td>${admin?`<button class="btn btn-sm" onclick="restoreSnap('${jsArg(name)}','${jsArg(s.name)}')">Restore</button>
        <button class="btn btn-sm btn-danger" onclick="deleteSnap('${jsArg(name)}','${jsArg(s.name)}')">Delete</button>`:''}</td></tr>`).join('')
    || '<tr><td colspan="3" class="help">none</td></tr>';
  const bakRows = (d.backups||[]).map(b => `<tr><td class="mono">${escapeHtml(b.name)}</td>
      <td><a class="btn btn-sm" href="/api/instances/${encodeURIComponent(name)}/backups/${encodeURIComponent(b.name)}/download">Download</a>
      ${admin?`<button class="btn btn-sm btn-danger" onclick="deleteBackup('${jsArg(name)}','${jsArg(b.name)}')">Delete</button>`:''}</td></tr>`).join('')
    || '<tr><td colspan="2" class="help">none — use Export to create one</td></tr>';
  openModal(`${name} ${d.type === 'virtual-machine' ? '(VM)' : '(container)'}`, `
    <p>${statusBadge(d.status)} &nbsp; ${escapeHtml((d.os||'') + ' ' + (d.release||''))} &nbsp; <span class="help">${escapeHtml(limits)}</span></p>
    <p class="help">Profiles: ${escapeHtml((d.profiles||[]).join(', '))} · Created: ${escapeHtml((d.created_at||'').slice(0,10))}</p>
    <p class="mono">IPv4: ${escapeHtml((d.ipv4||[]).join(', ')||'—')}<br>IPv6: ${escapeHtml((d.ipv6||[]).join(', ')||'—')}</p>
    ${netLine}
    ${actions}
    <h4>Snapshots (${(d.snapshots||[]).length}) ${admin?`<button class="btn btn-sm" onclick="createSnap('${jsArg(name)}')">+ Create</button>`:''}</h4>
    <table class="table"><thead><tr><th>Name</th><th>Created</th><th></th></tr></thead><tbody>${snapRows}</tbody></table>
    <h4>Exports / backups</h4>
    <table class="table"><tbody>${bakRows}</tbody></table>
    <h4>Configuration</h4>
    <table class="table"><tbody>${cfgRows}</tbody></table>
    <h4>Devices</h4>
    <table class="table"><tbody>${devRows}</tbody></table>
  `, { wide: true });
}

// snapshots
function createSnap(name) {
  openModal(`Snapshot ${name}`, `
    <div class="form-group"><label>Snapshot name</label><input id="sn-name" class="form-control mono" value="snap-${Date.now().toString().slice(-6)}"></div>
    <div class="form-group"><label><input type="checkbox" id="sn-stateful"> Stateful (save running memory — VMs / CRIU containers)</label></div>
    <button class="btn" onclick="submitSnap('${jsArg(name)}')">Create</button>`);
}
async function submitSnap(name) {
  try { await API.post(`/api/instances/${encodeURIComponent(name)}/snapshots`, { name: $('sn-name').value.trim(), stateful: $('sn-stateful').checked }); closeModal(); openInstance(name); }
  catch (e) { alert(e.message); }
}
async function restoreSnap(name, snap) {
  if (!confirm(`Restore ${name} to snapshot "${snap}"? Current state is discarded.`)) return;
  try { await API.post(`/api/instances/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snap)}/restore`, {}); alert('Restored.'); openInstance(name); }
  catch (e) { alert(e.message); }
}
async function deleteSnap(name, snap) {
  if (!confirm(`Delete snapshot "${snap}"?`)) return;
  try { await API.delete(`/api/instances/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snap)}`); openInstance(name); }
  catch (e) { alert(e.message); }
}
// limits
function editLimits(name) {
  API.get(`/api/instances/${encodeURIComponent(name)}`).then(d => {
    const c = d.config || {};
    openModal(`Edit limits — ${name}`, `
      <div class="form-group"><label>CPU limit</label><input id="el-cpu" class="form-control mono" value="${escapeHtml(c['limits.cpu']||'')}" placeholder="e.g. 2 or 0-3"></div>
      <div class="form-group"><label>Memory limit</label><input id="el-mem" class="form-control mono" value="${escapeHtml(c['limits.memory']||'')}" placeholder="e.g. 2GiB"></div>
      <div class="form-group"><label><input type="checkbox" id="el-auto" ${c['boot.autostart']==='true'?'checked':''}> Start on boot</label></div>
      <p class="help">Leave a field blank to leave it unchanged.</p>
      <button class="btn" onclick="submitLimits('${jsArg(name)}')">Apply</button>`);
  });
}
async function submitLimits(name) {
  const config = {};
  if ($('el-cpu').value.trim()) config['limits.cpu'] = $('el-cpu').value.trim();
  if ($('el-mem').value.trim()) config['limits.memory'] = $('el-mem').value.trim();
  config['boot.autostart'] = $('el-auto').checked ? 'true' : 'false';
  try { await API.patch(`/api/instances/${encodeURIComponent(name)}/config`, { config }); closeModal(); openInstance(name); }
  catch (e) { alert(e.message); }
}
// rename / copy
function renameInstance(name) {
  openModal(`Rename ${name}`, `
    <p class="help">The instance must be stopped to rename it.</p>
    <div class="form-group"><label>New name</label><input id="rn-name" class="form-control mono" value="${escapeHtml(name)}"></div>
    <button class="btn" onclick="submitRename('${jsArg(name)}')">Rename</button>`);
}
async function submitRename(name) {
  try { await API.post(`/api/instances/${encodeURIComponent(name)}/rename`, { new_name: $('rn-name').value.trim() }); closeModal(); refreshCurrent(); }
  catch (e) { alert(e.message); }
}
function copyInstance(name) {
  openModal(`Copy ${name}`, `
    <div class="form-group"><label>New instance name</label><input id="cp-name" class="form-control mono" value="${escapeHtml(name)}-copy"></div>
    <button class="btn" onclick="submitCopy('${jsArg(name)}')">Copy</button>`);
}
async function submitCopy(name) {
  const btn = event.target; btn.disabled = true; btn.textContent = 'Copying…';
  try { await API.post(`/api/instances/${encodeURIComponent(name)}/copy`, { new_name: $('cp-name').value.trim() }); closeModal(); refreshCurrent(); }
  catch (e) { alert(e.message); btn.disabled = false; btn.textContent = 'Copy'; }
}
// export
async function exportInstance(name) {
  if (!confirm(`Create an export archive of "${name}"? This can take a while for large instances.`)) return;
  try {
    const r = await API.post(`/api/instances/${encodeURIComponent(name)}/export`, { instance_only: true });
    if (confirm('Export created. Download it now?')) window.location = r.download;
    openInstance(name);
  } catch (e) { alert(e.message); }
}
async function deleteBackup(name, bak) {
  if (!confirm(`Delete export "${bak}"?`)) return;
  try { await API.delete(`/api/instances/${encodeURIComponent(name)}/backups/${encodeURIComponent(bak)}`); openInstance(name); }
  catch (e) { alert(e.message); }
}

// ─── Create instance ────────────────────────────────────
let _createImages = [];
async function openCreate(prefill) {
  prefill = prefill || {};
  let pools = [], profiles = [], nets = [];
  try { pools = await API.get('/api/storage-pools'); } catch (e) {}
  try { profiles = await API.get('/api/profiles'); } catch (e) {}
  try { nets = await API.get('/api/networks'); } catch (e) {}
  const poolOpts = pools.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)} (${escapeHtml(p.driver)})</option>`).join('');
  const profOpts = profiles.map(p => `<option value="${escapeHtml(p.name)}" ${p.name==='default'?'selected':''}>${escapeHtml(p.name)}</option>`).join('');
  const netOpts = nets.filter(n => n.managed || n.type === 'bridge')
    .map(n => `<option value="${escapeHtml(n.name)}">${escapeHtml(n.name)} (${escapeHtml(n.type)}${n.managed?'':' · unmanaged'})</option>`).join('');
  openModal('Create Instance', `
    <div class="form-group"><label>Name</label><input id="ci-name" class="form-control mono" value="${escapeHtml(prefill.name||'')}" placeholder="my-instance"></div>
    <div class="form-group"><label>Type</label>
      <select id="ci-type" class="form-control" onchange="loadCreateImages()">
        <option value="container" ${prefill.type==='virtual-machine'?'':'selected'}>Container</option>
        <option value="virtual-machine" ${prefill.type==='virtual-machine'?'selected':''}>Virtual machine</option>
      </select></div>
    <div class="form-group"><label>Image</label>
      <input id="ci-imgfilter" class="form-control" placeholder="filter images…" oninput="renderCreateImages()">
      <select id="ci-image" class="form-control" style="margin-top:6px"></select>
    </div>
    <div class="form-group"><label>Storage pool</label><select id="ci-pool" class="form-control"><option value="">(profile default)</option>${poolOpts}</select></div>
    <div class="form-group"><label>Network</label><select id="ci-network" class="form-control"><option value="">(profile default)</option>${netOpts}</select></div>
    <div class="form-group"><label>Profile</label><select id="ci-profile" class="form-control">${profOpts}</select></div>
    <div class="form-group"><label><input type="checkbox" id="ci-start" checked> Start after creation</label></div>
    <div id="ci-error" class="error" style="display:none"></div>
    <button class="btn" onclick="submitCreate()">Create</button>
  `, { wide: true });
  if (prefill.alias) window._prefillAlias = prefill.alias;
  await loadCreateImages();
}

async function loadCreateImages() {
  const type = $('ci-type').value;
  const arch = serverInfo.arch || 'amd64';
  const sel = $('ci-image');
  sel.innerHTML = '<option>loading…</option>';
  try {
    const r = await API.get(`/api/images/remote?arch=${encodeURIComponent(arch)}&type=${encodeURIComponent(type)}`);
    _createImages = r.images || [];
  } catch (e) { _createImages = []; }
  renderCreateImages();
}
function renderCreateImages() {
  const f = ($('ci-imgfilter').value || '').toLowerCase();
  const sel = $('ci-image');
  const list = _createImages.filter(im => !f || (im.os + ' ' + im.release + ' ' + im.alias + ' ' + im.variant).toLowerCase().includes(f));
  sel.innerHTML = list.map(im =>
    `<option value="${escapeHtml(im.alias)}">${escapeHtml(im.os)} ${escapeHtml(im.release)} · ${escapeHtml(im.variant)} (${escapeHtml(im.alias)})</option>`
  ).join('') || '<option value="">no matching images</option>';
  if (window._prefillAlias) {
    const opt = Array.from(sel.options).find(o => o.value === window._prefillAlias);
    if (opt) sel.value = window._prefillAlias;
    window._prefillAlias = null;
  }
}
async function submitCreate() {
  const errEl = $('ci-error'); errEl.style.display = 'none';
  const body = {
    name: ($('ci-name').value || '').trim(),
    type: $('ci-type').value,
    alias: $('ci-image').value,
    pool: $('ci-pool').value,
    network: $('ci-network').value,
    profiles: [$('ci-profile').value],
    start: $('ci-start').checked,
  };
  if (!body.name) { errEl.textContent = 'Name is required'; errEl.style.display = 'block'; return; }
  if (!body.alias) { errEl.textContent = 'Select an image'; errEl.style.display = 'block'; return; }
  const btn = event.target; btn.disabled = true; btn.textContent = 'Creating… (downloading image)';
  try {
    await API.post('/api/instances', body);
    closeModal();
    refreshCurrent();
  } catch (e) {
    errEl.textContent = e.message; errEl.style.display = 'block';
    btn.disabled = false; btn.textContent = 'Create';
  }
}

// ═══════════════════════════════════════════════════════
//  Console (xterm over websocket)
// ═══════════════════════════════════════════════════════
let _term = null, _termWs = null, _fit = null;
function openConsole(name, mode) {
  closeConsole();
  mode = mode === 'serial' ? 'serial' : 'shell';
  const other = mode === 'serial' ? 'shell' : 'serial';
  const note = mode === 'serial'
    ? 'Serial console — this is the guest OS <span class="mono">login:</span> prompt. Cloud images ship <strong>no default password</strong>. Prefer the root shell, or set a password with <span class="mono">passwd</span> there first.'
    : 'Direct <strong>root shell</strong> (via the guest agent on VMs) — no login needed. Want serial/SSH login? Run <span class="mono">passwd</span> right here to set a password.';
  openModal(`Console — ${name} (${mode})`, `
    <p class="help">${note}</p>
    <p><button class="btn btn-sm" onclick="openConsole('${jsArg(name)}','${other}')">Switch to ${other} console</button></p>
    <div id="console-term" class="console-term"></div>
    <div id="console-status" class="help" style="margin-top:6px">connecting…</div>
  `, { wide: true });
  _onModalClose = closeConsole;
  setTimeout(() => startConsole(name, mode), 60);
}
function closeConsole() {
  try { if (_termWs) _termWs.close(); } catch (e) {}
  try { if (_term) _term.dispose(); } catch (e) {}
  _termWs = null; _term = null; _fit = null;
}
// Graphical (SPICE/VGA) console for VMs — a separate ES-module page (spice-html5),
// so it opens in its own window rather than inline in the SPA.
function openGraphical(name) {
  window.open('/console/vga/' + encodeURIComponent(name),
    'vga_' + name.replace(/[^A-Za-z0-9_]/g, '_'),
    'width=1120,height=840,resizable=yes,scrollbars=yes');
}
function startConsole(name, mode) {
  const el = $('console-term');
  if (!el || !window.Terminal) { $('console-status').textContent = 'terminal unavailable'; return; }
  _term = new Terminal({ cursorBlink: true, fontSize: 13, theme: { background: '#000000' } });
  _fit = new FitAddon.FitAddon();
  _term.loadAddon(_fit);
  _term.open(el);
  try { _fit.fit(); } catch (e) {}
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const q = mode === 'serial' ? '?mode=serial' : '';
  const ws = new WebSocket(`${proto}//${location.host}/ws/console/${encodeURIComponent(name)}${q}`);
  ws.binaryType = 'arraybuffer';
  _termWs = ws;
  ws.onopen = () => {
    $('console-status').textContent = 'connected';
    sendResize();
    _term.focus();
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      try { const o = JSON.parse(ev.data); if (o && o.type === 'error') { _term.write('\r\n\x1b[31m' + o.error + '\x1b[0m\r\n'); return; } } catch (e) {}
      _term.write(ev.data);
    } else {
      _term.write(new Uint8Array(ev.data));
    }
  };
  ws.onclose = () => { $('console-status').textContent = 'disconnected'; };
  ws.onerror = () => { $('console-status').textContent = 'connection error'; };
  _term.onData(d => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'stdin', data: d })); });
  _term.onResize(() => sendResize());
  window.addEventListener('resize', consoleResizeHandler);
  function sendResize() {
    try { _fit.fit(); } catch (e) {}
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'resize', width: _term.cols, height: _term.rows }));
  }
  window._consoleSendResize = sendResize;
}
function consoleResizeHandler() { if (window._consoleSendResize) window._consoleSendResize(); }

// ═══════════════════════════════════════════════════════
//  Images
// ═══════════════════════════════════════════════════════
let _remoteImages = [];
async function page_images() {
  let remotes = { remotes: [], host_arch: 'amd64' };
  try { remotes = await API.get('/api/images/remotes'); } catch (e) {}
  const remoteOpts = (remotes.remotes || []).map(r => `<option value="${escapeHtml(r.url)}">${escapeHtml(r.name)} — ${escapeHtml(r.url)}</option>`).join('');
  let local = [];
  try { local = await API.get('/api/images'); } catch (e) {}
  const localRows = local.map(im => `<tr>
    <td class="mono">${escapeHtml((im.aliases||[]).join(', ') || im.fingerprint)}</td>
    <td>${typeBadge(im.type)}</td>
    <td>${escapeHtml(im.os)} ${escapeHtml(im.release)}</td>
    <td>${escapeHtml(im.architecture)}</td>
    <td>${fmtBytesIEC(im.size)}</td>
    <td>${currentRole==='admin'?`<button class="btn btn-sm btn-danger" onclick="deleteImage('${jsArg(im.fingerprint_full)}')">Delete</button>`:''}</td>
    </tr>`).join('') || '<tr><td colspan="6" class="help">none cached</td></tr>';
  $('page-content').innerHTML = `
    <h2>Images</h2>
    <div class="img-filter">
      <div class="form-group"><label>Remote server</label><select id="im-server" class="form-control" onchange="loadRemoteImages()">${remoteOpts}</select></div>
      <div class="form-group"><label>Arch</label><select id="im-arch" class="form-control" onchange="renderRemoteImages()">
        <option value="${escapeHtml(remotes.host_arch)}">${escapeHtml(remotes.host_arch)} (host)</option>
        <option value="amd64">amd64</option><option value="arm64">arm64</option><option value="">all</option></select></div>
      <div class="form-group"><label>Type</label><select id="im-type" class="form-control" onchange="renderRemoteImages()">
        <option value="container">Container</option><option value="virtual-machine">VM</option><option value="">any</option></select></div>
      <div class="form-group"><label>Filter</label><input id="im-filter" class="form-control" oninput="renderRemoteImages()" placeholder="ubuntu, alpine…"></div>
    </div>
    <div id="remote-images"><p class="help">Loading remote images…</p></div>
    <h3 style="margin-top:24px">Local (cached) images</h3>
    <table class="table"><thead><tr><th>Alias</th><th>Type</th><th>OS</th><th>Arch</th><th>Size</th><th></th></tr></thead><tbody>${localRows}</tbody></table>`;
  await loadRemoteImages();
}
async function deleteImage(fp) {
  if (!fp) { alert('Missing fingerprint'); return; }
  if (!confirm('Delete this cached image?')) return;
  try { await API.delete(`/api/images/${encodeURIComponent(fp)}`); page_images(); }
  catch (e) { alert(e.message); }
}
async function fetchImageLocal(server, alias) {
  if (!confirm(`Download image "${alias}" to the local cache now?`)) return;
  try { await API.post('/api/images/copy', { server, alias }); alert('Image cached.'); page_images(); }
  catch (e) { alert(e.message); }
}
async function loadRemoteImages() {
  const server = $('im-server').value;
  $('remote-images').innerHTML = '<p class="help">Loading remote images…</p>';
  try {
    const r = await API.get(`/api/images/remote?server=${encodeURIComponent(server)}`);
    _remoteImages = r.images || [];
  } catch (e) { $('remote-images').innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; return; }
  renderRemoteImages();
}
function renderRemoteImages() {
  const arch = $('im-arch').value, type = $('im-type').value, f = ($('im-filter').value || '').toLowerCase();
  let list = _remoteImages;
  if (arch) list = list.filter(i => i.arch === arch);
  if (type) list = list.filter(i => (i.types || []).includes(type));
  if (f) list = list.filter(i => (i.os + ' ' + i.release + ' ' + i.alias + ' ' + i.variant).toLowerCase().includes(f));
  const server = $('im-server').value;
  const rows = list.slice(0, 400).map(i => {
    const launch = currentRole === 'admin'
      ? `<button class="btn btn-sm" onclick='openCreate(${JSON.stringify({name:'', type:(i.types.includes('container')?'container':'virtual-machine'), alias:i.alias})})'>Launch</button>
         <button class="btn btn-sm" onclick="fetchImageLocal('${jsArg(server)}','${jsArg(i.alias)}')">Fetch</button>` : '';
    return `<tr><td class="mono">${escapeHtml(i.alias)}</td><td>${escapeHtml(i.os)} ${escapeHtml(i.release)}</td>
      <td>${escapeHtml(i.variant)}</td><td>${escapeHtml(i.arch)}</td>
      <td>${(i.types||[]).map(t=>`<span class="badge-type">${t==='virtual-machine'?'VM':'CT'}</span>`).join(' ')}</td>
      <td style="white-space:nowrap">${launch}</td></tr>`;
  }).join('');
  $('remote-images').innerHTML = `<p class="help">${list.length} images</p>
    <table class="table"><thead><tr><th>Alias</th><th>OS</th><th>Variant</th><th>Arch</th><th>Supports</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="6" class="help">no matches</td></tr>'}</tbody></table>`;
}

// ═══════════════════════════════════════════════════════
//  Networks
// ═══════════════════════════════════════════════════════
let _hostIfaces = [];
async function page_ctnetworks() {
  const nets = await API.get('/api/networks');
  try { _hostIfaces = await API.get('/api/host/interfaces'); } catch (e) { _hostIfaces = []; }
  const rows = nets.map(n => {
    const uplink = n.external_interfaces ? ` · uplink ${escapeHtml(n.external_interfaces)}`
                 : (n.parent ? ` · parent ${escapeHtml(n.parent)}` : '');
    let btns = '';
    if (currentRole === 'admin' && n.managed) {
      if (n.type === 'bridge') btns += `<button class="btn btn-sm" onclick="editNetwork('${jsArg(n.name)}')">Edit</button> `;
      btns += `<button class="btn btn-sm btn-danger" onclick="deleteNetwork('${jsArg(n.name)}',${n.used_by})">Delete</button>`;
    }
    return `<tr>
      <td class="mono">${escapeHtml(n.name)}</td>
      <td>${escapeHtml(n.type)}${uplink}</td>
      <td>${n.managed ? '<span class="status-badge green">managed</span>' : '<span class="status-badge gray">unmanaged</span>'}</td>
      <td class="mono">${escapeHtml(n.ipv4_address||'—')}</td>
      <td>${n.used_by}</td>
      <td style="white-space:nowrap">${btns}</td></tr>`;
  }).join('');
  // Show physical NICs and whether they can be enslaved into a no-IP LAN bridge.
  const ifRows = _hostIfaces.filter(i => i.lxd_type === 'physical' || i.lxd_type === 'bridge').map(i => {
    let note = '';
    if (i.master) note = `<span class="status-badge gray">enslaved to ${escapeHtml(i.master)}</span>`;
    else if (i.is_default_route || i.has_ip) note = '<span class="status-badge yellow">carries host IP — use macvlan, or free it first</span>';
    else if (i.bridgeable) note = '<span class="status-badge green">free — bridgeable</span>';
    return `<tr><td class="mono">${escapeHtml(i.name)}</td><td>${escapeHtml(i.lxd_type||'')}</td>
      <td>${i.carrier ? 'up' : 'no-carrier'}</td><td class="mono">${escapeHtml((i.addresses||[]).join(', ')||'—')}</td>
      <td>${note}</td></tr>`;
  }).join('');
  const createBtn = currentRole === 'admin' ? `<button class="btn" onclick="openNetworkCreate()">Create Network</button>` : '';
  $('page-content').innerHTML = `
    <div class="page-header"><h2>Networks</h2>${createBtn}</div>
    <table class="table"><thead><tr><th>Name</th><th>Type</th><th>State</th><th>IPv4</th><th>Used by</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    <h3 style="margin-top:24px">Host interfaces</h3>
    <p class="help">A no-IP <strong>LAN bridge</strong> can only enslave a NIC with no host IP. To put containers directly on your LAN, either enslave a free NIC or use a <strong>macvlan</strong> (works on any NIC, but the host can't talk to those containers over it).</p>
    <table class="table"><thead><tr><th>Interface</th><th>Type</th><th>Carrier</th><th>Address</th><th></th></tr></thead>
      <tbody>${ifRows || '<tr><td colspan="5" class="help">none</td></tr>'}</tbody></table>`;
}

function openNetworkCreate() {
  const freeNics = _hostIfaces.filter(i => i.bridgeable);
  const allNics = _hostIfaces.filter(i => i.lxd_type === 'physical');
  const freeOpts = freeNics.map(i => `<option value="${escapeHtml(i.name)}">${escapeHtml(i.name)}</option>`).join('');
  const allOpts = allNics.map(i => `<option value="${escapeHtml(i.name)}">${escapeHtml(i.name)}${i.has_ip?' (has host IP)':''}</option>`).join('');
  openModal('Create Network', `
    <div class="form-group"><label>Name</label><input id="nn-name" class="form-control mono" placeholder="lanbr0" maxlength="15"></div>
    <div class="form-group"><label>Kind</label>
      <select id="nn-kind" class="form-control" onchange="netKindFields()">
        <option value="nat">NAT bridge (private subnet + NAT, like lxdbr0)</option>
        <option value="bridge-lan">LAN bridge (enslave a free NIC → containers get LAN DHCP)</option>
        <option value="macvlan">macvlan (attach to a NIC → containers get LAN DHCP)</option>
      </select></div>
    <div id="nn-fields"></div>
    <div id="nn-error" class="error" style="display:none"></div>
    <button class="btn" onclick="submitNetwork()">Create</button>`);
  window._nnFree = freeOpts; window._nnAll = allOpts;
  netKindFields();
}
function netKindFields() {
  const kind = $('nn-kind').value;
  let html = '';
  if (kind === 'nat') {
    html = `<div class="form-group"><label>IPv4 subnet</label>
        <input id="nn-ipv4" class="form-control mono" value="auto" placeholder="auto or 10.10.0.1/24">
        <p class="help">"auto" lets the daemon pick a free private subnet.</p></div>
      <div class="form-group"><label><input type="checkbox" id="nn-nat" checked> Enable NAT (outbound internet)</label></div>`;
  } else if (kind === 'bridge-lan') {
    html = window._nnFree
      ? `<div class="form-group"><label>Uplink NIC (must have no host IP)</label>
           <select id="nn-uplink" class="form-control">${window._nnFree}</select>
           <p class="help">The NIC is added to a no-IP L2 bridge; attached containers get IPs from your real LAN DHCP.</p></div>`
      : `<div class="alert alert-warning">No free NIC available. Every physical NIC currently has a host IP. Free one (remove its IP in your host network config) or use macvlan.</div>`;
  } else {
    html = `<div class="form-group"><label>Parent NIC</label>
        <select id="nn-uplink" class="form-control">${window._nnAll}</select>
        <p class="help">Containers get their own MAC on this NIC and pull DHCP from your LAN. Note: the host cannot talk to these containers over this NIC.</p></div>`;
  }
  $('nn-fields').innerHTML = html;
}
async function submitNetwork() {
  const errEl = $('nn-error'); errEl.style.display = 'none';
  const kind = $('nn-kind').value;
  const body = { name: ($('nn-name').value || '').trim(), kind };
  if (kind === 'nat') { body.ipv4 = ($('nn-ipv4').value || 'auto').trim(); body.nat = $('nn-nat').checked; }
  else {
    const up = $('nn-uplink');
    if (!up) { errEl.textContent = 'No interface available'; errEl.style.display = 'block'; return; }
    body.uplink = up.value;
  }
  if (!body.name) { errEl.textContent = 'Name is required'; errEl.style.display = 'block'; return; }
  try { await API.post('/api/networks', body); closeModal(); page_ctnetworks(); }
  catch (e) { errEl.textContent = e.message; errEl.style.display = 'block'; }
}
async function deleteNetwork(name, usedBy) {
  if (usedBy > 0) { alert(`"${name}" is in use by ${usedBy} instance(s)/profile(s). Detach them first.`); return; }
  if (!confirm(`Delete network "${name}"?`)) return;
  try { await API.delete(`/api/networks/${encodeURIComponent(name)}`); page_ctnetworks(); }
  catch (e) { alert(e.message); }
}

// ─── Edit a managed bridge: assign / remove enslaved interfaces ─────────
function _extIfaces(n) { return (n.external_interfaces || '').split(',').map(s => s.trim()).filter(Boolean); }
async function _netByName(name) { return (await API.get('/api/networks')).find(x => x.name === name); }

async function editNetwork(name) {
  const n = await _netByName(name);
  if (!n) return;
  try { _hostIfaces = await API.get('/api/host/interfaces'); } catch (e) { _hostIfaces = []; }
  const current = _extIfaces(n);
  const curRows = current.length
    ? current.map(i => `<tr><td class="mono">${escapeHtml(i)}</td>
        <td><button class="btn btn-sm btn-danger" onclick="removeBridgeIface('${jsArg(name)}','${jsArg(i)}')">Remove</button></td></tr>`).join('')
    : '<tr><td colspan="2" class="help">No enslaved interface. A no-IP bridge needs one enslaved NIC to reach the LAN.</td></tr>';
  const free = _hostIfaces.filter(i => i.bridgeable && current.indexOf(i.name) === -1);
  const addOpts = free.map(i => `<option value="${escapeHtml(i.name)}">${escapeHtml(i.name)}</option>`).join('');
  openModal(`Edit network — ${name}`, `
    <p class="help">${escapeHtml(n.type)} · ${n.managed ? 'managed' : 'unmanaged'}</p>
    <h4>Settings</h4>
    <div class="form-group"><label>IPv4 address (CIDR, "auto", or "none")</label>
      <input id="en-ipv4" class="form-control mono" value="${escapeHtml(n.ipv4_address||'')}"></div>
    <div class="form-group"><label><input type="checkbox" id="en-nat" ${n.ipv4_nat==='true'?'checked':''}> IPv4 NAT (outbound internet)</label></div>
    <div class="form-group"><label><input type="checkbox" id="en-dhcp" ${n.ipv4_dhcp!=='false'?'checked':''}> IPv4 DHCP server</label></div>
    <div class="form-group"><label>DNS domain</label><input id="en-dns" class="form-control mono" value="${escapeHtml(n.dns_domain||'')}" placeholder="lxd"></div>
    <button class="btn btn-sm" onclick="saveNetworkSettings('${jsArg(name)}')">Apply settings</button>
    <p class="help">Note: NAT/DHCP/address don't apply to a no-IP LAN bridge (its IPv4 is "none").</p>
    <h4 style="margin-top:20px">Enslaved interfaces</h4>
    <table class="table"><tbody>${curRows}</tbody></table>
    <h4>Add an interface</h4>
    ${free.length
      ? `<div class="form-group"><select id="eb-iface" class="form-control">${addOpts}</select></div>
         <button class="btn" onclick="addBridgeIface('${jsArg(name)}')">Add interface</button>
         <p class="help">Only NICs with no host IP can be enslaved.</p>`
      : '<p class="help">No free NIC available — every physical NIC has a host IP or is already enslaved.</p>'}
    <div id="eb-error" class="error" style="display:none"></div>`, { wide: true });
}
async function saveNetworkSettings(name) {
  const config = {
    'ipv4.address': ($('en-ipv4').value || '').trim() || 'none',
    'ipv4.nat': $('en-nat').checked ? 'true' : 'false',
    'ipv4.dhcp': $('en-dhcp').checked ? 'true' : 'false',
    'dns.domain': ($('en-dns').value || '').trim(),
  };
  try { await API.patch(`/api/networks/${encodeURIComponent(name)}`, { config }); alert('Applied.'); editNetwork(name); page_ctnetworks(); }
  catch (e) { const el = $('eb-error'); el.textContent = e.message; el.style.display = 'block'; }
}
async function addBridgeIface(name) {
  const n = await _netByName(name);
  const current = _extIfaces(n);
  const add = $('eb-iface').value;
  if (!add || current.indexOf(add) !== -1) return;
  try { await _setBridgeIfaces(name, current.concat([add])); await editNetwork(name); page_ctnetworks(); }
  catch (e) { const el = $('eb-error'); el.textContent = e.message; el.style.display = 'block'; }
}
async function removeBridgeIface(name, iface) {
  const n = await _netByName(name);
  const current = _extIfaces(n).filter(x => x !== iface);
  try { await _setBridgeIfaces(name, current); await editNetwork(name); page_ctnetworks(); }
  catch (e) { alert(e.message); }
}

// Change an instance's network (works for new or existing instances).
async function changeNetwork(name) {
  let nets = [];
  try { nets = await API.get('/api/networks'); } catch (e) {}
  const opts = nets.filter(n => n.managed || n.type === 'bridge')
    .map(n => `<option value="${escapeHtml(n.name)}">${escapeHtml(n.name)} (${escapeHtml(n.type)}${n.managed?'':' · unmanaged'})</option>`).join('');
  openModal(`Change network — ${name}`, `
    <div class="form-group"><label>Attach eth0 to</label><select id="cn-net" class="form-control">${opts}</select></div>
    <p class="help">The change overrides the profile's NIC for this instance. A running container usually needs a restart to pick up the new network.</p>
    <div id="cn-error" class="error" style="display:none"></div>
    <button class="btn" onclick="submitChangeNetwork('${jsArg(name)}')">Apply</button>`);
}
async function submitChangeNetwork(name) {
  const errEl = $('cn-error'); errEl.style.display = 'none';
  try {
    await API.post(`/api/instances/${encodeURIComponent(name)}/network`, { network: $('cn-net').value, device: 'eth0' });
    closeModal();
    if (confirm('Network updated. Restart the instance now to apply it?')) {
      try { await API.put(`/api/instances/${encodeURIComponent(name)}/state`, { action: 'restart', force: true }); } catch (e) {}
    }
    refreshCurrent();
  } catch (e) { errEl.textContent = e.message; errEl.style.display = 'block'; }
}

// ═══════════════════════════════════════════════════════
//  Port forwarding (LXD proxy devices)
// ═══════════════════════════════════════════════════════
async function page_portforward() {
  const proxies = await API.get('/api/proxies');
  const rows = proxies.map(p => `<tr>
    <td class="mono">${escapeHtml(p.instance)}</td>
    <td class="mono">${escapeHtml(p.device)}</td>
    <td class="mono">${escapeHtml(p.listen)}</td>
    <td class="mono">${escapeHtml(p.connect)}</td>
    <td>${escapeHtml(p.bind)}${p.nat==='true'?' · nat':''}</td>
    <td>${statusBadge(p.status)}</td>
    <td>${currentRole==='admin'?`<button class="btn btn-sm btn-danger" onclick="removeProxy('${jsArg(p.instance)}','${jsArg(p.device)}')">Remove</button>`:''}</td>
    </tr>`).join('') || '<tr><td colspan="7" class="help">No port forwards defined.</td></tr>';
  const addBtn = currentRole==='admin' ? `<button class="btn" onclick="openProxyAdd()">Add Port Forward</button>` : '';
  $('page-content').innerHTML = `
    <div class="page-header"><h2>Port Forwarding</h2>${addBtn}</div>
    <p class="help">Forward a host port to a port inside a container (LXD <span class="mono">proxy</span> device). Example: listen <span class="mono">tcp:0.0.0.0:80</span> → connect <span class="mono">tcp:127.0.0.1:80</span>. No manual iptables needed.</p>
    <table class="table"><thead><tr><th>Instance</th><th>Name</th><th>Listen (host)</th><th>Connect (container)</th><th>Mode</th><th>State</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}
async function openProxyAdd(prefillInstance) {
  let insts = [];
  try { insts = await API.get('/api/instances'); } catch (e) {}
  const opts = insts.map(i => `<option value="${escapeHtml(i.name)}" ${i.name===prefillInstance?'selected':''}>${escapeHtml(i.name)}</option>`).join('');
  openModal('Add Port Forward', `
    <div class="form-group"><label>Instance</label><select id="pf-inst" class="form-control">${opts}</select></div>
    <div class="form-group"><label>Name</label><input id="pf-dev" class="form-control mono" placeholder="http" value="fwd"></div>
    <div class="form-group"><label>Listen on host</label><input id="pf-listen" class="form-control mono" placeholder="tcp:0.0.0.0:80"></div>
    <div class="form-group"><label>Connect inside container</label><input id="pf-connect" class="form-control mono" placeholder="tcp:127.0.0.1:80"></div>
    <div class="form-group"><label><input type="checkbox" id="pf-nat"> Use NAT mode (DNAT — preserves source IP; requires the container's real IP in Connect, not 127.0.0.1)</label></div>
    <div id="pf-error" class="error" style="display:none"></div>
    <button class="btn" onclick="submitProxy()">Add</button>`);
}
async function submitProxy() {
  const errEl = $('pf-error'); errEl.style.display = 'none';
  const inst = $('pf-inst').value;
  const body = { device: ($('pf-dev').value||'').trim(), listen: ($('pf-listen').value||'').trim(),
                 connect: ($('pf-connect').value||'').trim(), nat: $('pf-nat').checked };
  try { await API.post(`/api/instances/${encodeURIComponent(inst)}/proxy`, body); closeModal(); page_portforward(); }
  catch (e) { errEl.textContent = e.message; errEl.style.display = 'block'; }
}
async function removeProxy(inst, dev) {
  if (!confirm(`Remove port forward "${dev}" from ${inst}?`)) return;
  try { await API.delete(`/api/instances/${encodeURIComponent(inst)}/device/${encodeURIComponent(dev)}`); page_portforward(); }
  catch (e) { alert(e.message); }
}

// ═══════════════════════════════════════════════════════
//  System pages
// ═══════════════════════════════════════════════════════
