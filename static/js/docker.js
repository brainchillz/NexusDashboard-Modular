// Docker subsystem page — containers / images / volumes / networks tabs.
// Shared scaffolding (API, modal, router, theme, auth) lives in core.js.
let dockerTab = 'containers';

function dkStateBadge(state) {
  const s = (state || '').toLowerCase();
  const cls = s === 'running' ? 'green'
    : (s === 'paused' ? 'yellow'
    : (s === 'restarting' || s === 'dead' ? 'red' : 'gray'));
  return `<span class="status-badge ${cls}">${escapeHtml(state || '?')}</span>`;
}

async function page_docker() {
  const d = await API.get('/api/docker');
  if (!d.reachable) {
    $('page-content').innerHTML = `<h2>Docker</h2>
      <div class="alert alert-info">The Docker daemon is not reachable at <code>${escapeHtml(d.socket)}</code>.
        ${escapeHtml(d.error || '')}<br>
        Install Docker and add the service user to the <code>docker</code> group
        (<code>usermod -aG docker dashboard</code>, then restart the dashboard), and reload this page.</div>`;
    return;
  }
  const tabs = ['containers', 'images', 'volumes', 'networks'].map(t =>
    `<button class="btn btn-sm ${dockerTab === t ? '' : 'btn-outline'}"
       onclick="dkSwitchTab('${t}')">${t[0].toUpperCase() + t.slice(1)}</button>`).join(' ');
  $('page-content').innerHTML = `
    <div class="page-header"><h2>Docker</h2></div>
    <p class="help">Engine ${escapeHtml(d.version || '?')} (API ${escapeHtml(d.api_version || '?')}) —
      ${d.running} running / ${d.containers} containers, ${d.images} images,
      storage <code>${escapeHtml(d.storage_driver || '?')}</code></p>
    <div class="toolbar" id="dk-tabs">${tabs}</div>
    <div id="dk-tab-content"><div class="loading">Loading...</div></div>`;
  await dkRenderTab();
}

function dkSwitchTab(t) {
  dockerTab = t;
  page_docker();
}

async function dkRenderTab() {
  const el = $('dk-tab-content');
  try {
    if (dockerTab === 'containers') el.innerHTML = await dkContainersView();
    else if (dockerTab === 'images') el.innerHTML = await dkImagesView();
    else if (dockerTab === 'volumes') el.innerHTML = await dkVolumesView();
    else el.innerHTML = await dkNetworksView();
  } catch (e) {
    el.innerHTML = `<div class="error">Error: ${escapeHtml(e.message)}</div>`;
  }
}

// ─── Containers ─────────────────────────────────────────
async function dkContainersView() {
  const cts = await API.get('/api/docker/containers');
  const admin = currentRole === 'admin';
  const createBtn = admin
    ? `<div class="toolbar" style="margin-bottom:12px"><button class="btn" onclick="dkCreateModal()">Create Container</button></div>` : '';
  if (!cts.length) return createBtn + '<p class="help">No containers yet.</p>';
  const rows = cts.map(c => {
    const running = c.state === 'running';
    const id = jsArg(c.id);
    let actions = '';
    if (admin) {
      actions = running
        ? `<button class="btn btn-sm" onclick="dkOpenShell('${id}','${jsArg(c.name)}')">Shell</button>
           <button class="btn btn-sm" onclick="dkAction('${id}','restart')">Restart</button>
           <button class="btn btn-sm btn-warning" onclick="dkAction('${id}','stop')">Stop</button>`
        : `<button class="btn btn-sm btn-success" onclick="dkAction('${id}','start')">Start</button>`;
      actions += ` <button class="btn btn-sm btn-danger" onclick="dkDeleteContainer('${id}','${jsArg(c.name)}',${running})">Delete</button>`;
    }
    return `<tr>
      <td style="max-width:340px"><a href="#" onclick="dkOpenContainer('${id}');return false" class="mono">${escapeHtml(c.name)}</a>
        <div class="help mono" style="word-break:break-all">${escapeHtml(c.image)}</div></td>
      <td>${dkStateBadge(c.state)} <span class="help">${escapeHtml(c.status || '')}</span></td>
      <td class="mono">${(c.ports || []).map(escapeHtml).join('<br>') || '—'}</td>
      <td>${escapeHtml(c.compose_project || '')}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-sm btn-outline" onclick="dkLogs('${id}','${jsArg(c.name)}')">Logs</button>
        ${running ? `<button class="btn btn-sm btn-outline" onclick="dkStats('${id}','${jsArg(c.name)}')">Stats</button>` : ''}
        ${actions}</td>
    </tr>`;
  }).join('');
  return createBtn + `<table class="table"><thead><tr>
    <th>Name / Image</th><th>State</th><th>Ports</th><th>Compose</th><th>Actions</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

async function dkAction(id, action) {
  try {
    const r = await API.post(`/api/docker/containers/${encodeURIComponent(id)}/action`, { action });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_docker();
}

async function dkDeleteContainer(id, name, running) {
  const extra = running ? ' It is RUNNING and will be force-stopped.' : '';
  if (!confirm(`Delete container "${name}"?${extra} Its named volumes are kept.`)) return;
  try {
    const r = await API.post(`/api/docker/containers/${encodeURIComponent(id)}/delete`,
                             { force: running });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_docker();
}

async function dkOpenContainer(id) {
  let c;
  try { c = await API.get(`/api/docker/containers/${encodeURIComponent(id)}`); }
  catch (e) { alert(e.message); return; }
  const kv = (k, v) => `<tr><td class="help" style="white-space:nowrap">${k}</td><td class="mono" style="word-break:break-all">${v}</td></tr>`;
  const mounts = (c.mounts || []).map(m =>
    `${escapeHtml(m.source || '')} &rarr; ${escapeHtml(m.destination || '')}${m.rw ? '' : ' (ro)'} <span class="help">[${escapeHtml(m.type || '')}]</span>`).join('<br>') || '—';
  const nets = Object.entries(c.networks || {}).map(([n, i]) =>
    `${escapeHtml(n)}${i.ip ? ' <span class="help">' + escapeHtml(i.ip) + '</span>' : ''}`).join(', ') || '—';
  const st = c.state || {};
  openModal(`Container: ${escapeHtml(c.name)}`, `
    <table class="table">
      ${kv('Id', escapeHtml(c.id))}
      ${kv('Image', escapeHtml(c.image || '') + ' <span class="help">' + escapeHtml(c.image_id || '') + '</span>')}
      ${kv('State', dkStateBadge(st.status) + (st.health ? ' health: ' + escapeHtml(st.health) : '')
           + (st.status === 'exited' ? ' exit code ' + st.exit_code : ''))}
      ${kv('Started', escapeHtml(st.started_at || '—'))}
      ${kv('Restart policy', escapeHtml(c.restart_policy || '—'))}
      ${kv('Ports', (c.ports || []).map(escapeHtml).join('<br>') || '—')}
      ${kv('Mounts', mounts)}
      ${kv('Networks', nets)}
      ${kv('Command', escapeHtml(((c.entrypoint || []).concat(c.cmd || [])).join(' ')) || '—')}
      ${c.compose_project ? kv('Compose project', escapeHtml(c.compose_project)) : ''}
    </table>
    <div style="display:flex;gap:8px">
      <button class="btn btn-outline" onclick="closeModal();dkLogs('${jsArg(c.id)}','${jsArg(c.name)}')">Logs</button>
      ${st.status === 'running' ? `<button class="btn btn-outline" onclick="closeModal();dkStats('${jsArg(c.id)}','${jsArg(c.name)}')">Stats</button>` : ''}
      ${currentRole === 'admin' && (c.env || []).length ?
        `<button class="btn btn-outline" onclick="dkToggleEnv()">Environment</button>` : ''}
    </div>
    <pre id="dk-env" class="mono" style="display:none;overflow-x:auto">${(c.env || []).map(escapeHtml).join('\n')}</pre>`);
}

function dkToggleEnv() {
  const el = $('dk-env');
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function dkLogs(id, name, tail) {
  tail = tail || 200;
  let r;
  try { r = await API.get(`/api/docker/containers/${encodeURIComponent(id)}/logs?tail=${tail}`); }
  catch (e) { alert(e.message); return; }
  openModal(`Logs: ${escapeHtml(name)}`, `
    <div class="toolbar">
      ${[200, 1000, 5000].map(n => `<button class="btn btn-sm ${n === tail ? '' : 'btn-outline'}"
          onclick="dkLogs('${jsArg(id)}','${jsArg(name)}',${n})">${n}</button>`).join(' ')}
      <button class="btn btn-sm btn-outline" onclick="dkLogs('${jsArg(id)}','${jsArg(name)}',${tail})">Refresh</button>
    </div>
    <pre class="mono" style="max-height:60vh;overflow:auto;white-space:pre-wrap">${escapeHtml(r.logs || '(no output)')}</pre>`);
  // Scroll the log tail into view.
  const pre = document.querySelector('#modal-body pre');
  if (pre) pre.scrollTop = pre.scrollHeight;
}

async function dkStats(id, name) {
  let s;
  try { s = await API.get(`/api/docker/containers/${encodeURIComponent(id)}/stats`); }
  catch (e) { alert(e.message); return; }
  const memPct = (s.mem_usage != null && s.mem_limit) ? (s.mem_usage / s.mem_limit * 100) : null;
  openModal(`Stats: ${escapeHtml(name)}`, `
    <div class="cards">
      <div class="card"><div class="card-title">CPU</div>
        <div class="card-value">${s.cpu_pct != null ? s.cpu_pct + '%' : '—'}</div></div>
      <div class="card"><div class="card-title">Memory</div>
        <div class="card-value">${s.mem_usage != null ? fmtBytes(s.mem_usage) : '—'}</div>
        <div class="card-sub">${memPct != null ? memPct.toFixed(1) + '% of ' + fmtBytes(s.mem_limit) : ''}</div></div>
      <div class="card"><div class="card-title">Network I/O</div>
        <div class="card-value">${fmtBytes(s.net_rx)} rx</div>
        <div class="card-sub">${fmtBytes(s.net_tx)} tx</div></div>
      <div class="card"><div class="card-title">Disk I/O</div>
        <div class="card-value">${fmtBytes(s.blk_read)} read</div>
        <div class="card-sub">${fmtBytes(s.blk_write)} written</div></div>
      <div class="card"><div class="card-title">PIDs</div>
        <div class="card-value">${s.pids != null ? s.pids : '—'}</div></div>
    </div>
    <button class="btn btn-outline" onclick="dkStats('${jsArg(id)}','${jsArg(name)}')">Refresh</button>`);
}

// ─── Images ─────────────────────────────────────────────
async function dkImagesView() {
  const imgs = await API.get('/api/docker/images');
  const admin = currentRole === 'admin';
  const pullBox = admin ? `
    <div style="display:flex;gap:8px;max-width:640px;margin-bottom:12px">
      <input id="dk-pull-ref" class="form-control" placeholder="e.g. nginx:latest or ghcr.io/owner/image:tag">
      <button class="btn" onclick="dkPull(this)">Pull</button>
      <button class="btn btn-outline" onclick="dkPruneImages()">Prune dangling</button>
    </div>
    <p class="help">Pulls run to completion before the page refreshes — large images can take minutes.</p>` : '';
  if (!imgs.length) return pullBox + '<p class="help">No images.</p>';
  const rows = imgs.map(i => {
    const ref = i.tags.length ? i.tags[0] : i.id;
    // Long registry refs must wrap, never widen the table: split repo:tag
    // (the last colon after the final slash — a registry :port is not a tag).
    const tags = i.tags.map(t => {
      const cut = t.lastIndexOf(':');
      const repo = cut > t.lastIndexOf('/') ? t.slice(0, cut) : t;
      const tag = cut > t.lastIndexOf('/') ? t.slice(cut + 1) : '';
      return `<div class="mono" style="word-break:break-all">${escapeHtml(repo)}</div>` +
             (tag ? `<div class="help mono">tag: ${escapeHtml(tag)}</div>` : '');
    }).join('') || '<span class="help">&lt;none&gt; (dangling)</span>';
    return `<tr>
      <td style="max-width:380px">${tags}</td>
      <td class="mono">${escapeHtml(i.id)}</td>
      <td>${fmtBytes(i.size)}</td>
      <td>${i.created ? new Date(i.created * 1000).toLocaleString() : '—'}</td>
      <td>${i.in_use ? '<span class="status-badge green">in use</span>' : ''}</td>
      <td>${admin ? `<button class="btn btn-sm btn-danger" onclick="dkDeleteImage('${jsArg(ref)}',${i.in_use})">Delete</button>` : ''}</td>
    </tr>`;
  }).join('');
  return pullBox + `<table class="table"><thead><tr>
    <th>Image</th><th>Id</th><th>Size</th><th>Created</th><th></th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

async function dkPull(btn) {
  const ref = $('dk-pull-ref').value.trim();
  if (!ref) { alert('Image reference required'); return; }
  if (btn) { btn.disabled = true; btn.textContent = 'Pulling…'; }
  try {
    const r = await API.post('/api/docker/images/pull', { reference: ref });
    if (!r.success) { alert(r.error || 'Pull failed'); return; }
  } catch (e) { alert(e.message); return; }
  finally { if (btn) { btn.disabled = false; btn.textContent = 'Pull'; } }
  page_docker();
}

async function dkDeleteImage(ref, inUse) {
  if (inUse) { alert('That image is used by a container — delete the container first.'); return; }
  if (!confirm(`Delete image ${ref}?`)) return;
  try {
    const r = await API.post('/api/docker/images/delete', { id: ref });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_docker();
}

async function dkPruneImages() {
  if (!confirm('Remove all dangling (untagged) images?')) return;
  try {
    const r = await API.post('/api/docker/images/prune', {});
    if (!r.success) { alert(r.error || 'Failed'); return; }
    alert(`Removed ${r.deleted} layer(s), reclaimed ${fmtBytes(r.reclaimed)}.`);
  } catch (e) { alert(e.message); }
  page_docker();
}

// ─── Volumes ────────────────────────────────────────────
async function dkVolumesView() {
  const vols = await API.get('/api/docker/volumes');
  const admin = currentRole === 'admin';
  const createBox = admin ? `
    <div style="display:flex;gap:8px;max-width:480px;margin-bottom:12px">
      <input id="dk-vol-name" class="form-control" placeholder="volume name">
      <button class="btn" onclick="dkCreateVolume()">Create</button>
    </div>` : '';
  if (!vols.length) return createBox + '<p class="help">No volumes.</p>';
  const rows = vols.map(v => `<tr>
      <td class="mono">${escapeHtml(v.name)}</td>
      <td>${escapeHtml(v.driver)}</td>
      <td class="mono">${escapeHtml(v.mountpoint || '')}</td>
      <td>${(v.used_by || []).map(escapeHtml).join(', ') || '<span class="help">unused</span>'}</td>
      <td>${admin && !(v.used_by || []).length ?
        `<button class="btn btn-sm btn-danger" onclick="dkDeleteVolume('${jsArg(v.name)}')">Delete</button>` : ''}</td>
    </tr>`).join('');
  return createBox + `<table class="table"><thead><tr>
    <th>Name</th><th>Driver</th><th>Mountpoint</th><th>Used by</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

async function dkCreateVolume() {
  const name = $('dk-vol-name').value.trim();
  if (!name) { alert('Name required'); return; }
  try {
    const r = await API.post('/api/docker/volumes/create', { name });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_docker();
}

async function dkDeleteVolume(name) {
  if (!confirm(`Delete volume "${name}"? Its DATA is permanently removed.`)) return;
  try {
    const r = await API.post('/api/docker/volumes/delete', { name });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_docker();
}

// ─── Networks ───────────────────────────────────────────
async function dkNetworksView() {
  const nets = await API.get('/api/docker/networks');
  const admin = currentRole === 'admin';
  const createBox = admin ? `
    <div style="display:flex;gap:8px;max-width:640px;margin-bottom:12px">
      <input id="dk-net-name" class="form-control" placeholder="network name">
      <input id="dk-net-subnet" class="form-control" placeholder="subnet (optional, e.g. 172.30.0.0/16)">
      <button class="btn" onclick="dkCreateNetwork()">Create</button>
    </div>` : '';
  const rows = nets.map(n => `<tr>
      <td class="mono">${escapeHtml(n.name)}${n.builtin ? ' <span class="help">(built-in)</span>' : ''}</td>
      <td>${escapeHtml(n.driver || '')}</td>
      <td class="mono">${(n.subnets || []).map(escapeHtml).join(', ') || '—'}</td>
      <td>${n.containers}</td>
      <td>${admin && !n.builtin && !n.containers ?
        `<button class="btn btn-sm btn-danger" onclick="dkDeleteNetwork('${jsArg(n.name)}')">Delete</button>` : ''}</td>
    </tr>`).join('');
  return createBox + `<table class="table"><thead><tr>
    <th>Name</th><th>Driver</th><th>Subnets</th><th>Containers</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

async function dkCreateNetwork() {
  const name = $('dk-net-name').value.trim();
  const subnet = $('dk-net-subnet').value.trim();
  if (!name) { alert('Name required'); return; }
  try {
    const r = await API.post('/api/docker/networks/create', { name, subnet });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_docker();
}

async function dkDeleteNetwork(name) {
  if (!confirm(`Delete network "${name}"?`)) return;
  try {
    const r = await API.post('/api/docker/networks/delete', { name });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_docker();
}

// ─── Create container ───────────────────────────────────
async function dkCreateModal() {
  let nets = [];
  try { nets = await API.get('/api/docker/networks'); } catch (e) {}
  const netOpts = ['<option value="">(default bridge)</option>']
    .concat(nets.filter(n => n.driver === 'bridge' || !n.builtin)
                .map(n => `<option value="${jsArg(n.name)}">${escapeHtml(n.name)}</option>`))
    .join('');
  openModal('Create Container', `
    <div class="form-group"><label>Image (pulled automatically if not local)</label>
      <input id="dkc-image" class="form-control" placeholder="e.g. nginx:latest or ghcr.io/owner/image:tag"></div>
    <div class="form-group"><label>Name (optional)</label>
      <input id="dkc-name" class="form-control" placeholder="my-app"></div>
    <div class="form-group"><label>Ports — one per line: <span class="mono">host:container[/udp]</span> or <span class="mono">ip:host:container</span></label>
      <textarea id="dkc-ports" class="form-control" rows="2" placeholder="8080:80&#10;127.0.0.1:5432:5432"></textarea></div>
    <div class="form-group"><label>Volumes — one per line: <span class="mono">volume-or-/host/path:/container/path[:ro]</span></label>
      <textarea id="dkc-volumes" class="form-control" rows="2" placeholder="appdata:/data&#10;/srv/media:/media:ro"></textarea></div>
    <div class="form-group"><label>Environment — one per line: <span class="mono">KEY=value</span></label>
      <textarea id="dkc-env" class="form-control" rows="2" placeholder="TZ=America/New_York"></textarea></div>
    <div style="display:flex;gap:8px">
      <div class="form-group" style="flex:1"><label>Restart policy</label>
        <select id="dkc-restart" class="form-control">
          <option value="unless-stopped">unless-stopped (recommended)</option>
          <option value="always">always</option>
          <option value="on-failure">on-failure</option>
          <option value="no">no</option>
        </select></div>
      <div class="form-group" style="flex:1"><label>Network</label>
        <select id="dkc-network" class="form-control">${netOpts}</select></div>
    </div>
    <div class="form-group"><label>Command override (optional)</label>
      <input id="dkc-command" class="form-control" placeholder="leave empty for the image default"></div>
    <div class="form-group"><label><input type="checkbox" id="dkc-start" checked> Start after creating</label></div>
    <button class="btn" onclick="dkCreate(this)">Create</button>
    <p class="help" id="dkc-note" style="display:none">Pulling the image — this can take minutes for large images…</p>`);
}

function dkParsePorts(text) {
  const out = [];
  for (const line of text.split('\n').map(s => s.trim()).filter(Boolean)) {
    let [spec, proto] = line.split('/');
    proto = (proto || 'tcp').trim();
    const parts = spec.split(':').map(s => s.trim());
    if (parts.length === 2) out.push({ host: +parts[0], container: +parts[1], proto });
    else if (parts.length === 3) out.push({ host_ip: parts[0], host: +parts[1], container: +parts[2], proto });
    else throw new Error(`Bad port line: "${line}"`);
  }
  return out;
}

function dkParseVolumes(text) {
  const out = [];
  for (const line of text.split('\n').map(s => s.trim()).filter(Boolean)) {
    const m = line.match(/^(.+?):(\/[^:]*)(:ro)?$/);
    if (!m) throw new Error(`Bad volume line: "${line}" (need source:/dest[:ro])`);
    out.push({ source: m[1], destination: m[2], ro: !!m[3] });
  }
  return out;
}

async function dkCreate(btn) {
  let body;
  try {
    body = {
      image: $('dkc-image').value.trim(),
      name: $('dkc-name').value.trim(),
      ports: dkParsePorts($('dkc-ports').value),
      volumes: dkParseVolumes($('dkc-volumes').value),
      env: $('dkc-env').value.split('\n').map(s => s.trim()).filter(Boolean),
      restart: $('dkc-restart').value,
      network: $('dkc-network').value,
      command: $('dkc-command').value.trim(),
      start: $('dkc-start').checked,
    };
  } catch (e) { alert(e.message); return; }
  if (!body.image) { alert('Image required'); return; }
  btn.disabled = true; btn.textContent = 'Creating…';
  const note = $('dkc-note');
  if (note) note.style.display = 'block';
  try {
    const r = await API.post('/api/docker/containers', body);
    if (!r.success) { alert(r.error || 'Failed'); return; }
    if ((r.warnings || []).length) alert('Created with warnings:\n' + r.warnings.join('\n'));
    closeModal();
  } catch (e) { alert(e.message); return; }
  finally {
    btn.disabled = false; btn.textContent = 'Create';
    if (note) note.style.display = 'none';
  }
  page_docker();
}

// ─── Compose stacks ─────────────────────────────────────
async function page_compose() {
  const d = await API.get('/api/compose');
  const admin = currentRole === 'admin';
  if (!d.available) {
    $('page-content').innerHTML = `<h2>Compose Stacks</h2>
      <div class="alert alert-info">The <code>docker compose</code> plugin is not available on this host
        (or Docker itself is not installed). Install <code>docker-compose-v2</code> and reload.</div>`;
    return;
  }
  const cards = (d.stacks || []).map(st => {
    const name = jsArg(st.name);
    const services = st.services.map(s => `<tr>
        <td class="mono">${escapeHtml(s.service)}</td>
        <td class="mono">${escapeHtml(s.container)}</td>
        <td>${dkStateBadge(s.state)} <span class="help">${escapeHtml(s.status || '')}</span></td>
        <td>${s.state === 'running' && admin ? `<button class="btn btn-sm btn-outline" onclick="dkOpenShell('${jsArg(s.id)}','${jsArg(s.container)}')">Shell</button>` : ''}
            <button class="btn btn-sm btn-outline" onclick="dkLogs('${jsArg(s.id)}','${jsArg(s.container)}')">Logs</button></td>
      </tr>`).join('');
    let actions = '';
    if (admin && st.actions_available) {
      actions = st.running
        ? `<button class="btn btn-sm" onclick="composeAction('${name}','restart')">Restart</button>
           <button class="btn btn-sm" onclick="composeAction('${name}','pull')">Pull</button>
           <button class="btn btn-sm btn-warning" onclick="composeAction('${name}','down')">Down</button>`
        : `<button class="btn btn-sm btn-success" onclick="composeAction('${name}','up')">Up</button>`;
      actions += ` <button class="btn btn-sm btn-outline" onclick="composeLogs('${name}')">Stack Logs</button>`;
    }
    if (st.file_readable)
      actions += ` <button class="btn btn-sm btn-outline" onclick="composeFile('${name}')">${st.managed && admin ? 'Edit' : 'View'} File</button>`;
    if (admin && st.managed && !st.total)
      actions += ` <button class="btn btn-sm btn-danger" onclick="composeDelete('${name}')">Delete</button>`;
    return `<div class="card" style="margin-bottom:16px;max-width:900px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <div><strong class="mono">${escapeHtml(st.name)}</strong>
          ${st.managed ? '<span class="status-badge green">managed</span>' : '<span class="status-badge gray">adopted</span>'}
          <span class="help">${st.running}/${st.total} running</span></div>
        <div style="white-space:nowrap">${actions}</div>
      </div>
      <div class="help mono" style="word-break:break-all">${escapeHtml(st.working_dir || '(no project directory label)')}</div>
      ${services ? `<table class="table" style="margin-top:8px"><thead><tr>
        <th>Service</th><th>Container</th><th>State</th><th></th></tr></thead>
        <tbody>${services}</tbody></table>` : '<p class="help">No containers (stack is down).</p>'}
    </div>`;
  }).join('');
  $('page-content').innerHTML = `
    <div class="page-header"><h2>Compose Stacks</h2>
      ${admin ? '<button class="btn" onclick="composeCreateModal()">Create Stack</button>' : ''}</div>
    <p class="help">Stacks running on this host are discovered from their compose labels ("adopted");
      stacks created here live under <code>${escapeHtml(d.compose_root)}</code> and are editable ("managed").
      Down never deletes volumes.</p>
    ${cards || '<p class="help">No compose stacks found.</p>'}`;
}

async function composeAction(name, action) {
  if (action === 'down' && !confirm(`Bring stack "${name}" down? All its containers stop and are removed (volumes are kept).`)) return;
  const note = action === 'up' || action === 'pull' ? ' — this can take minutes if images need pulling' : '';
  $('page-content').insertAdjacentHTML('afterbegin',
    `<div class="alert alert-info" id="compose-busy">Running docker compose ${escapeHtml(action)}${note}…</div>`);
  try {
    const r = await API.post(`/api/compose/${encodeURIComponent(name)}/action`, { action });
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_compose();
}

async function composeLogs(name, tail) {
  tail = tail || 200;
  let r;
  try { r = await API.get(`/api/compose/${encodeURIComponent(name)}/logs?tail=${tail}`); }
  catch (e) { alert(e.message); return; }
  openModal(`Stack logs: ${escapeHtml(name)}`, `
    <div class="toolbar">
      ${[200, 1000, 5000].map(n => `<button class="btn btn-sm ${n === tail ? '' : 'btn-outline'}"
          onclick="composeLogs('${jsArg(name)}',${n})">${n}</button>`).join(' ')}
    </div>
    <pre class="mono" style="max-height:60vh;overflow:auto;white-space:pre-wrap">${escapeHtml(r.logs || '(no output)')}</pre>`);
  const pre = document.querySelector('#modal-body pre');
  if (pre) pre.scrollTop = pre.scrollHeight;
}

async function composeFile(name) {
  let r;
  try { r = await API.get(`/api/compose/${encodeURIComponent(name)}/file`); }
  catch (e) { alert(e.message); return; }
  const editable = r.editable && currentRole === 'admin';
  openModal(`${editable ? 'Edit' : 'View'}: ${escapeHtml(name)}`, `
    <p class="help mono" style="word-break:break-all">${escapeHtml(r.file)}</p>
    <textarea id="compose-content" class="form-control mono" rows="20"
      style="white-space:pre;overflow-x:auto" ${editable ? '' : 'readonly'}>${escapeHtml(r.content)}</textarea>
    ${editable ? `<div style="margin-top:8px">
      <button class="btn" onclick="composeSave('${jsArg(name)}',this)">Validate &amp; Save</button>
      <span class="help">Saved changes apply on the next Up/Restart.</span></div>` : ''}`,
    { wide: true });
}

async function composeSave(name, btn) {
  btn.disabled = true; btn.textContent = 'Validating…';
  try {
    const r = await API.post(`/api/compose/${encodeURIComponent(name)}/file`,
                             { content: $('compose-content').value });
    if (!r.success) { alert(r.error || 'Failed'); return; }
    closeModal();
  } catch (e) { alert(e.message); return; }
  finally { btn.disabled = false; btn.textContent = 'Validate & Save'; }
  page_compose();
}

function composeCreateModal() {
  openModal('Create Compose Stack', `
    <div class="form-group"><label>Stack name (lowercase letters, digits, - and _)</label>
      <input id="compose-name" class="form-control" placeholder="my-stack"></div>
    <div class="form-group"><label>compose.yaml</label>
      <textarea id="compose-content" class="form-control mono" rows="16"
        style="white-space:pre;overflow-x:auto" placeholder="services:&#10;  web:&#10;    image: nginx:alpine&#10;    ports:&#10;      - '8080:80'"></textarea></div>
    <button class="btn" onclick="composeCreate(this)">Validate &amp; Create</button>
    <p class="help">The file is checked with <code>docker compose config</code> before it is kept.
      Bring the stack up from its card afterwards.</p>`, { wide: true });
}

async function composeCreate(btn) {
  const name = $('compose-name').value.trim();
  if (!name) { alert('Name required'); return; }
  btn.disabled = true; btn.textContent = 'Validating…';
  try {
    const r = await API.post('/api/compose/create',
                             { name, content: $('compose-content').value });
    if (!r.success) { alert(r.error || 'Failed'); return; }
    closeModal();
  } catch (e) { alert(e.message); return; }
  finally { btn.disabled = false; btn.textContent = 'Validate & Create'; }
  page_compose();
}

async function composeDelete(name) {
  if (!confirm(`Delete managed stack "${name}"? Its compose file is removed (volumes and images are untouched).`)) return;
  try {
    const r = await API.post(`/api/compose/${encodeURIComponent(name)}/delete`, {});
    if (!r.success) { alert(r.error || 'Failed'); return; }
  } catch (e) { alert(e.message); }
  page_compose();
}

// ─── Shell (exec terminal) ──────────────────────────────
// Reuses the xterm scaffolding globals from containers.js (_term, _termWs,
// _fit, closeConsole) — same modal lifecycle, different ws endpoint.
function dkOpenShell(id, name) {
  closeConsole();
  openModal(`Shell — ${escapeHtml(name)}`, `
    <p class="help">Interactive shell inside the container (bash if the image has it, else sh).
      Exiting the shell ends the session; the container keeps running.</p>
    <div id="console-term" class="console-term"></div>
    <div id="console-status" class="help" style="margin-top:6px">connecting…</div>
  `, { wide: true });
  _onModalClose = closeConsole;
  setTimeout(() => dkStartShell(id), 60);
}

function dkStartShell(id) {
  const el = $('console-term');
  if (!el || !window.Terminal) { $('console-status').textContent = 'terminal unavailable'; return; }
  _term = new Terminal({ cursorBlink: true, fontSize: 13, theme: { background: '#000000' } });
  _fit = new FitAddon.FitAddon();
  _term.loadAddon(_fit);
  _term.open(el);
  try { _fit.fit(); } catch (e) {}
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/docker/${encodeURIComponent(id)}`);
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
