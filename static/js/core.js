function onUnauthorized() { showLogin(); throw new Error('Session expired — please sign in'); }

const API = {
  async get(path) {
    const r = await fetch(path);
    if (r.status === 401) onUnauthorized();
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  async post(path, data) {
    const r = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    if (r.status === 401) onUnauthorized();
    const j = await r.json();
    if (!r.ok && !j.success) throw new Error(j.error || JSON.stringify(j));
    return j;
  },
  async put(path, data) {
    const r = await fetch(path, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    if (r.status === 401) onUnauthorized();
    const j = await r.json();
    if (!r.ok && !j.success) throw new Error(j.error || JSON.stringify(j));
    return j;
  },
  async delete(path) {
    const r = await fetch(path, { method: 'DELETE' });
    if (r.status === 401) onUnauthorized();
    const j = await r.json();
    if (!j.success) throw new Error(j.error || j.stderr || 'Command failed');
    return j;
  }
};

function $(id) { return document.getElementById(id); }
function showPage(id) { document.querySelectorAll('.nav-list a').forEach(a => a.classList.toggle('active', a.dataset.page === id)); renderPage(id); }
function escapeHtml(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
// Escape a value for safe use as a single-quoted JS string inside a
// double-quoted HTML attribute (e.g. onclick="fn('VALUE')").
function jsArg(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;')
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

// Multi-select rendered as a checkbox list — click any combination, no Ctrl/Shift
// gymnastics (native <select multiple> range-selects across non-adjacent items).
// items: [{value, label}]; read the chosen values back with checkedValues(id).
function checkboxList(id, items, emptyMsg) {
  if (!items || !items.length) {
    return `<div class="checklist" id="${id}"><p class="help" style="margin:6px">${escapeHtml(emptyMsg || 'None available')}</p></div>`;
  }
  return `<div class="checklist" id="${id}">` + items.map(it =>
    `<label class="checkitem"><input type="checkbox" value="${escapeHtml(String(it.value))}">${escapeHtml(String(it.label))}</label>`
  ).join('') + '</div>';
}
function checkedValues(id) {
  return Array.from(document.querySelectorAll('#' + id + ' input[type=checkbox]:checked')).map(c => c.value);
}

let isAuthed = false;
let currentUser = '';
let currentRole = 'admin';

// Parse a ZFS/human size string ("1.18M", "928G", "0", "512K") to bytes.
function parseSize(s) {
  if (s == null) return 0;
  s = String(s).trim();
  if (s === '' || s === '-') return 0;
  const m = s.match(/^([0-9.]+)\s*([KMGTPEZ]?)i?B?$/i);
  if (!m) return parseFloat(s) || 0;
  const u = { '': 1, K: 1024, M: 1024**2, G: 1024**3, T: 1024**4, P: 1024**5, E: 1024**6, Z: 1024**7 };
  return parseFloat(m[1]) * (u[m[2].toUpperCase()] || 1);
}

// Render a usage bar; colour shifts green -> yellow -> red as it fills.
function usageBar(pct) {
  pct = Math.max(0, Math.min(100, Math.round(pct)));
  const cls = pct >= 90 ? 'red' : pct >= 70 ? 'yellow' : 'green';
  return `<div class="usage-bar"><div class="usage-bar-fill ${cls}" style="width:${pct}%"></div><span class="usage-bar-label">${pct}%</span></div>`;
}

// ─── Modal ──────────────────────────────────────────────
function openModal(title, html, opts) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = html;
  // Wide modals (device/property tables) get more room so they don't overflow.
  $('modal-content').classList.toggle('wide', !!(opts && opts.wide));
  $('modal-overlay').style.display = 'flex';
}
let modalLocked = false;  // forced modals (e.g. first-run password change) can't be dismissed
let _onModalClose = null;  // page cleanup hook (e.g. console ws teardown)
function closeModal() {
  if (modalLocked) return;
  $('modal-overlay').style.display = 'none';
  const fn = _onModalClose; _onModalClose = null;
  if (fn) try { fn(); } catch (e) {}
}
$('modal-overlay').addEventListener('click', e => { if(e.target === $('modal-overlay')) closeModal(); });

// Reusable "type the exact name to confirm" guard for irreversible/destructive
// actions (pool & dataset destroy, rollback, …). opts: {title, name, warning,
// label?, button?, onConfirm: async () => {...}}.
let _confirmNameFn = null;
function confirmName(opts) {
  _confirmNameFn = opts.onConfirm;
  openModal(opts.title, `
    <div class="alert alert-warning"><strong>Destructive &amp; irreversible.</strong> ${opts.warning}</div>
    <div class="form-group">
      <label>${opts.label || 'Type'} <code>${escapeHtml(opts.name)}</code> to confirm</label>
      <input id="confirm-name" class="form-control" autocomplete="off" spellcheck="false"
             onkeydown="if(event.key==='Enter'){event.preventDefault();confirmNameGo('${jsArg(opts.name)}');}">
    </div>
    <button class="btn btn-danger" onclick="confirmNameGo('${jsArg(opts.name)}')">${opts.button || 'Confirm'}</button>
  `);
  setTimeout(() => { const el = $('confirm-name'); if (el) el.focus(); }, 50);
}
async function confirmNameGo(name) {
  if (($('confirm-name').value || '').trim() !== name) { alert('Type the name exactly to confirm.'); return; }
  const fn = _confirmNameFn; _confirmNameFn = null;
  if (fn) await fn();
}

// ─── Navigation ─────────────────────────────────────────
document.querySelectorAll('.nav-list a').forEach(a => {
  a.addEventListener('click', e => { e.preventDefault(); showPage(a.dataset.page); });
});

function toggleCat(cat) {
  const g = document.querySelector(`.nav-group[data-cat="${cat}"]`);
  if (!g) return;
  g.classList.toggle('collapsed');
  try {
    const st = JSON.parse(localStorage.getItem('navCollapsed') || '{}');
    st[cat] = g.classList.contains('collapsed');
    localStorage.setItem('navCollapsed', JSON.stringify(st));
  } catch (e) {}
}

function restoreNavCats() {
  try {
    const st = JSON.parse(localStorage.getItem('navCollapsed') || '{}');
    Object.entries(st).forEach(([cat, collapsed]) => {
      const g = document.querySelector(`.nav-group[data-cat="${cat}"]`);
      if (g && collapsed) g.classList.add('collapsed');
    });
  } catch (e) {}
}
restoreNavCats();

// ─── Feature modules (nav visibility) ───────────────────
// Disabled modules are hidden from the left nav. State is global (set by an
// admin on the Modules page) and applied on every load for all users.
let moduleEnabled = {};  // id -> bool
async function applyModules() {
  try {
    const r = await API.get('/api/modules');
    moduleEnabled = {};
    (r.modules || []).forEach(m => {
      moduleEnabled[m.id] = m.enabled;
      const a = document.querySelector(`.nav-list a[data-page="${m.id}"]`);
      if (a && a.parentElement) a.parentElement.classList.toggle('module-hidden', !m.enabled);
      // Multi-page modules (one toggle, several nav entries) mark each <li>
      // with data-module=<id> since data-page != the module id.
      document.querySelectorAll(`.nav-list li[data-module="${m.id}"]`).forEach(
        li => li.classList.toggle('module-hidden', !m.enabled));
    });
    // Hide a whole nav group (e.g. "Sharing") when every item in it is hidden.
    const readonly = document.body.classList.contains('readonly');
    document.querySelectorAll('.nav-group').forEach(g => {
      const anyVisible = Array.from(g.querySelectorAll('.nav-sub > li')).some(li =>
        !li.classList.contains('module-hidden') &&
        !(readonly && li.classList.contains('nav-admin-only')));
      g.classList.toggle('group-hidden', !anyVisible);
    });
    // If the page currently in view just got disabled, fall back to the dashboard.
    const active = document.querySelector('.nav-list a.active');
    if (active) {
      const mod = active.parentElement && active.parentElement.dataset.module;
      if (moduleEnabled[active.dataset.page] === false ||
          (mod && moduleEnabled[mod] === false)) showPage('dashboard');
    }
  } catch (e) {}
}

async function renderPage(page) {
  $('page-content').innerHTML = '<div class="loading">Loading...</div>';
  // Reset scroll: .content scrolls independently, and the window itself
  // scrolls when the sidebar is taller than the viewport — reset both, or a
  // page opened while scrolled down starts below its own top.
  const content = document.querySelector('.content');
  if (content) content.scrollTop = 0;
  window.scrollTo(0, 0);
  try {
    if (typeof window['page_' + page] === 'function') await window['page_' + page]();
    else $('page-content').innerHTML = '<h2>Page not found</h2>';
  } catch(e) {
    $('page-content').innerHTML = `<div class="error">Error: ${escapeHtml(e.message)}</div>`;
  }
}

// ─── Theme (light / dark) ───────────────────────────────
function applyThemeLabel() {
  const light = document.documentElement.classList.contains('theme-light');
  const el = $('theme-label');
  if (el) el.textContent = light ? 'Dark theme' : 'Light theme';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', light ? '#ffffff' : '#1a1f2e');
}
function toggleTheme(e) {
  if (e) e.preventDefault();
  const light = document.documentElement.classList.toggle('theme-light');
  try { localStorage.setItem('theme', light ? 'light' : 'dark'); } catch (err) {}
  applyThemeLabel();
}

// ─── Dashboard ──────────────────────────────────────────
function fmtUptime(sec) {
  sec = Number(sec) || 0;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return (d ? d + 'd ' : '') + (d || h ? h + 'h ' : '') + m + 'm';
}

function resourcesPanel(r) {
  if (!r) return '';
  const mem = r.memory || {}, sw = r.swap || {}, load = r.load || {};
  const cpuCls = r.cpu_pct >= 90 ? 'red' : r.cpu_pct >= 70 ? 'yellow' : 'green';
  const swapRow = (sw.total > 0)
    ? `<div class="res-item"><div class="res-label">Swap</div>${usageBar(sw.pct || 0)}
         <div class="card-sub">${fmtBytes(sw.used)} / ${fmtBytes(sw.total)}</div></div>` : '';
  return `<h3>System Resources</h3>
    <div class="cards">
      <div class="card">
        <div class="card-head">CPU</div>
        <div class="card-value">${(Number(r.cpu_pct) || 0).toFixed(1)}<span class="card-unit">%</span></div>
        ${usageBar(r.cpu_pct || 0)}
        <div id="spark-cpu"></div>
        <div class="card-sub">${r.cpus || 1} cores · load ${load['1'] ?? '-'} / ${load['5'] ?? '-'} / ${load['15'] ?? '-'}</div>
      </div>
      <div class="card">
        <div class="card-head">Memory</div>
        <div class="card-value">${mem.pct ?? 0}<span class="card-unit">%</span></div>
        ${usageBar(mem.pct || 0)}
        <div id="spark-mem"></div>
        <div class="card-sub">${fmtBytes(mem.used)} / ${fmtBytes(mem.total)} used${sw.total > 0 ? ` · swap ${fmtBytes(sw.used)}/${fmtBytes(sw.total)}` : ''}</div>
      </div>
      <div class="card">
        <div class="card-head">Uptime</div>
        <div class="card-value" style="font-size:1.4em">${fmtUptime(r.uptime_seconds)}</div>
        <div class="card-sub">${r.cpus || 1} logical CPUs</div>
      </div>
    </div>`;
}

// Minimal inline-SVG sparkline from [[ts,value],...] — no chart lib (no build step).
function sparkline(points, opts) {
  opts = opts || {};
  const w = opts.w || 140, h = opts.h || 26, pad = 2;
  const pts = (points || []).filter(p => p && p[1] != null);
  if (pts.length < 2) return '<span class="help" style="font-size:.78em">collecting…</span>';
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 === y0) y1 = y0 + 1;
  const sx = t => pad + (x1 === x0 ? 0 : (t - x0) / (x1 - x0)) * (w - 2 * pad);
  const sy = v => (h - pad) - (v - y0) / (y1 - y0) * (h - 2 * pad);
  const d = pts.map((p, i) => (i ? 'L' : 'M') + sx(p[0]).toFixed(1) + ' ' + sy(p[1]).toFixed(1)).join(' ');
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${d}" fill="none" stroke="var(--primary,#c1550f)" stroke-width="1.5"/></svg>`;
}

// Lazily fill dashboard resource sparklines from the history store (last 24h).
async function fillResourceSparks() {
  for (const [id, metric] of [['spark-cpu', 'cpu_pct'], ['spark-mem', 'mem_pct']]) {
    const el = document.getElementById(id);
    if (!el) continue;
    try { const h = await API.get(`/api/history?metric=${metric}&since=86400`); el.innerHTML = sparkline(h.points); }
    catch (e) {}
  }
}

// Lazily fill ZFS pool "full in ~N days" notes from the forecast endpoint.
async function fillPoolForecasts(pools) {
  for (const p of (pools || [])) {
    const el = document.getElementById('fc-' + p.name);
    if (!el) continue;
    try {
      const f = await API.get(`/api/history/forecast?label=${encodeURIComponent(p.name)}`);
      if (f.days_to_full != null) el.textContent = `~${f.days_to_full}d to full`;
      else if (f.fill_rate_bytes_per_day > 0) el.textContent = 'filling';
    } catch (e) {}
  }
}

async function page_dashboard() {
  const [s, res] = await Promise.all([
    API.get('/api/summary'),
    API.get('/api/system/resources').catch(() => null),
  ]);
  const svc = s.services || {};
  const dot = k => (svc[k] && svc[k].active === 'active') ? 'green' : 'red';
  const sys = s.system || {};
  const z = s.zfs || {}, isc = s.iscsi || {}, nf = s.nfs || {}, sm = s.smb || {}, dk = s.disks || {};
  const alerts = s.alerts || [];

  const health = alerts.length
    ? `<div class="alert alert-warning"><strong>${alerts.length} issue${alerts.length > 1 ? 's' : ''}:</strong> ${alerts.map(escapeHtml).join(' · ')}</div>`
    : `<div class="health-ok">✓ All systems healthy</div>`;

  // First-run welcome: no pools yet -> point at the next steps. Skipped when the
  // ZFS module is disabled (e.g. an AI-focused node has no use for a pool prompt).
  const welcome = (currentRole === 'admin' && moduleEnabled['zfs'] !== false && (z.pools || 0) === 0) ? `
    <div class="alert alert-info">
      <strong>Welcome — let's get started.</strong> No ZFS pools exist yet.
      <div class="toolbar" style="margin-top:8px">
        <button class="btn btn-sm" onclick="showPage('disks')">1. Review disks</button>
        <button class="btn btn-sm" onclick="showPage('zfs')">2. Create a pool</button>
        <button class="btn btn-sm" onclick="showPage('smb')">3. Create a share</button>
        <button class="btn btn-sm btn-outline" onclick="showPage('notifications')">Set up notifications</button>
      </div>
    </div>` : '';

  const smartLabel = dk.smart_ok === false ? 'SMART FAIL' : (dk.smart_ok === null ? 'SMART n/a' : 'SMART OK');

  // One card per service module; only render those whose module is enabled
  // (a module is treated as enabled unless explicitly disabled).
  const cardDefs = [
    { id: 'zfs', html: `
    <div class="card card-link" onclick="showPage('zfs')">
      <div class="card-head"><span class="status-dot ${z.online ? dot('zfs') : 'red'}"></span>ZFS Pools</div>
      <div class="card-value">${z.pools || 0} <span class="card-unit">pool${z.pools === 1 ? '' : 's'}</span></div>
      <div class="card-sub">${escapeHtml(z.used || '0')} / ${escapeHtml(z.size || '0')}${z.scanning ? ' · scrubbing' : ''}</div>
      ${z.pools ? usageBar(z.pct || 0) : ''}
    </div>` },
    { id: 'iscsi', html: `
    <div class="card card-link" onclick="showPage('iscsi')">
      <div class="card-head"><span class="status-dot ${dot('iscsi')}"></span>iSCSI</div>
      <div class="card-value">${isc.targets || 0} <span class="card-unit">target${isc.targets === 1 ? '' : 's'}</span></div>
      <div class="card-sub">${isc.luns || 0} LUNs · ${isc.sessions || 0} connected</div>
      <div class="card-sub">${escapeHtml(isc.provisioned || '0B')} provisioned</div>
    </div>` },
    { id: 'nfs', html: `
    <div class="card card-link" onclick="showPage('nfs')">
      <div class="card-head"><span class="status-dot ${dot('nfs')}"></span>NFS</div>
      <div class="card-value">${nf.exports || 0} <span class="card-unit">export${nf.exports === 1 ? '' : 's'}</span></div>
      <div class="card-sub">${nf.clients || 0} client mount${nf.clients === 1 ? '' : 's'}</div>
    </div>` },
    { id: 'smb', html: `
    <div class="card card-link" onclick="showPage('smb')">
      <div class="card-head"><span class="status-dot ${dot('smb')}"></span>SMB</div>
      <div class="card-value">${sm.shares || 0} <span class="card-unit">share${sm.shares === 1 ? '' : 's'}</span></div>
      <div class="card-sub">${sm.users || 0} users · ${sm.connections || 0} connections</div>
    </div>` },
    { id: 'disks', html: `
    <div class="card card-link" onclick="showPage('disks')">
      <div class="card-head"><span class="status-dot ${dk.smart_ok === false ? 'red' : 'green'}"></span>Disks</div>
      <div class="card-value">${dk.total || 0} <span class="card-unit">disks</span></div>
      <div class="card-sub">${dk.free || 0} free · ${smartLabel}</div>
    </div>` },
  ];
  // llama.cpp health/metrics card — only when the module is enabled. Fetched
  // separately (it pings llama-server's /health + /metrics) so /api/summary
  // stays cheap and nothing runs when the module is off.
  if (moduleEnabled['llamacpp'] !== false) {
    let lh = null;
    try { lh = await API.get('/api/llama/health'); } catch (e) {}
    const up = !!(lh && lh.ok);
    const m = (lh && lh.metrics) || {};
    const bits = [];
    if (up) {
      bits.push(escapeHtml(lh.status || 'ok'));
      if (m.kv_cache_usage_ratio != null) bits.push(`KV ${Math.round(m.kv_cache_usage_ratio * 100)}%`);
      if (m.requests_processing != null) bits.push(`${m.requests_processing} active`);
      if (lh.tokens_per_sec != null) bits.push(`${lh.tokens_per_sec} tok/s`);
    }
    cardDefs.push({ id: 'llamacpp', html: `
    <div class="card card-link" onclick="showPage('llamacpp')">
      <div class="card-head"><span class="status-dot ${up ? 'green' : 'red'}"></span>llama.cpp</div>
      <div class="card-value">${up ? 'up' : 'down'}</div>
      <div class="card-sub">${up ? bits.join(' · ') : 'server not responding'}</div>
      ${up && m.tokens_predicted_total != null ? `<div class="card-sub">${m.tokens_predicted_total} tokens generated</div>` : ''}
    </div>` });
  }

  // GPU card — only when the module is enabled AND tooling is present. Fetched
  // separately (nvidia-smi/rocm-smi) so /api/summary stays cheap on GPU-less hosts.
  if (moduleEnabled['gpu'] !== false) {
    let gpu = null;
    try { gpu = await API.get('/api/gpu'); } catch (e) {}
    if (gpu && gpu.available && (gpu.gpus || []).length) {
      const g0 = gpu.gpus[0], n = gpu.gpus.length;
      const bits = [];
      if (g0.mem_pct != null) bits.push(`${Math.round(g0.mem_pct)}% VRAM`);
      if (g0.temp != null) bits.push(`${Math.round(g0.temp)}°C`);
      if (g0.power != null) bits.push(`${Math.round(g0.power)}W`);
      cardDefs.push({ id: 'gpu', html: `
    <div class="card card-link" onclick="showPage('gpu')">
      <div class="card-head"><span class="status-dot green"></span>GPU</div>
      <div class="card-value">${g0.util != null ? Math.round(g0.util) : 0}<span class="card-unit">% util</span></div>
      ${usageBar(g0.util || 0)}
      <div class="card-sub">${escapeHtml(g0.name || 'GPU')}${n > 1 ? ` +${n - 1} more` : ''}</div>
      <div class="card-sub">${bits.join(' · ')}</div>
    </div>` });
    }
  }

  // DLNA Media card — only when the module is enabled. Media-library counts are
  // fetched separately (reads minidlna's files.db) so /api/summary stays cheap.
  if (moduleEnabled['minidlna'] !== false) {
    const up = svc.minidlna && svc.minidlna.active === 'active';
    let lib = null;
    try { lib = (await API.get('/api/minidlna/stats')).library; } catch (e) {}
    const has = lib && lib.available;
    cardDefs.push({ id: 'minidlna', html: `
    <div class="card card-link" onclick="showPage('minidlna')">
      <div class="card-head"><span class="status-dot ${up ? dot('minidlna') : 'red'}"></span>DLNA Media</div>
      <div class="card-value">${has ? Number(lib.objects || 0).toLocaleString() : (up ? 'up' : 'down')}${has ? ' <span class="card-unit">items</span>' : ''}</div>
      <div class="card-sub">${has ? `${Number(lib.audio || 0).toLocaleString()} audio · ${Number(lib.video || 0).toLocaleString()} video · ${Number(lib.image || 0).toLocaleString()} image` : 'MiniDLNA media server'}</div>
      ${has ? `<div class="card-sub">${fmtBytesIEC(lib.size)} database</div>` : ''}
    </div>` });
  }

  const cards = `<div class="cards">${
    cardDefs.filter(c => moduleEnabled[c.id] !== false).map(c => c.html).join('')
  }</div>`;

  $('page-content').innerHTML = `
    <h2>Dashboard</h2>
    <div class="info-row">
      <span>Host: <strong>${escapeHtml(sys.hostname || '-')}</strong></span>
      <span>IP: <strong>${escapeHtml(sys.ip || '-')}</strong></span>
      <span>Uptime: <strong>${sys.uptime_days || 0} d</strong></span>
    </div>
    ${welcome}
    ${health}
    ${cards}
    ${resourcesPanel(res)}
  `;
  fillResourceSparks();
}

// ─── Disks ──────────────────────────────────────────────
let _fsBases = ['/mnt', '/media'];

function fmtBytes(n) {
  n = Number(n) || 0;
  const u = ['B','K','M','G','T','P'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + u[i];
}
// IEC / binary units with a space (e.g. "14.2 MiB") — matches minidlna's own
// reporting style for the media-database size.
function fmtBytesIEC(n) {
  n = Number(n) || 0;
  const u = ['B','KiB','MiB','GiB','TiB','PiB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + ' ' + u[i];
}

// The snapshot name (dataset@snap) is a <path:> route segment, so it is used raw
// in the URL path (slashes intact); only the query value is encoded.
function renderMaintenance() {
  const banner = maintTimerActive
    ? '<div class="health-ok">✓ Scheduled maintenance is ON (hourly timer active)</div>'
    : '<div class="alert alert-info">Scheduled maintenance is OFF — it runs once you save at least one schedule below.</div>';
  const poolOpts = maintPools.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join('') || '<option value="">(no pools)</option>';
  const diskOpts = maintDisks.map(d => `<option value="${escapeHtml(d)}">${escapeHtml(d)}</option>`).join('') || '<option value="">(no disks)</option>';
  const scrubRows = maintState.scrubs.map((s, i) => `<tr>
      <td><code>${escapeHtml(s.pool)}</code></td><td>${escapeHtml(s.freq)}</td>
      <td>${escapeHtml(s.last_run || 'never')}</td>
      <td><button class="btn btn-sm btn-danger" onclick="maintRemoveScrub(${i})">Remove</button></td></tr>`).join('');
  const smartRows = maintState.smart.map((s, i) => `<tr>
      <td><code>${escapeHtml(s.device)}</code></td><td>${escapeHtml(s.type)}</td><td>${escapeHtml(s.freq)}</td>
      <td>${escapeHtml(s.last_run || 'never')}</td>
      <td><button class="btn btn-sm" onclick="maintSmartNow('${jsArg(s.device)}','${jsArg(s.type)}')">Run now</button>
          <button class="btn btn-sm btn-danger" onclick="maintRemoveSmart(${i})">Remove</button></td></tr>`).join('');
  $('page-content').innerHTML = `
    <h2>Maintenance</h2>
    ${banner}
    <h3>Scrub schedules</h3>
    <table class="table"><thead><tr><th>Pool</th><th>Frequency</th><th>Last run</th><th></th></tr></thead>
      <tbody>${scrubRows || '<tr><td colspan="4">No scrub schedules</td></tr>'}</tbody></table>
    <div class="toolbar">
      <select id="ms-pool" class="form-control" style="width:auto">${poolOpts}</select>
      <select id="ms-freq" class="form-control" style="width:auto"><option value="monthly">monthly</option><option value="weekly">weekly</option></select>
      <button class="btn btn-sm" onclick="maintAddScrub()">+ Add scrub</button>
    </div>
    <h3 style="margin-top:24px">SMART self-test schedules</h3>
    <table class="table"><thead><tr><th>Disk</th><th>Type</th><th>Frequency</th><th>Last run</th><th></th></tr></thead>
      <tbody>${smartRows || '<tr><td colspan="5">No SMART schedules</td></tr>'}</tbody></table>
    <div class="toolbar">
      <select id="mt-dev" class="form-control" style="width:auto">${diskOpts}</select>
      <select id="mt-type" class="form-control" style="width:auto"><option value="short">short</option><option value="long">long</option></select>
      <select id="mt-freq" class="form-control" style="width:auto"><option value="weekly">weekly</option><option value="daily">daily</option><option value="monthly">monthly</option></select>
      <button class="btn btn-sm" onclick="maintAddSmart()">+ Add SMART test</button>
    </div>
    <div class="toolbar" style="margin-top:16px"><button class="btn" onclick="maintSave()">Save schedules</button></div>
    <p class="help">Scrubs verify pool data integrity; SMART self-tests check drive health — the two checks that catch
      silent rot early. A degraded pool or SMART failure then shows on the dashboard and fires a notification.
      Saving with ≥1 schedule enables the hourly timer.</p>`;
}

function fmtTs(sec) {
  if (!sec) return '-';
  try { return new Date(sec * 1000).toLocaleString(); } catch (e) { return '-'; }
}

async function taskRun(id) {
  try { await API.post(`/api/tasks/${encodeURIComponent(id)}/run`, {}); setTimeout(page_tasks, 900); }
  catch (e) { alert(e.message); }
}

// ─── Log viewer ─────────────────────────────────────────
async function logsRefresh() {
  const out = $('log-output');
  if (!out) return;
  const src = $('log-source').value, pri = $('log-priority').value;
  const lines = $('log-lines').value, grep = $('log-grep').value.trim();
  out.textContent = 'Loading…';
  const qs = new URLSearchParams({ source: src, lines });
  if (pri) qs.set('priority', pri);
  if (grep) qs.set('grep', grep);
  try {
    const r = await API.get('/api/logs/query?' + qs.toString());
    out.textContent = r.logs || 'No log entries.';
    out.scrollTop = out.scrollHeight;
  } catch (e) { out.textContent = 'Error: ' + e.message; }
}

async function svcAction(service, action) {
  try {
    await API.post(`/api/service/${service}/${action}`, {});
    page_services();
  } catch(e) { alert(e.message); }
}

async function svcLogs(service) {
  try {
    const r = await API.get(`/api/logs/${service}`);
    openModal(`Logs: ${service}`, `<pre class="raw-output" style="max-height:500px;overflow:auto">${escapeHtml(r.logs || 'No logs')}</pre>`);
  } catch(e) { alert(e.message); }
}

// ─── Settings / TLS ─────────────────────────────────────
// System submenu pages — split out of the old monolithic Settings page.
function adminOnlyPage(title) {
  $('page-content').innerHTML = `<h2>${title}</h2><div class="alert alert-warning">Administrator access required.</div>`;
}

// "My Account": change your own password — available to every user (incl.
// read-only), which is why it's separate from the admin-only Users page.
async function toggleModule(id, enabled) {
  try {
    await API.post('/api/modules', { id, enabled });
    await applyModules();
  } catch (e) {
    alert(e.message);
    page_modules();  // re-sync the switches with server state
  }
}

// ─── LLama.cpp ──────────────────────────────────────────
let _llamaArgs = [];
let _llamaPresets = [];   // [{name, model, args}]
let _llamaModels = [];

async function fillTokRateSpark() {
  const el = document.getElementById('spark-tokrate');
  if (!el) return;
  try {
    const h = await API.get('/api/history?metric=llama_tokens_total&since=86400');
    const p = h.points || [];
    const rate = [];
    for (let i = 1; i < p.length; i++) {
      const dt = p[i][0] - p[i - 1][0], dv = p[i][1] - p[i - 1][1];
      if (dt > 0 && dv >= 0) rate.push([p[i][0], dv / dt]);
    }
    el.innerHTML = sparkline(rate);
  } catch (e) {}
}

async function auditRefresh() {
  const el = $('audit-body');
  if (!el) return;
  let data;
  try { data = await API.get('/api/audit?limit=200'); }
  catch(e) { el.innerHTML = `<p class="help">Could not load audit log: ${escapeHtml(e.message)}</p>`; return; }
  const entries = data.entries || [];
  if (!entries.length) { el.innerHTML = '<p class="help">No audit entries yet.</p>'; return; }
  const badge = r => r === 'ok' ? 'green' : (r === 'denied' ? 'yellow' : 'gray');
  const rows = entries.map(e => {
    const tgt = (e.target && Object.keys(e.target).length) ? ' ' + JSON.stringify(e.target) : '';
    return `<tr>
      <td><code>${escapeHtml(e.ts || '-')}</code></td>
      <td>${escapeHtml(e.user || '-')}</td>
      <td>${escapeHtml(e.ip || '-')}</td>
      <td><code>${escapeHtml(e.method || '')} ${escapeHtml(e.path || '')}${escapeHtml(tgt)}</code></td>
      <td><span class="status-badge ${badge(e.result)}">${escapeHtml(e.result || '')} ${e.status || ''}</span></td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table class="table">
      <thead><tr><th>Time</th><th>User</th><th>IP</th><th>Action</th><th>Result</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function tlsUploadCert() {
  const cert = $('tls-cert').value.trim();
  const key = $('tls-key').value.trim();
  if (!cert || !key) { alert('Paste both the certificate and the private key'); return; }
  try {
    const r = await API.post('/api/tls/cert', { cert, key });
    if (r.success) alert('Certificate saved. Restart the dashboard service on this node to apply it.');
    page_certificate();
  } catch(e) { alert(e.message); }
}

async function tlsRegenerate() {
  if (!confirm('Generate a new self-signed certificate? This replaces the current one.')) return;
  try {
    const r = await API.post('/api/tls/regenerate', {});
    if (r.success) alert('New self-signed certificate generated. Restart the dashboard service on this node to apply it.');
    page_certificate();
  } catch(e) { alert(e.message); }
}

// ─── Snapshot schedules ─────────────────────────────────
const SNAP_FREQS = ['hourly', 'daily', 'weekly', 'monthly'];
let snapSchedules = [];

async function userDoCreate() {
  const username = $('nu-name').value.trim(), password = $('nu-pass').value, role = $('nu-role').value, smb = $('nu-smb').checked;
  if (!username || !password) { alert('Username and password required'); return; }
  try {
    const r = await API.post('/api/users', { username, password, role, smb });
    if (!r.success) { alert(r.error || 'Failed'); return; }
    closeModal(); page_users();
  } catch(e) { alert(e.message); }
}
async function userSetRole(username, role) {
  if (!confirm(`Change ${username} to ${role}?`)) return;
  try { const r = await API.post(`/api/users/${encodeURIComponent(username)}/role`, { role }); if (!r.success) { alert(r.error || 'Failed'); return; } page_users(); } catch(e) { alert(e.message); }
}
async function userDoPassword(username) {
  const password = $('up-pass').value;
  if (!password) { alert('Password required'); return; }
  try { const r = await API.post(`/api/users/${encodeURIComponent(username)}/password`, { password }); if (!r.success) { alert(r.error || 'Failed'); return; } closeModal(); alert('Password updated.'); } catch(e) { alert(e.message); }
}
async function userDelete(username) {
  if (!confirm(`Delete dashboard user "${username}"?`)) return;
  try { const r = await API.delete(`/api/users/${encodeURIComponent(username)}`); if (!r.success) { alert(r.error || 'Failed'); return; } page_users(); } catch(e) { alert(e.message); }
}

// ─── API tokens ─────────────────────────────────────────
async function tokenDoCreate() {
  const name = $('tk-name').value.trim();
  const role = $('tk-role').value;
  if (!name) { alert('Name required'); return; }
  try {
    const r = await API.post('/api/tokens', { name, role });
    if (!r.success) { alert(r.error || 'Failed'); return; }
    openModal('Token created — copy it now', `
      <div class="alert alert-warning"><strong>This is shown only once.</strong> Store it somewhere safe;
        it can't be retrieved again (only revoked).</div>
      <div class="form-group"><label>Token for <strong>${escapeHtml(r.name)}</strong> (${escapeHtml(r.role)})</label>
        <textarea class="form-control" rows="2" readonly onclick="this.select()">${escapeHtml(r.token)}</textarea></div>
      <p class="help">Use it as a header: <code>Authorization: Bearer ${escapeHtml(r.token)}</code></p>
      <button class="btn" onclick="closeModal(); page_users();">Done</button>`);
  } catch(e) { alert(e.message); }
}

async function tokenDelete(id, name) {
  if (!confirm(`Revoke API token "${name}"? Any script using it will stop working.`)) return;
  try { const r = await API.delete(`/api/tokens/${encodeURIComponent(id)}`); if (!r.success) { alert(r.error || 'Failed'); return; } page_users(); } catch(e) { alert(e.message); }
}

// ─── Notifications ──────────────────────────────────────
async function notifSave() {
  const body = {
    email: {
      enabled: $('nf-email-en').checked, host: $('nf-host').value.trim(),
      port: parseInt($('nf-port').value) || 587, security: $('nf-sec').value,
      username: $('nf-user').value.trim(), password: $('nf-pass').value,
      from: $('nf-from').value.trim(), to: $('nf-to').value.trim(),
    },
    webhook: { enabled: $('nf-web-en').checked, url: $('nf-url').value.trim() },
  };
  try {
    const r = await API.post('/api/notifications', body);
    if (!r.success) { alert(r.error || 'Failed'); return; }
    $('nf-result').innerHTML = '<span style="color:#6c6">✓ Saved.</span>';
  } catch(e) { alert(e.message); }
}

async function notifTest() {
  $('nf-result').textContent = 'Sending test (using the last saved config)…';
  try {
    const r = await API.post('/api/notifications/test', {});
    if (r.success) { $('nf-result').innerHTML = '<span style="color:#6c6">✓ Test sent.</span>'; return; }
    const detail = (r.results || []).map(x => `${x.channel}: ${x.ok ? 'ok' : escapeHtml(x.error)}`).join(' · ');
    $('nf-result').innerHTML = `<span style="color:#e66">✗ ${escapeHtml(detail || r.error || 'failed')}</span>`;
  } catch(e) { $('nf-result').innerHTML = `<span style="color:#e66">✗ ${escapeHtml(e.message)}</span>`; }
}

// ─── Authentication ─────────────────────────────────────
function showLogin() {
  isAuthed = false;
  document.querySelector('.sidebar').style.display = 'none';
  document.querySelector('.content').style.display = 'none';
  closeModal();
  $('login-screen').style.display = 'flex';
  $('login-pass').value = '';
  $('login-user').focus();
}

async function showApp(user, fqdn, role, mustChange) {
  isAuthed = true;
  currentRole = role || 'admin';
  $('login-screen').style.display = 'none';
  document.querySelector('.sidebar').style.display = '';
  document.querySelector('.content').style.display = '';
  document.body.classList.toggle('readonly', currentRole !== 'admin');
  currentUser = user || '';
  if (fqdn) $('sidebar-title').textContent = fqdn;
  $('account-user').textContent = user ? `Signed in as ${user}${currentRole !== 'admin' ? ' · read-only' : ''}` : '';
  await applyModules();   // load module state before the dashboard first renders
  showPage('dashboard');
  if (mustChange) forcePasswordChange();
}

// First-run: force the bootstrap admin to set a real password before anything else.
function forcePasswordChange() {
  modalLocked = true;
  openModal('Set a new password to continue', `
    <div class="alert alert-warning">This account is still using its initial setup password. Choose a new one to continue.</div>
    <div class="form-group"><label>Current password</label><input id="cp-old" type="password" class="form-control" autocomplete="current-password"></div>
    <div class="form-group"><label>New password</label><input id="cp-new" type="password" class="form-control" autocomplete="new-password"></div>
    <div class="form-group"><label>Confirm new password</label><input id="cp-confirm" type="password" class="form-control" autocomplete="new-password"></div>
    <p class="help">Must be at least 8 characters.</p>
    <button class="btn" onclick="doChangePassword(true)">Set Password</button>`);
}

async function doLogin(e) {
  e.preventDefault();
  const errEl = $('login-error');
  errEl.style.display = 'none';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ username: $('login-user').value.trim(), password: $('login-pass').value })
    });
    const j = await r.json();
    if (!r.ok || !j.success) {
      errEl.textContent = j.error || 'Login failed';
      errEl.style.display = 'block';
      return;
    }
    showApp(j.user, j.fqdn, j.role, j.must_change);
  } catch (err) {
    errEl.textContent = 'Login failed';
    errEl.style.display = 'block';
  }
}

async function doLogout(e) {
  if (e) e.preventDefault();
  try { await fetch('/api/logout', { method: 'POST' }); } catch (err) {}
  showLogin();
}

function changePassword(e) {
  if (e) e.preventDefault();
  openModal('Change Password', `
    <div class="form-group"><label>Current password</label><input id="cp-old" type="password" class="form-control" autocomplete="current-password"></div>
    <div class="form-group"><label>New password</label><input id="cp-new" type="password" class="form-control" autocomplete="new-password"></div>
    <div class="form-group"><label>Confirm new password</label><input id="cp-confirm" type="password" class="form-control" autocomplete="new-password"></div>
    <p class="help">Must be at least 8 characters.</p>
    <button class="btn" onclick="doChangePassword()">Update Password</button>
  `);
}

async function doChangePassword(forced) {
  const oldp = $('cp-old').value, newp = $('cp-new').value, confirm = $('cp-confirm').value;
  if (newp !== confirm) { alert('New passwords do not match'); return; }
  try {
    await API.post('/api/account/password', { old_password: oldp, new_password: newp });
    modalLocked = false;
    closeModal();
    alert('Password updated.');
    if (forced) showPage('dashboard');
  } catch (err) { alert(err.message); }
}

async function checkAuth() {
  try {
    const r = await fetch('/api/me');
    if (!r.ok) { showLogin(); return; }
    const j = await r.json();
    showApp(j.user, j.fqdn, j.role, j.must_change);
  } catch (err) { showLogin(); }
}

// ─── Init ───────────────────────────────────────────────
// Set when this page was opened via a network-handoff link on the *new* IP
// (so we're known to be reachable there) — drives the "Finalize" primary action.
let cameFromHandoff = false;

async function bootstrap() {
  applyThemeLabel();
  const params = new URLSearchParams(window.location.search);
  const h = params.get('nethandoff');
  if (h) {
    try {
      const r = await fetch('/api/network/handoff', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: h })
      });
      const j = await r.json().catch(() => ({}));
      history.replaceState(null, '', window.location.pathname);  // never leave the token in the URL
      if (r.ok && j.success) {
        cameFromHandoff = true;
        await showApp(j.user, j.fqdn, j.role, false);
        showPage('network');
        return;
      }
    } catch (e) {
      history.replaceState(null, '', window.location.pathname);
    }
  }
  checkAuth();
}
bootstrap();

// Auto-refresh dashboard every 10s
setInterval(async () => {
  if (!isAuthed) return;
  // Refresh the dashboard metrics when it's open and no modal is in the way.
  const active = document.querySelector('.nav-list a.active');
  if (active && active.dataset.page === 'dashboard' && $('modal-overlay').style.display === 'none') {
    try { await page_dashboard(); } catch(e) {}
  }
}, 30000);
