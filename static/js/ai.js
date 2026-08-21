// llama.cpp page state. These live at the top on purpose: every read happens
// inside a function called after load, but an assignment-before-declaration only
// "works" because sloppy mode invents a global — and the first READ of one that
// was never assigned throws.
let _llamaArgs = [];
let _llamaPresets = [];   // [{name, model, args}]
let _llamaModels = [];
let _hfTokenSet = false;  // presence only — the token itself never reaches the browser
let _hfRate = 0;          // saved default speed cap, Mbps (0 = unlimited)

function llamaPresetOptions() {
  if (!_llamaPresets.length) return '<option value="">(no presets saved)</option>';
  return _llamaPresets.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join('');
}

function llamaArgRows() {
  if (!_llamaArgs.length) return '<tr><td colspan="3" class="help">No arguments set.</td></tr>';
  return _llamaArgs.map((a, i) => `
    <tr class="llama-arg-row">
      <td><input class="form-control" value="${escapeHtml(a.flag || '')}" placeholder="--flag" data-i="${i}" data-k="flag"></td>
      <td><input class="form-control" value="${escapeHtml(a.value || '')}" placeholder="value (blank = boolean flag)" data-i="${i}" data-k="value"></td>
      <td style="width:1%"><button class="btn btn-sm btn-danger" onclick="llamaDelArg(${i})" title="Remove">&times;</button></td>
    </tr>`).join('');
}

// Pull the current input values back into _llamaArgs before any re-render.
function llamaSyncArgs() {
  document.querySelectorAll('.llama-arg-row').forEach(row => {
    const fi = row.querySelector('[data-k="flag"]'), vi = row.querySelector('[data-k="value"]');
    const i = +fi.dataset.i;
    if (_llamaArgs[i]) { _llamaArgs[i].flag = fi.value; _llamaArgs[i].value = vi.value; }
  });
}
function llamaAddArg() { llamaSyncArgs(); _llamaArgs.push({ flag: '', value: '' }); $('llama-args-body').innerHTML = llamaArgRows(); }
function llamaDelArg(i) { llamaSyncArgs(); _llamaArgs.splice(i, 1); $('llama-args-body').innerHTML = llamaArgRows(); }

// ─── GPU tunables ─────────────────────────────────────────────────────
// Power cap and power profile only. Both are reversible and safe to change
// while llama-server is mid-model; clocks and perf determinism are not offered
// for exactly that reason. Anything the card reports as unsupported simply
// does not render a control.
let _gpuTun = null;

async function gpuTunablesHtml() {
  try { _gpuTun = await API.get('/api/gpu/tunables'); }
  catch (e) { return ''; }
  const t = _gpuTun;
  if (!t || !t.vendor) return '';
  if (t.problem && !(t.gpus || []).length) {
    return `<div class="card"><div class="card-head">Tunables</div>
      <p class="help">${escapeHtml(t.problem)}</p></div>`;
  }
  const rows = (t.gpus || []).filter(g => (g.settable || []).length).map(g => {
    const pc = g.power_cap || {};
    const cap = (g.settable.indexOf('power_cap') !== -1) ? `
      <div class="form-group">
        <label>Power cap <span class="help">(${pc.min}–${pc.max} ${pc.unit || 'W'}${
          pc.default != null ? ', default ' + pc.default : ''})</span></label>
        <div class="toolbar" style="margin:0">
          <input id="gputun-cap-${g.index}" class="form-control" type="number"
                 style="max-width:120px" min="${pc.min}" max="${pc.max}" value="${pc.current}">
          <button class="btn btn-sm" onclick="gpuSetCap(${g.index})">Apply</button>
        </div>
      </div>` : '';
    const opts = (g.profile && g.profile.available || []).map(p =>
      `<option value="${escapeHtml(p)}" ${p === g.profile.current ? 'selected' : ''}>${escapeHtml(p)}</option>`).join('');
    const prof = (g.settable.indexOf('profile') !== -1) ? `
      <div class="form-group">
        <label>Power profile</label>
        <div class="toolbar" style="margin:0">
          <select id="gputun-prof-${g.index}" class="form-control" style="max-width:220px">${opts}</select>
          <button class="btn btn-sm" onclick="gpuSetProfile(${g.index})">Apply</button>
        </div>
      </div>` : '';
    const now = g.power_now != null ? `${Math.round(g.power_now)} W now` : '';
    const thr = g.throttled ? ' <span class="status-badge yellow">throttled</span>' : '';
    return `<div class="card">
      <div class="card-head">GPU ${g.index} · ${escapeHtml(g.name || '')}</div>
      <p class="help">${now}${now && pc.current != null ? ' · cap ' + pc.current + ' W' : ''}${thr}
        ${g.slowdown_temp ? ' · slows at ' + g.slowdown_temp + '°C' : ''}</p>
      ${cap}${prof}</div>`;
  }).join('');
  if (!rows) return '';
  return `<h3 style="margin-top:18px">Tunables</h3>
    ${t.helper ? '' : `<div class="alert alert-warning">The GPU tuning helper is not
      installed on this node, so changes cannot be applied. Run
      <code>fleet-deploy.sh --helpers</code>.</div>`}
    <div class="cards">${rows}</div>
    <p class="help">Changes take effect immediately and are safe while a model is loaded.
      They do <strong>not</strong> persist across a reboot. Clock limits, perf determinism
      and partitioning are deliberately not exposed — the first two can destabilise a
      running inference job, and partitioning is an MI300-class feature these cards
      do not implement.</p>`;
}

async function gpuSetCap(index) {
  const el = $('gputun-cap-' + index);
  const watts = parseInt(el && el.value, 10);
  if (!watts) { alert('Enter a power cap in watts.'); return; }
  try {
    await API.post(`/api/gpu/${index}/power-cap`, { watts });
    page_gpu();
  } catch (e) { alert(e.message); }
}

async function gpuSetProfile(index) {
  const el = $('gputun-prof-' + index);
  const profile = el && el.value;
  if (!profile) return;
  if (!confirm(`Set GPU ${index} power profile to ${profile}?`)) return;
  try {
    await API.post(`/api/gpu/${index}/profile`, { profile });
    page_gpu();
  } catch (e) { alert(e.message); }
}

async function page_gpu() {
  const g = await API.get('/api/gpu');
  if (!g.available || !(g.gpus || []).length) {
    $('page-content').innerHTML = `<h2>GPU</h2>
      <div class="alert alert-info">No GPU telemetry available.
      Install <code>nvidia-smi</code> (NVIDIA) or <code>rocm-smi</code> (AMD/ROCm) on this host.</div>`;
    return;
  }
  const vend = g.vendor === 'nvidia' ? 'NVIDIA' : g.vendor === 'amd' ? 'AMD / ROCm' : '';
  const cards = g.gpus.map(gp => {
    const idx = gp.index != null ? gp.index : '?';
    const sub = [];
    if (gp.power != null) sub.push(`${Math.round(gp.power)} W`);
    if (gp.temp != null) sub.push(`${Math.round(gp.temp)}°C`);
    const mem = (gp.mem_used != null && gp.mem_total != null)
      ? `${fmtBytes(gp.mem_used)} / ${fmtBytes(gp.mem_total)}` : '';
    return `
    <div class="card">
      <div class="card-head">GPU ${idx} · ${escapeHtml(gp.name || 'GPU')}</div>
      <div class="res-item"><div class="res-label">Utilization</div>
        ${usageBar(gp.util || 0)}<div class="card-sub">${gp.util != null ? gp.util : '-'}%</div>
        <div id="spark-gpu${idx}"></div></div>
      <div class="res-item"><div class="res-label">VRAM</div>
        ${usageBar(gp.mem_pct || 0)}<div class="card-sub">${gp.mem_pct != null ? Math.round(gp.mem_pct) : '-'}% ${mem ? '· ' + mem : ''}</div></div>
      ${sub.length ? `<div class="card-sub">${sub.join(' · ')}</div>` : ''}
    </div>`;
  }).join('');
  $('page-content').innerHTML = `
    <h2>GPU</h2>
    <div class="info-row"><span>Vendor: <strong>${escapeHtml(vend)}</strong></span>
      <span>Devices: <strong>${g.gpus.length}</strong></span></div>
    <div class="cards">${cards}</div>
    ${await gpuTunablesHtml()}`;
  // Per-GPU utilization sparklines from the history store (last 24h).
  for (const gp of g.gpus) {
    const el = document.getElementById('spark-gpu' + (gp.index != null ? gp.index : '?'));
    if (!el || gp.index == null) continue;
    try {
      const h = await API.get(`/api/history?metric=gpu_util&label=gpu${gp.index}&since=86400`);
      el.innerHTML = sparkline(h.points);
    } catch (e) {}
  }
}

// Link to llama-server's own web UI. The port comes from the configured
// --port flag; the HOST deliberately does not — it is whatever this browser
// used to reach the dashboard, which is the only address we know the client
// can route to. A loopback --host bind means nobody but the node itself can
// open it, so say that instead of offering a link that cannot work.
function llamaWebUiHtml(ui, running) {
  if (!ui) return '';
  if (!running) return `<p class="help">llama-server's web UI (port ${ui.port}) opens once the service is running.</p>`;
  if (!ui.reachable) {
    return `<p class="help">Web UI is bound to <code>${escapeHtml(ui.host)}</code>, so it is reachable
      only from the node itself. Set <code>--host 0.0.0.0</code> in Server Arguments to open it from here.</p>`;
  }
  const url = location.protocol.replace('https:', 'http:') + '//' + location.hostname + ':' + ui.port + '/';
  return `<div class="toolbar"><a class="btn btn-sm btn-outline" href="${escapeHtml(url)}"
     target="_blank" rel="noopener">Open llama.cpp web UI ↗</a></div>`;
}

async function page_llamacpp() {
  const [d, pr, lh, hf] = await Promise.all([
    API.get('/api/llama'),
    API.get('/api/llama/presets').catch(() => ({ presets: [] })),
    API.get('/api/llama/health').catch(() => null),
    API.get('/api/llama/hf').catch(() => ({ token_set: false, rate_mbps: 0 })),
  ]);
  _hfTokenSet = !!hf.token_set;
  _hfRate = hf.rate_mbps || 0;
  _llamaArgs = (d.args || []).map(a => ({ flag: a.flag, value: a.value }));
  _llamaPresets = pr.presets || [];
  _llamaModels = d.models || [];
  const svc = d.service || {};
  const active = svc.active === 'active';
  // Live server metrics (tokens/sec is derived in-memory server-side).
  const hm = (lh && lh.metrics) || {};
  const liveBits = [];
  if (lh && lh.ok) {
    liveBits.push('server ' + escapeHtml(lh.status || 'ok'));
    if (lh.tokens_per_sec != null) liveBits.push(`${lh.tokens_per_sec} tok/s`);
    if (hm.kv_cache_usage_ratio != null) liveBits.push(`KV ${Math.round(hm.kv_cache_usage_ratio * 100)}%`);
    if (hm.requests_processing != null) liveBits.push(`${hm.requests_processing} active`);
    if (hm.tokens_predicted_total != null) liveBits.push(`${hm.tokens_predicted_total} tokens total`);
  }
  const liveLine = liveBits.length ? `<p class="help">Live: ${liveBits.join(' · ')}</p>` : '';
  const warn = d.configured ? '' : `
    <div class="alert alert-warning">
      <strong>llama.cpp isn't fully set up on this host yet.</strong>
      Expected <code>/etc/llama.conf</code>, a <code>llama-server</code> systemd unit,
      and <code>.gguf</code> models under <code>${escapeHtml(d.models_dir)}</code>.
      You can still edit settings below; they apply once the service exists.
    </div>`;
  const modelOpts = (d.models || []).map(m =>
    `<option value="${escapeHtml(m.path)}" ${m.path === d.model ? 'selected' : ''}>${escapeHtml(m.name)}</option>`
  ).join('') || '<option value="">(no .gguf models found)</option>';

  $('page-content').innerHTML = `
    <h2>LLama.cpp</h2>
    ${warn}
    <div class="card">
      <h3>Service</h3>
      <p>Status: <span class="status-badge ${active ? 'green' : 'red'}">${escapeHtml(svc.active || 'unknown')}</span>
         &nbsp;·&nbsp; Boot: <span class="status-badge ${svc.enabled === 'enabled' ? 'green' : 'gray'}">${escapeHtml(svc.enabled || 'disabled')}</span></p>
      ${liveLine}
      ${llamaWebUiHtml(d.web_ui, active)}
      <div class="res-item" style="max-width:220px;margin:0 auto 16px"><div class="res-label">Tokens/sec (24h)</div><div id="spark-tokrate"></div></div>
      <div class="toolbar">
        <button class="btn btn-sm" onclick="llamaSvc('start')">Start</button>
        <button class="btn btn-sm btn-warning" onclick="llamaSvc('stop')">Stop</button>
        <button class="btn btn-sm" onclick="llamaSvc('restart')">Restart</button>
        <button class="btn btn-sm btn-outline" onclick="llamaSvc('enable')">Enable</button>
        <button class="btn btn-sm btn-outline" onclick="llamaSvc('disable')">Disable</button>
      </div>
    </div>
    <div class="card">
      <h3>Model</h3>
      <p class="help">Current: <code>${escapeHtml(d.model || '(none)')}</code></p>
      <div class="toolbar">
        <select id="llama-model" class="form-control" style="max-width:480px">${modelOpts}</select>
        <button class="btn" onclick="llamaSetModel()">Switch Model</button>
        <button class="btn btn-outline" onclick="showPage('models')">Get models…</button>
      </div>
      <p class="help">Models discovered under <code>${escapeHtml(d.models_dir)}</code>. Switching rewrites <code>LLAMA_MODEL</code> in <code>/etc/llama.conf</code> and restarts the server if it's running.</p>
    </div>
    <div class="card">
      <h3>Server Arguments</h3>
      <table class="table"><thead><tr><th>Flag</th><th>Value</th><th></th></tr></thead>
        <tbody id="llama-args-body">${llamaArgRows()}</tbody></table>
      <div class="toolbar">
        <button class="btn btn-sm btn-outline" onclick="llamaAddArg()">Add Argument</button>
        <button class="btn" onclick="llamaSaveArgs()">Save Changes</button>
      </div>
      <p class="help">CLI flags for <code>llama-server</code> (the <code>-m</code> model flag is managed by the Model card above). Saving rewrites <code>LLAMA_OPTS</code> and restarts the server if it's running.</p>
      <hr style="border:none;border-top:1px solid var(--border);margin:14px 0">
      <h4 style="margin-bottom:8px">Profiles</h4>
      <div class="toolbar">
        <select id="llama-preset-select" class="form-control" style="max-width:300px">${llamaPresetOptions()}</select>
        <button class="btn btn-sm" onclick="llamaApplyPreset()">Apply</button>
        <button class="btn btn-sm btn-outline" onclick="llamaLoadPreset()">Load into editor</button>
        <button class="btn btn-sm btn-outline" onclick="llamaExportPreset()">Export</button>
        <button class="btn btn-sm btn-danger" onclick="llamaDeletePreset()">Delete</button>
      </div>
      <div class="toolbar">
        <input id="llama-preset-name" class="form-control" style="max-width:300px" placeholder="New profile name" autocomplete="off">
        <button class="btn btn-sm btn-outline" onclick="llamaSavePreset()">Save current as profile</button>
        <button class="btn btn-sm btn-outline" onclick="llamaImportModal()">Import profile</button>
      </div>
      <p class="help">A <strong>profile</strong> bundles the model selected above with these arguments.
        <strong>Apply</strong> writes both to <code>/etc/llama.conf</code> and restarts the server if running;
        <strong>Load into editor</strong> just loads them for tweaking (then <strong>Save Changes</strong> to apply args only).
        <strong>Save current as profile</strong> stores the current model + arguments under a name without changing the running config.
        <strong>Export</strong> emits the selected profile as JSON to copy to another node, where <strong>Import profile</strong> reads it back.</p>
    </div>`;
  fillTokRateSpark();
}

// tokens/sec trend: the history store keeps the cumulative tokens_predicted_total
// counter; difference consecutive samples into a rate (drop the negative step a
// server restart produces) and draw it as a sparkline.
async function llamaSvc(action) {
  try { await API.post(`/api/service/llamacpp/${action}`, {}); page_llamacpp(); }
  catch (e) { alert(e.message); }
}

async function llamaSetModel() {
  const model = $('llama-model').value;
  if (!model) { alert('No model selected.'); return; }
  try {
    const r = await API.put('/api/llama/model', { model });
    alert('Model switched.' + (r.restarted ? ' Service restarted.' : ''));
    page_llamacpp();
  } catch (e) { alert(e.message); }
}

async function llamaSaveArgs() {
  llamaSyncArgs();
  const args = _llamaArgs.filter(a => (a.flag || '').trim());
  try {
    const r = await API.put('/api/llama/args', { args });
    alert('Arguments saved.' + (r.restarted ? ' Service restarted.' : ''));
    page_llamacpp();
  } catch (e) { alert(e.message); }
}

// Load the selected profile's model + args into the editor (does NOT apply to
// the running server — review, then Apply or Save Changes).
function llamaLoadPreset() {
  const name = $('llama-preset-select').value;
  const p = _llamaPresets.find(x => x.name === name);
  if (!p) { alert('No profile selected.'); return; }
  _llamaArgs = (p.args || []).map(a => ({ flag: a.flag, value: a.value }));
  $('llama-args-body').innerHTML = llamaArgRows();
  if (p.model) { const sel = $('llama-model'); if (sel) sel.value = p.model; }
}

// Apply a profile server-side: writes both model and args in one rewrite.
async function llamaApplyPreset() {
  const name = $('llama-preset-select').value;
  if (!name) { alert('No profile selected.'); return; }
  if (!confirm(`Apply profile "${name}"? This rewrites the model + arguments and restarts llama-server if it's running.`)) return;
  try {
    const r = await API.post('/api/llama/presets/' + encodeURIComponent(name) + '/apply', {});
    alert('Profile applied.' + (r.restarted ? ' Service restarted.' : ''));
    page_llamacpp();
  } catch (e) { alert(e.message); }
}

async function llamaSavePreset() {
  llamaSyncArgs();
  const name = ($('llama-preset-name').value || '').trim();
  if (!name) { alert('Enter a profile name.'); return; }
  const args = _llamaArgs.filter(a => (a.flag || '').trim());
  const model = ($('llama-model') && $('llama-model').value) || '';
  try {
    await API.post('/api/llama/presets', { name, model, args });
    alert('Profile saved: ' + name);
    page_llamacpp();
  } catch (e) { alert(e.message); }
}

async function llamaDeletePreset() {
  const name = $('llama-preset-select').value;
  if (!name) { alert('No profile selected.'); return; }
  if (!confirm(`Delete profile "${name}"?`)) return;
  try {
    await API.delete('/api/llama/presets/' + encodeURIComponent(name));
    page_llamacpp();
  } catch (e) { alert(e.message); }
}

// ─── Profile portability (export / import between nodes) ────────────
// Both halves are deliberately CLIENT-side. Export re-serialises data the page
// already holds; import POSTs to the ordinary save endpoint, so a pasted
// document passes exactly the same server-side validation as a hand-typed
// profile — the preset-name regex, the flag/value regexes, and the models-dir
// confinement check. That is the point: no new endpoint, no new trust boundary,
// and a malicious paste can do nothing a user with the form could not already do.
const LLAMA_PROFILE_KIND = 'nexus-dashboard/llama-profile';
let _llamaExportName = '';

function llamaProfileDoc(p) {
  return {
    kind: LLAMA_PROFILE_KIND,
    version: 1,
    name: p.name,
    model: p.model || '',
    args: (p.args || []).map(a => ({ flag: a.flag, value: a.value })),
    // Provenance only — never read back on import. The model path is recorded
    // because it is useful to a human reading the file, but it is host-specific,
    // which is why import re-resolves it against the local models dir.
    exported_from: {
      host: stripInfo.host || location.hostname,
      app_version: appVersion || '',
      at: new Date().toISOString(),
    },
  };
}

function llamaExportPreset() {
  const name = $('llama-preset-select').value;
  const p = _llamaPresets.find(x => x.name === name);
  if (!p) { alert('No profile selected.'); return; }
  _llamaExportName = name;
  const json = JSON.stringify(llamaProfileDoc(p), null, 2);
  openModal('Export profile: ' + name, `
    <div class="form-group">
      <label>Profile JSON</label>
      <textarea id="llama-export-json" class="form-control" rows="14" readonly style="font-family:monospace">${escapeHtml(json)}</textarea>
    </div>
    <div class="toolbar">
      <button class="btn" onclick="llamaCopyExport()">Copy to clipboard</button>
      <button class="btn btn-outline" onclick="llamaDownloadExport()">Download .json</button>
    </div>
    <p class="help">Copy this to another node and use <strong>Import profile</strong> there. The model path is
      host-specific: on import it is matched against that node's models directory by full path, then by filename,
      and dropped if neither matches — the arguments still import.</p>
  `);
}

async function llamaCopyExport() {
  const ta = $('llama-export-json');
  if (!ta) return;
  try {
    await navigator.clipboard.writeText(ta.value);
    alert('Profile JSON copied.');
  } catch (e) {
    // The clipboard API needs a secure context and permission. Fall back to the
    // old selection copy; if that fails too the text is at least left selected
    // for a manual copy.
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    if (!ok) alert('Could not copy automatically — the text is selected, copy it manually.');
  }
}

function llamaDownloadExport() {
  const ta = $('llama-export-json');
  if (!ta) return;
  const blob = new Blob([ta.value], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = (_llamaExportName || 'profile').replace(/[^A-Za-z0-9_.-]+/g, '-') + '.llama-profile.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoked on a later tick: revoking synchronously can cancel the download in
  // some browsers before they have finished reading the blob.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function llamaImportModal() {
  openModal('Import profile', `
    <div class="form-group">
      <label>Profile JSON</label>
      <textarea id="llama-import-json" class="form-control" rows="12" placeholder="Paste the JSON exported from another node" style="font-family:monospace"></textarea>
    </div>
    <div class="form-group">
      <label>…or choose a file</label>
      <input id="llama-import-file" class="form-control" type="file" accept=".json,application/json" onchange="llamaImportFile(this)">
    </div>
    <div class="form-group">
      <label>Import as <span class="help">(optional — defaults to the name in the document)</span></label>
      <input id="llama-import-name" class="form-control" placeholder="Profile name" autocomplete="off">
    </div>
    <div class="toolbar"><button class="btn" onclick="llamaDoImport()">Import</button></div>
    <p class="help">Saves the profile on THIS node. It does not touch the running server — apply it afterwards.</p>
  `);
}

function llamaImportFile(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = () => { const ta = $('llama-import-json'); if (ta) ta.value = r.result; };
  r.readAsText(f);
}

// An exported model path came from another host's models dir. Prefer an exact
// path match, then a same-filename match (the common case — same model, different
// directory), and otherwise import the arguments alone: the save endpoint
// rejects a path that does not resolve here, so keeping it would fail the whole
// import over a detail the operator can fix in one click afterwards.
function llamaResolveModel(path) {
  if (!path) return { model: '', note: '' };
  const models = _llamaModels || [];
  if (models.some(m => m.path === path)) return { model: path, note: '' };
  const base = path.split('/').pop();
  const hit = models.find(m => m.path.split('/').pop() === base);
  if (hit) return { model: hit.path, note: 'Model re-pointed to this node\'s copy: ' + hit.path };
  return { model: '', note: 'Model "' + base + '" is not on this node — imported the arguments only.' };
}

async function llamaDoImport() {
  const raw = ($('llama-import-json').value || '').trim();
  if (!raw) { alert('Paste the profile JSON first.'); return; }
  let doc;
  try { doc = JSON.parse(raw); } catch (e) { alert('Not valid JSON: ' + e.message); return; }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) { alert('Expected a JSON object.'); return; }
  if (doc.kind && doc.kind !== LLAMA_PROFILE_KIND) { alert('Not a llama profile (kind: ' + doc.kind + ').'); return; }
  if (!Array.isArray(doc.args)) { alert('Document has no "args" list.'); return; }
  const name = ($('llama-import-name').value || '').trim() || ((doc.name || '') + '').trim();
  if (!name) { alert('No profile name in the document — enter one.'); return; }
  if (_llamaPresets.some(p => p.name === name) &&
      !confirm('A profile named "' + name + '" already exists on this node. Overwrite it?')) return;
  const r = llamaResolveModel(((doc.model || '') + '').trim());
  try {
    await API.post('/api/llama/presets', { name, model: r.model, args: doc.args });
    closeModal();
    alert('Profile imported: ' + name + (r.note ? '\n\n' + r.note : ''));
    page_llamacpp();
  } catch (e) { alert(e.message); }
}

// ─── Models page (AI Tools > Models) ──────────────────────────────────
// The model library for llama.cpp: what is on disk, and how to get more. Its
// own page rather than a card on the llama.cpp page because downloading a
// 200 GB quant is a task in its own right, with its own progress and its own
// settings — but still the llamacpp MODULE, so a node with AI Tools switched
// off does not show it.
async function page_models() {
  const [d, hf] = await Promise.all([
    API.get('/api/llama'),
    API.get('/api/llama/hf').catch(() => ({ token_set: false, rate_mbps: 0 })),
  ]);
  _hfTokenSet = !!hf.token_set;
  _hfRate = hf.rate_mbps || 0;
  _llamaModels = d.models || [];

  const rows = _llamaModels.map(m => `
    <tr>
      <td><code>${escapeHtml(m.name)}</code>${m.path === d.model
        ? ' <span class="status-badge green">in use</span>' : ''}</td>
      <td>${fmtBytes(m.size || 0)}</td>
      <td>${m.parts ? m.parts + ' parts' : ''}</td>
    </tr>`).join('') ||
    '<tr><td colspan="3" class="help">No .gguf models yet — add one below.</td></tr>';

  $('page-content').innerHTML = `
    <h2>Models</h2>
    <div class="card" id="hf-job-card" style="display:none">
      <h3>Download in progress</h3>
      <div class="hf-progress"></div>
      <p class="help">The transfer runs in a detached process: it keeps going if you leave
        this page, sign out, or the dashboard restarts during a deploy. Come back here to
        see where it got to.</p>
    </div>
    <div class="card">
      <h3>Installed</h3>
      <table class="table"><thead><tr><th>Model</th><th>Size</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>
      <p class="help">Under <code>${escapeHtml(d.models_dir)}</code> —
        ${fmtBytes(d.models_free_bytes || 0)} free. A split model is listed once, by its
        first part; llama.cpp opens that and finds the rest beside it.
        Switch the active model on the <a href="#" onclick="showPage('llamacpp');return false">LLama.cpp</a> page.</p>
    </div>
    <div class="card">
      <h3>Add a model</h3>
      <p class="help">Downloads a <strong>GGUF</strong> from Hugging Face into its own
        directory under the models directory. llama.cpp reads GGUF only — a repository of
        original weights (safetensors) is refused with a pointer to the GGUF conversion.</p>
      <div class="toolbar">
        <button class="btn" onclick="llamaPullModal()">Add from Hugging Face</button>
        <button class="btn btn-outline" onclick="llamaHfSettingsModal()">Hugging Face settings</button>
      </div>
      <p class="help">${_hfTokenSet ? 'An access token is saved (needed only for gated or private repositories).'
                                    : 'No access token saved — public GGUF repositories do not need one.'}
        ${_hfRate ? 'Downloads are capped at ' + _hfRate + ' Mbps by default.'
                  : 'Downloads are not speed-capped by default.'}</p>
    </div>
    <div class="card">
      <h3>Backup &amp; restore</h3>
      <div id="bk-progress" class="help"></div>
      <div id="bk-body"><p class="help">Loading…</p></div>
    </div>`;
  llamaPollPull();       // reattach to whatever is already running on this node
  llamaBackupPoll();     // ...and to a backup or restore, which is a separate job
  llamaBackupsRender();
}

// ─── Model backup / restore ───────────────────────────────────────────
// Copies a model to a shared location (an NFS share, typically) and back. Its
// own poller because a backup and a download are independent jobs — you can be
// downloading one model while archiving another.
let _bkPollTimer = null;
let _bkSawActive = false;

async function llamaBackupsRender() {
  const box = $('bk-body');
  if (!box) return;
  let d;
  try { d = await API.get('/api/llama/backups'); }
  catch (e) { box.innerHTML = `<p class="help" style="color:var(--red)">${escapeHtml(e.message)}</p>`; return; }
  const st = d.status || {};
  // Where the bytes will actually land, stated plainly. `mounted` is the field
  // that matters: an unmounted mount point looks identical to a working one
  // until the root disk fills.
  const where = `<p class="help">Target <code>${escapeHtml(st.base || '')}</code>
      ${st.mounted ? `<span class="status-badge green">mounted</span> ${escapeHtml(st.source || '')}`
                   : '<span class="status-badge red">not a mount point</span>'}
      · ${fmtBytes(st.free_bytes || 0)} free
      ${st.rsync ? '' : ' · <span class="status-badge red">rsync missing</span>'}</p>`;
  const problem = d.problem
    ? `<div class="alert alert-warning">${escapeHtml(d.problem)}</div>` : '';

  const local = (d.local_groups || []).map(g => `
    <tr><td><code>${escapeHtml(g.group)}</code></td><td>${fmtBytes(g.size)}</td>
      <td><button class="btn btn-sm" ${d.problem ? 'disabled' : ''}
        onclick="llamaBackupStart('${escapeHtml(g.group)}')">Back up</button></td></tr>`).join('')
    || '<tr><td colspan="3" class="help">No models on this node yet.</td></tr>';

  const saved = (d.backups || []).map(b => `
    <tr>
      <td><code>${escapeHtml(b.group)}</code>${b.present_locally
        ? ' <span class="help">(also local)</span>' : ''}</td>
      <td>${fmtBytes(b.size)}</td>
      <td class="help">${escapeHtml(b.source_node || '')}${b.saved_at
        ? ' · ' + new Date(b.saved_at * 1000).toLocaleDateString() : ''}</td>
      <td>
        <button class="btn btn-sm" ${st.rsync ? '' : 'disabled'}
          onclick="llamaBackupRestore('${escapeHtml(b.group)}')">Restore</button>
        <button class="btn btn-sm btn-danger"
          onclick="llamaBackupDelete('${escapeHtml(b.group)}')">Delete</button>
      </td></tr>`).join('')
    || '<tr><td colspan="4" class="help">Nothing backed up here yet.</td></tr>';

  box.innerHTML = `${problem}${where}
    <h4 style="margin:14px 0 6px">On this node</h4>
    <table class="table"><thead><tr><th>Model</th><th>Size</th><th></th></tr></thead>
      <tbody>${local}</tbody></table>
    <h4 style="margin:14px 0 6px">Backed up</h4>
    <table class="table"><thead><tr><th>Model</th><th>Size</th><th>From</th><th></th></tr></thead>
      <tbody>${saved}</tbody></table>`;
}

async function llamaBackupStart(group) {
  if (!confirm(`Back up "${group}" to the shared location?`)) return;
  try { await API.post('/api/llama/backups/' + encodeURIComponent(group), {}); llamaBackupPoll(); }
  catch (e) { alert(e.message); }
}

async function llamaBackupRestore(group) {
  if (!confirm(`Restore "${group}" from the backup into this node's models directory?`)) return;
  try { await API.post('/api/llama/backups/' + encodeURIComponent(group) + '/restore', {}); llamaBackupPoll(); }
  catch (e) { alert(e.message); }
}

async function llamaBackupDelete(group) {
  if (!confirm(`Delete the BACKUP of "${group}"? The copy on this node is not touched.`)) return;
  try { await API.delete('/api/llama/backups/' + encodeURIComponent(group)); llamaBackupsRender(); }
  catch (e) { alert(e.message); }
}

async function llamaBackupCancel() {
  try { await API.post('/api/llama/backups/job/cancel', {}); llamaBackupPoll(); }
  catch (e) { alert(e.message); }
}

async function llamaBackupPoll() {
  if (_bkPollTimer) { clearTimeout(_bkPollTimer); _bkPollTimer = null; }
  const el = $('bk-progress');
  if (!el) return;                       // navigated away
  let job;
  try { job = await API.get('/api/llama/backups/job'); } catch (e) { return; }
  const done = job.done || 0, tot = job.total || 0;
  const pct = tot ? Math.min(100, Math.round(done / tot * 100)) : 0;
  const verb = job.op === 'restore' ? 'Restoring' : 'Backing up';
  if (job.state === 'running') {
    el.innerHTML = `<strong>${verb} ${escapeHtml(job.group || '')}</strong> —
      ${fmtBytes(done)}${tot ? ' / ' + fmtBytes(tot) : ''} ${tot ? '(' + pct + '%)' : ''}
      ${tot ? hfBar(pct) : ''}
      <div class="toolbar"><button class="btn btn-sm btn-danger"
        onclick="llamaBackupCancel()">Cancel</button></div>`;
    _bkSawActive = true;
    _bkPollTimer = setTimeout(llamaBackupPoll, 2000);
  } else if (job.state === 'interrupted' || job.state === 'cancelled' || job.state === 'error') {
    el.innerHTML = `<span style="color:var(--red)">${escapeHtml(job.error || 'stopped')}</span>
      <p class="help">${fmtBytes(done)} copied. Starting the same ${job.op || 'copy'} again resumes it.</p>`;
    if (_bkSawActive) { _bkSawActive = false; llamaBackupsRender(); }
  } else {
    el.innerHTML = '';
    if (_bkSawActive) { _bkSawActive = false; llamaBackupsRender(); }
  }
}

// ─── Hugging Face model download ──────────────────────────────────────
// Two-step by design: inspect the repo first, then choose what to fetch. A repo
// can hold a dozen quants at wildly different sizes, and picking one blind is
// how you accidentally start a 140 GB transfer.
let _hfGroups = [];
let _hfRepo = '';
let _hfPollTimer = null;
let _hfSawActive = false;   // watched a transfer run? gates the single refresh on completion

function llamaPullModal() {
  openModal('Add model from Hugging Face', `
    <div class="form-group">
      <label>Repository</label>
      <div class="toolbar" style="margin:0">
        <input id="hf-repo" class="form-control" style="flex:1"
               placeholder="e.g. bartowski/Llama-3.2-3B-Instruct-GGUF" autocomplete="off">
        <button class="btn" id="hf-look" onclick="llamaHfLookup()">Look up</button>
      </div>
      <p class="help">llama.cpp loads <strong>GGUF</strong> only. A repository of the
        original weights (safetensors) will be refused — look for a GGUF conversion,
        usually a repo whose name ends in <code>-GGUF</code>.</p>
    </div>
    <div id="hf-result"></div>
    <div class="hf-progress help"></div>
  `);
  $('hf-repo').addEventListener('keydown', e => { if (e.key === 'Enter') llamaHfLookup(); });
  llamaPollPull();     // an already-running download shows up immediately
}

async function llamaHfLookup() {
  const repo = ($('hf-repo').value || '').trim();
  if (!repo) { alert('Enter a repository id.'); return; }
  const btn = $('hf-look'); if (btn) btn.disabled = true;
  const box = $('hf-result');
  box.innerHTML = '<p class="help">Looking up…</p>';
  try {
    const d = await API.get('/api/llama/hf/repo?repo=' + encodeURIComponent(repo));
    _hfGroups = d.groups || [];
    _hfRepo = d.repo;
    box.innerHTML = llamaHfGroupsHtml(d);
  } catch (e) {
    // The "needs a GGUF" refusal is the common, expected case — show it as
    // guidance rather than as a failure.
    box.innerHTML = `<p class="help" style="color:var(--red)">${escapeHtml(e.message)}</p>`;
  } finally { if (btn) btn.disabled = false; }
}

function llamaHfGroupsHtml(d) {
  const rows = _hfGroups.map((g, i) => `
    <tr>
      <td><label style="display:flex;gap:8px;align-items:center;cursor:pointer">
        <input type="radio" name="hf-group" value="${escapeHtml(g.name)}" ${i === 0 ? 'checked' : ''}>
        <span>${escapeHtml(g.name)}</span></label></td>
      <td>${fmtBytes(g.bytes)}</td>
      <td>${g.parts > 1 ? g.parts + ' parts' : 'single file'}</td>
      <td>${g.installed ? '<span class="help">already present</span>' : ''}</td>
    </tr>`).join('');
  return `
    <table class="table"><thead><tr><th>Quant</th><th>Size</th><th></th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    <p class="help">Free in <code>${escapeHtml(d.models_dir)}</code>: ${fmtBytes(d.free_bytes)}.
      A split model downloads all its parts into one directory — llama.cpp opens the
      first part and finds the rest beside it.</p>
    <div class="form-group">
      <label>Speed limit <span class="help">(Mbps — 0 means unlimited)</span></label>
      <input id="hf-rate" class="form-control" style="max-width:160px" type="number"
             min="0" value="${_hfRate}" autocomplete="off">
      <p class="help">Caps this download so it does not consume the whole link.
        600 Mbps ≈ 75 MB/s. The saved default is used unless you change it here.</p>
    </div>
    <div class="toolbar"><button class="btn" id="hf-start" onclick="llamaStartPull()">Download</button></div>`;
}

async function llamaStartPull() {
  const sel = document.querySelector('input[name="hf-group"]:checked');
  if (!sel) { alert('Choose which quant to download.'); return; }
  const rateEl = $('hf-rate');
  const body = { repo: _hfRepo, group: sel.value };
  if (rateEl && rateEl.value !== '') body.rate_mbps = parseInt(rateEl.value, 10) || 0;
  const b = $('hf-start'); if (b) b.disabled = true;
  try {
    await API.post('/api/llama/models/pull', body);
    llamaPollPull();
  } catch (e) { alert(e.message); if (b) b.disabled = false; }
}

async function llamaHfAction(path, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  try { await API.post('/api/llama/models/pull/' + path, {}); llamaPollPull(); }
  catch (e) { alert(e.message); }
}

// Progress bar reusing the .usage-bar CSS but pinned green: usageBar() ramps
// green -> yellow -> red as it fills, which reads as "nearly full is bad" —
// exactly backwards for a download that is nearly finished.
function hfBar(pct) {
  pct = Math.max(0, Math.min(100, Math.round(pct)));
  return `<div class="usage"><div class="usage-bar"><div class="usage-bar-fill green"
    style="width:${pct}%"></div></div><span class="usage-pct">${pct}%</span></div>`;
}

// One poller, cleared before rescheduling: opening the modal twice must not
// leave two timers racing to write the same element.
//
// It writes to EVERY `.hf-progress` target, because a download outlives the
// modal that started it. The llama.cpp page carries its own card, so leaving
// the page — or closing the browser entirely — and coming back REATTACHES to
// the transfer in progress instead of hiding it behind a modal nobody thinks
// to reopen. With the modal open there are two targets; both get the same html.
async function llamaPollPull() {
  if (_hfPollTimer) { clearTimeout(_hfPollTimer); _hfPollTimer = null; }
  const targets = document.querySelectorAll('.hf-progress');
  if (!targets.length) return;           // navigated away — stop polling
  const el = { set innerHTML(v) { targets.forEach(t => { t.innerHTML = v; }); } };
  let job;
  try { job = await API.get('/api/llama/models/pull/status'); } catch (e) { return; }
  const card = $('hf-job-card');
  if (card) {
    card.style.display =
      ['downloading', 'interrupted', 'cancelled', 'error'].includes(job.state) ? '' : 'none';
  }
  const dl = job.downloaded || 0, tot = job.total || 0;
  const pct = tot ? Math.min(100, Math.round(dl / tot * 100)) : 0;
  const bar = tot ? hfBar(pct) : '';
  const what = escapeHtml(job.group || '');

  if (job.state === 'downloading') {
    el.innerHTML = `<strong>${what}</strong> — ${fmtBytes(dl)}${tot ? ' / ' + fmtBytes(tot) : ''}
      ${tot ? '(' + pct + '%)' : ''}${bar}
      <span class="help">${escapeHtml(job.current || '')}${job.rate_mbps ? ' · capped at ' + job.rate_mbps + ' Mbps' : ''}</span>
      <div class="toolbar"><button class="btn btn-sm btn-danger"
        onclick="llamaHfAction('cancel')">Cancel</button></div>`;
    _hfSawActive = true;
    _hfPollTimer = setTimeout(llamaPollPull, 1500);
  } else if (job.state === 'done') {
    el.innerHTML = `<strong>${what}</strong> downloaded. Refreshing…`;
    // Guarded: page_llamacpp() re-renders the card and restarts the poller, so
    // an unguarded refresh here would loop forever on an already-finished job.
    if (_hfSawActive) { _hfSawActive = false; setTimeout(() => { closeModal(); page_llamacpp(); }, 900); }
  } else if (job.state === 'interrupted' || job.state === 'cancelled') {
    el.innerHTML = `<strong>${what}</strong> — ${escapeHtml(job.error || 'paused')}
      ${bar}<span class="help">${fmtBytes(dl)}${tot ? ' / ' + fmtBytes(tot) : ''} already on disk.</span>
      <div class="toolbar"><button class="btn btn-sm" onclick="llamaHfAction('resume')">Resume</button></div>`;
  } else if (job.state === 'error') {
    el.innerHTML = `<span style="color:var(--red)">${escapeHtml(job.error || 'Download failed')}</span>
      <div class="toolbar"><button class="btn btn-sm" onclick="llamaHfAction('resume')">Retry</button></div>`;
    const b = $('hf-start'); if (b) b.disabled = false;
  } else {
    el.innerHTML = '';
  }
}

// ─── Hugging Face access token + default speed limit ──────────────────
// The token is write-only from here: the API reports only whether one is set,
// so a stored credential can never be read back out through the browser.
function llamaHfSettingsModal() {
  openModal('Hugging Face settings', `
    <div class="form-group">
      <label>Access token ${_hfTokenSet ? '<span class="help">(one is saved — entering a new one replaces it)</span>'
                                        : '<span class="help">(optional)</span>'}</label>
      <input id="hf-token" class="form-control" type="password" autocomplete="off"
             placeholder="hf_…">
      <p class="help">Only needed for <strong>gated or private</strong> repositories. Public
        GGUF repos download without one. Stored on this node only and never shown again.</p>
    </div>
    <div class="form-group">
      <label>Default speed limit <span class="help">(Mbps — 0 means unlimited)</span></label>
      <input id="hf-rate-default" class="form-control" style="max-width:160px" type="number"
             min="0" value="${_hfRate}" autocomplete="off">
      <p class="help">Pre-filled into every download, and overridable per download.
        On a gigabit link, 600 leaves roughly 400 Mbps for everything else.</p>
    </div>
    <div class="toolbar">
      <button class="btn" onclick="llamaHfSave()">Save</button>
      ${_hfTokenSet ? '<button class="btn btn-danger btn-outline" onclick="llamaHfForgetToken()">Forget token</button>' : ''}
    </div>
  `);
}

async function llamaHfSave() {
  const tok = ($('hf-token').value || '').trim();
  const rate = parseInt(($('hf-rate-default').value || '0'), 10) || 0;
  const body = { rate_mbps: rate };
  if (tok) body.token = tok;      // blank means "leave it alone", not "clear it"
  try {
    const r = await API.put('/api/llama/hf', body);
    _hfTokenSet = r.token_set; _hfRate = r.rate_mbps;
    closeModal();
    alert('Saved.');
  } catch (e) { alert(e.message); }
}

async function llamaHfForgetToken() {
  if (!confirm('Forget the stored Hugging Face token?')) return;
  try {
    await API.delete('/api/llama/hf/token');
    _hfTokenSet = false;
    closeModal();
  } catch (e) { alert(e.message); }
}

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
