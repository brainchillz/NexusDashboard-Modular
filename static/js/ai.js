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
    <div class="cards">${cards}</div>`;
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

async function page_llamacpp() {
  const [d, pr, lh] = await Promise.all([
    API.get('/api/llama'),
    API.get('/api/llama/presets').catch(() => ({ presets: [] })),
    API.get('/api/llama/health').catch(() => null),
  ]);
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
        <button class="btn btn-outline" onclick="llamaPullModal()">Add from Hugging Face</button>
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
        <button class="btn btn-sm btn-danger" onclick="llamaDeletePreset()">Delete</button>
      </div>
      <div class="toolbar">
        <input id="llama-preset-name" class="form-control" style="max-width:300px" placeholder="New profile name" autocomplete="off">
        <button class="btn btn-sm btn-outline" onclick="llamaSavePreset()">Save current as profile</button>
      </div>
      <p class="help">A <strong>profile</strong> bundles the model selected above with these arguments.
        <strong>Apply</strong> writes both to <code>/etc/llama.conf</code> and restarts the server if running;
        <strong>Load into editor</strong> just loads them for tweaking (then <strong>Save Changes</strong> to apply args only).
        <strong>Save current as profile</strong> stores the current model + arguments under a name without changing the running config.</p>
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

// Pull a GGUF from Hugging Face into the models dir (background download).
function llamaPullModal() {
  openModal('Add model from Hugging Face', `
    <div class="form-group">
      <label>Repository</label>
      <input id="hf-repo" class="form-control" placeholder="e.g. bartowski/Llama-3.2-3B-Instruct-GGUF" autocomplete="off">
    </div>
    <div class="form-group">
      <label>Filename (.gguf)</label>
      <input id="hf-file" class="form-control" placeholder="e.g. Llama-3.2-3B-Instruct-Q4_K_M.gguf" autocomplete="off">
    </div>
    <div class="form-group">
      <label>HF token <span class="help">(optional — only for gated/private repos)</span></label>
      <input id="hf-token" class="form-control" type="password" autocomplete="off">
    </div>
    <div id="hf-progress" class="help"></div>
    <div class="toolbar"><button class="btn" id="hf-start" onclick="llamaStartPull()">Download</button></div>
    <p class="help">Downloads into the models directory in the background; the file appears in the model list when it finishes. One download at a time.</p>
  `);
}

async function llamaStartPull() {
  const repo = ($('hf-repo').value || '').trim();
  const filename = ($('hf-file').value || '').trim();
  const token = ($('hf-token').value || '').trim();
  if (!repo || !filename) { alert('Enter a repository and filename.'); return; }
  try {
    await API.post('/api/llama/models/pull', { repo, filename, token });
    const b = $('hf-start'); if (b) b.disabled = true;
    llamaPollPull();
  } catch (e) { alert(e.message); }
}

async function llamaPollPull() {
  let job;
  try { job = await API.get('/api/llama/models/pull/status'); } catch (e) { return; }
  const el = $('hf-progress');
  if (!el) return;   // modal was closed
  if (job.state === 'downloading') {
    const dl = job.downloaded || 0, tot = job.total || 0;
    const pct = tot ? ` (${Math.round(dl / tot * 100)}%)` : '';
    el.textContent = `Downloading ${job.filename}… ${fmtBytes(dl)}${tot ? ' / ' + fmtBytes(tot) : ''}${pct}`;
    setTimeout(llamaPollPull, 1500);
  } else if (job.state === 'done') {
    el.textContent = `Done: ${job.filename}. Refreshing…`;
    setTimeout(() => { closeModal(); page_llamacpp(); }, 800);
  } else if (job.state === 'error') {
    el.textContent = 'Error: ' + (job.error || 'download failed');
    const b = $('hf-start'); if (b) b.disabled = false;
  }
}

// ─── Network ────────────────────────────────────────────
let _netCountdown = null;

