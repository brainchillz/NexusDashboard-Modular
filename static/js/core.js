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
let currentPage = 'dashboard';   // survives nav re-renders (active-link restore)
function showPage(id) { currentPage = id; document.querySelectorAll('.nav-list a').forEach(a => a.classList.toggle('active', a.dataset.page === id)); renderPage(id); }
function escapeHtml(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
// Inline stroke icon from the symbol set in index.html (currentColor-inheriting).
function icon(name, cls) { return `<svg class="ico ${cls || ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`; }
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
// The sidebar is rendered at runtime from /api/modules/nav (renderNav below),
// so clicks are DELEGATED on the <ul> — it exists (empty) at parse time,
// while the <a> elements don't yet.
document.querySelector('.nav-list').addEventListener('click', e => {
  const a = e.target.closest('a[data-page]');
  if (!a) return;
  e.preventDefault();
  showPage(a.dataset.page);
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

// Manifest strings are third-party input once plugins exist: identifiers are
// allowlist-validated before ANY interpolation into selectors/attributes/
// onclick; free text (labels) always goes through escapeHtml.
const NAV_ID_RE = /^[A-Za-z0-9_-]+$/;
const PAGE_ID_RE = /^[A-Za-z0-9_-]+$/;     // becomes part of window['page_<id>']
                                           // (computed property: '-' is fine)
const SVG_PATH_RE = /^[MmZzLlHhVvCcSsQqTtAa0-9\s,.\-+eE]+$/;  // path data only

function navIcon(it) {
  if (typeof it.icon === 'string' && NAV_ID_RE.test(it.icon) &&
      document.getElementById('i-' + it.icon))
    return `<svg class="ico"><use href="#i-${it.icon}"/></svg>`;
  // Plugins without a stock sprite icon supply raw path data (d= only — the
  // charset allowlist forecloses attribute breakout; path data can't script).
  if (Array.isArray(it.icon_paths) && it.icon_paths.length &&
      it.icon_paths.every(d => typeof d === 'string' && SVG_PATH_RE.test(d)))
    return `<svg class="ico" viewBox="0 0 24 24">${it.icon_paths.map(d => `<path d="${d}"/>`).join('')}</svg>`;
  return `<svg class="ico"><use href="#i-pkg"/></svg>`;
}

// Render the whole sidebar from the nav manifest. Emits the exact DOM shape
// of the old static nav (classes/attributes) so style.css and the
// module-visibility selectors work unchanged. The Dashboard link is
// renderer-emitted, not manifest data — a broken manifest can never remove
// the one guaranteed page.
function renderNav(nav) {
  const ul = document.querySelector('.nav-list');
  const cats = ((nav && nav.categories) || []).slice()
    .sort((a, b) => (a.order || 0) - (b.order || 0));
  let html = `<li><a href="#" data-page="dashboard"><svg class="ico"><use href="#i-grid"/></svg> Dashboard</a></li>`;
  for (const c of cats) {
    if (!NAV_ID_RE.test(String(c.cat || ''))) continue;
    const items = (c.items || []).filter(it =>
      PAGE_ID_RE.test(String(it.page || '')) &&
      (!it.module || NAV_ID_RE.test(String(it.module))));
    if (!items.length) continue;
    const lis = items.map(it =>
      `<li${it.admin_only ? ' class="nav-admin-only"' : ''}${
         it.module && it.module !== it.page ? ` data-module="${it.module}"` : ''
       }><a href="#" data-page="${it.page}">${navIcon(it)} ${escapeHtml(it.label || it.page)}</a></li>`
    ).join('');
    html += `<li class="nav-group" data-cat="${c.cat}">` +
      `<div class="nav-cat" onclick="toggleCat('${c.cat}')">${escapeHtml(c.label || c.cat)} <span class="caret">▾</span></div>` +
      `<ul class="nav-sub">${lis}</ul></li>`;
  }
  ul.innerHTML = html;
  restoreNavCats();   // re-apply persisted collapsed state to the fresh groups
  document.querySelectorAll('.nav-list a').forEach(a =>
    a.classList.toggle('active', a.dataset.page === currentPage));
}

// ─── Feature modules (nav + visibility orchestration) ───
// applyModules() is the UI-manifest orchestrator: fetch /api/modules/nav
// (cache the last good manifest — a fetch failure must not brick the nav),
// render the sidebar, hand plugin assets / declarative pages to their
// loaders (later script files; typeof-guarded), then apply module
// visibility. Runs once at login (before the first page renders) and again
// on every Modules-page toggle.
let moduleEnabled = {};  // id -> bool (enabled AND installed)
let uiManifest = null;
async function applyModules() {
  let man = null;
  try {
    man = await API.get('/api/modules/nav');
    try { localStorage.setItem('uiManifest.v1', JSON.stringify(man)); } catch (e) {}
  } catch (e) {
    try { man = JSON.parse(localStorage.getItem('uiManifest.v1') || 'null'); } catch (e2) {}
  }
  if (!man) {   // no server, no cache: minimal shell + visible error
    renderNav({ categories: [] });
    $('page-content').innerHTML =
      '<div class="error">Could not load the module list — navigation is unavailable. Reload to retry.</div>';
    return;
  }
  uiManifest = man;
  renderNav(man.nav);
  if (typeof loadPluginAssets === 'function') loadPluginAssets(man.modules);
  if (typeof registerDeclarativePages === 'function') registerDeclarativePages(man.modules);
  applyModuleVisibility(man.modules || []);
}

// ─── Plugin asset delivery ──────────────────────────────
// Plugin JS/CSS arrive as appended classic <script>/<link> tags (no build
// step, no ES modules — a parse error in one plugin file kills only that
// file). Safe because window['page_<id>'] is resolved lazily at click time:
// a plugin script only has to have PARSED before its page is first opened,
// and injection starts before the dashboard (always the first page) renders.
const _loadedAssets = new Set();
let _pendingScripts = 0;

function _assetUrlOk(u) {   // same-origin absolute paths only
  return typeof u === 'string' && u.startsWith('/') && !u.startsWith('//');
}

function loadPluginAssets(mods) {
  (mods || []).forEach(m => {
    if (!m.assets || m.enabled === false || m.installed === false) return;
    (m.assets.css || []).forEach(u => {
      if (!_assetUrlOk(u) || _loadedAssets.has(u)) return;
      _loadedAssets.add(u);
      const l = document.createElement('link');
      l.rel = 'stylesheet'; l.href = u;
      document.head.appendChild(l);
    });
    (m.assets.js || []).forEach(u => {
      if (!_assetUrlOk(u) || _loadedAssets.has(u)) return;
      _loadedAssets.add(u);
      _pendingScripts++;
      const s = document.createElement('script');
      s.src = u;
      s.async = false;    // preserves order among a plugin's own files
      s.onload = s.onerror = ev => {
        _pendingScripts--;
        if (ev.type === 'error') console.warn('plugin asset failed:', u);
        // If the user beat the script to its own page, re-render it now.
        if (document.querySelector('#page-content .plugin-wait') &&
            typeof window['page_' + currentPage] === 'function') renderPage(currentPage);
      };
      document.body.appendChild(s);
    });
  });
}

function applyModuleVisibility(mods) {
  moduleEnabled = {};
  mods.forEach(m => {
    if (!NAV_ID_RE.test(String(m.id || ''))) return;
    // Not-installed (a plugin whose load failed / dir removed) hides from nav
    // exactly like disabled; the Modules page shows the distinct third state.
    const hidden = m.enabled === false || m.installed === false;
    moduleEnabled[m.id] = m.enabled !== false && m.installed !== false;
    const a = document.querySelector(`.nav-list a[data-page="${m.id}"]`);
    if (a && a.parentElement) a.parentElement.classList.toggle('module-hidden', hidden);
    // Multi-page modules (one toggle, several nav entries) mark each <li>
    // with data-module=<id> since data-page != the module id.
    document.querySelectorAll(`.nav-list li[data-module="${m.id}"]`).forEach(
      li => li.classList.toggle('module-hidden', hidden));
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
  // A page whose nav entry AND page function both vanished (plugin removed
  // server-side between manifests) also falls back.
  if (currentPage !== 'dashboard' &&
      !document.querySelector(`.nav-list a[data-page="${currentPage}"]`) &&
      typeof window['page_' + currentPage] !== 'function') showPage('dashboard');
}

let _renderSeq = 0;   // bumps per navigation; slow async pages check it
                      // before writing so a stale render can't clobber the
                      // page the user has since navigated to
let _pageCleanup = null;   // per-page teardown (widget refresh timers, …) —
                           // the page-level twin of _onModalClose
async function renderPage(page) {
  _renderSeq++;
  const cleanup = _pageCleanup; _pageCleanup = null;
  if (cleanup) try { cleanup(); } catch (e) {}
  $('page-content').innerHTML = '<div class="loading">Loading...</div>';
  // Reset scroll: .content scrolls independently, and the window itself
  // scrolls when the sidebar is taller than the viewport — reset both, or a
  // page opened while scrolled down starts below its own top.
  const content = document.querySelector('.content');
  if (content) content.scrollTop = 0;
  window.scrollTo(0, 0);
  try {
    if (typeof window['page_' + page] === 'function') await window['page_' + page]();
    else if (_pendingScripts > 0)
      // a plugin script that defines this page may still be parsing; its
      // onload handler re-renders when it lands
      $('page-content').innerHTML = '<div class="loading plugin-wait">Loading plugin…</div>';
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


// ─── Dashboard cards ────────────────────────────────────
// A module contributes a card by defining a global `dashcard_<id>(ctx)`
// returning an HTML string ('' = no card this render) — the same
// name-convention-resolved-lazily idiom as page_*. ctx: {s (the /api/summary
// payload), svc, dot}. Declarative plugins with no JS get a card from their
// manifest's dashboard_card instead (rendered by widgets.js).
// BUILTIN_CARD_ORDER is ordering data, not wiring — plugin cards need no
// edit here; they append after, in manifest order.
const BUILTIN_CARD_ORDER = ['zfs', 'iscsi', 'nfs', 'smb', 'disks',
                            'llamacpp', 'gpu', 'minidlna'];

function dashcard_zfs(ctx) {
  const z = ctx.s.zfs || {}, dot = ctx.dot;
  return `
    <div class="card card-link" onclick="showPage('zfs')">
      <div class="card-head"><span class="status-dot ${z.online ? dot('zfs') : 'red'}"></span>ZFS Pools</div>
      <div class="card-value">${z.pools || 0} <span class="card-unit">pool${z.pools === 1 ? '' : 's'}</span></div>
      <div class="card-sub">${escapeHtml(z.used || '0')} / ${escapeHtml(z.size || '0')}${z.scanning ? ' · scrubbing' : ''}</div>
      ${z.pools ? usageBar(z.pct || 0) : ''}
    </div>`;
}

function dashcard_iscsi(ctx) {
  const isc = ctx.s.iscsi || {}, dot = ctx.dot;
  return `
    <div class="card card-link" onclick="showPage('iscsi')">
      <div class="card-head"><span class="status-dot ${dot('iscsi')}"></span>iSCSI</div>
      <div class="card-value">${isc.targets || 0} <span class="card-unit">target${isc.targets === 1 ? '' : 's'}</span></div>
      <div class="card-sub">${isc.luns || 0} LUNs · ${isc.sessions || 0} connected</div>
      <div class="card-sub">${escapeHtml(isc.provisioned || '0B')} provisioned</div>
    </div>`;
}

function dashcard_nfs(ctx) {
  const nf = ctx.s.nfs || {}, dot = ctx.dot;
  return `
    <div class="card card-link" onclick="showPage('nfs')">
      <div class="card-head"><span class="status-dot ${dot('nfs')}"></span>NFS</div>
      <div class="card-value">${nf.exports || 0} <span class="card-unit">export${nf.exports === 1 ? '' : 's'}</span></div>
      <div class="card-sub">${nf.clients || 0} client mount${nf.clients === 1 ? '' : 's'}</div>
    </div>`;
}

function dashcard_smb(ctx) {
  const sm = ctx.s.smb || {}, dot = ctx.dot;
  return `
    <div class="card card-link" onclick="showPage('smb')">
      <div class="card-head"><span class="status-dot ${dot('smb')}"></span>SMB</div>
      <div class="card-value">${sm.shares || 0} <span class="card-unit">share${sm.shares === 1 ? '' : 's'}</span></div>
      <div class="card-sub">${sm.users || 0} users · ${sm.connections || 0} connections</div>
    </div>`;
}

function dashcard_disks(ctx) {
  const dk = ctx.s.disks || {};
  const smartLabel = dk.smart_ok === false ? 'SMART FAIL' : (dk.smart_ok === null ? 'SMART n/a' : 'SMART OK');
  return `
    <div class="card card-link" onclick="showPage('disks')">
      <div class="card-head"><span class="status-dot ${dk.smart_ok === false ? 'red' : 'green'}"></span>Disks</div>
      <div class="card-value">${dk.total || 0} <span class="card-unit">disks</span></div>
      <div class="card-sub">${dk.free || 0} free · ${smartLabel}</div>
    </div>`;
}

// llama.cpp health/metrics card. Fetched separately (it pings llama-server's
// /health + /metrics) so /api/summary stays cheap and nothing runs when the
// module is off (collectDashboardCards skips disabled modules' cards).
async function dashcard_llamacpp() {
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
  return `
    <div class="card card-link" onclick="showPage('llamacpp')">
      <div class="card-head"><span class="status-dot ${up ? 'green' : 'red'}"></span>llama.cpp</div>
      <div class="card-value">${up ? 'up' : 'down'}</div>
      <div class="card-sub">${up ? bits.join(' · ') : 'server not responding'}</div>
      ${up && m.tokens_predicted_total != null ? `<div class="card-sub">${m.tokens_predicted_total} tokens generated</div>` : ''}
    </div>`;
}

// GPU card — only when tooling is present ('' otherwise). Fetched separately
// (nvidia-smi/rocm-smi) so /api/summary stays cheap on GPU-less hosts.
async function dashcard_gpu() {
  let gpu = null;
  try { gpu = await API.get('/api/gpu'); } catch (e) {}
  if (!(gpu && gpu.available && (gpu.gpus || []).length)) return '';
  const g0 = gpu.gpus[0], n = gpu.gpus.length;
  const bits = [];
  if (g0.mem_pct != null) bits.push(`${Math.round(g0.mem_pct)}% VRAM`);
  if (g0.temp != null) bits.push(`${Math.round(g0.temp)}°C`);
  if (g0.power != null) bits.push(`${Math.round(g0.power)}W`);
  return `
    <div class="card card-link" onclick="showPage('gpu')">
      <div class="card-head"><span class="status-dot green"></span>GPU</div>
      <div class="card-value">${g0.util != null ? Math.round(g0.util) : 0}<span class="card-unit">% util</span></div>
      ${usageBar(g0.util || 0)}
      <div class="card-sub">${escapeHtml(g0.name || 'GPU')}${n > 1 ? ` +${n - 1} more` : ''}</div>
      <div class="card-sub">${bits.join(' · ')}</div>
    </div>`;
}

// DLNA Media card. Media-library counts are fetched separately (reads
// minidlna's files.db) so /api/summary stays cheap.
async function dashcard_minidlna(ctx) {
  const svc = ctx.svc, dot = ctx.dot;
  const up = svc.minidlna && svc.minidlna.active === 'active';
  let lib = null;
  try { lib = (await API.get('/api/minidlna/stats')).library; } catch (e) {}
  const has = lib && lib.available;
  return `
    <div class="card card-link" onclick="showPage('minidlna')">
      <div class="card-head"><span class="status-dot ${up ? dot('minidlna') : 'red'}"></span>DLNA Media</div>
      <div class="card-value">${has ? Number(lib.objects || 0).toLocaleString() : (up ? 'up' : 'down')}${has ? ' <span class="card-unit">items</span>' : ''}</div>
      <div class="card-sub">${has ? `${Number(lib.audio || 0).toLocaleString()} audio · ${Number(lib.video || 0).toLocaleString()} video · ${Number(lib.image || 0).toLocaleString()} image` : 'MiniDLNA media server'}</div>
      ${has ? `<div class="card-sub">${fmtBytesIEC(lib.size)} database</div>` : ''}
    </div>`;
}

async function collectDashboardCards(ctx) {
  const ids = BUILTIN_CARD_ORDER.slice();
  ((uiManifest && uiManifest.modules) || []).forEach(m => {
    if (!ids.includes(m.id) &&
        (typeof window['dashcard_' + m.id] === 'function' || m.dashboard_card))
      ids.push(m.id);
  });
  const out = [];
  for (const id of ids) {
    if (moduleEnabled[id] === false) continue;   // disabled module: no card
    try {
      const fn = window['dashcard_' + id];
      let html = '';
      if (typeof fn === 'function') html = await fn(ctx);
      else if (typeof renderDeclarativeCard === 'function') {
        const m = ((uiManifest && uiManifest.modules) || []).find(x => x.id === id);
        if (m && m.dashboard_card) html = await renderDeclarativeCard(m.id, m.dashboard_card, ctx);
      }
      if (html) out.push(html);
    } catch (e) {}   // one bad card must never blank the dashboard
  }
  return out;
}

async function page_dashboard() {
  const seq = _renderSeq;
  const [s, res] = await Promise.all([
    API.get('/api/summary'),
    API.get('/api/system/resources').catch(() => null),
  ]);
  const svc = s.services || {};
  const dot = k => (svc[k] && svc[k].active === 'active') ? 'green' : 'red';
  const sys = s.system || {};
  const z = s.zfs || {};
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

  const cards = `<div class="cards">${
    (await collectDashboardCards({ s, svc, dot })).join('')
  }</div>`;

  // The card fetches above take real time; if the user navigated away in the
  // meantime, writing now would clobber the page they moved to.
  if (seq !== _renderSeq) return;

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


function fmtTs(sec) {
  if (!sec) return '-';
  try { return new Date(sec * 1000).toLocaleString(); } catch (e) { return '-'; }
}



// ─── Settings / TLS ─────────────────────────────────────
// System submenu pages — split out of the old monolithic Settings page.
function adminOnlyPage(title) {
  $('page-content').innerHTML = `<h2>${title}</h2><div class="alert alert-warning">Administrator access required.</div>`;
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
