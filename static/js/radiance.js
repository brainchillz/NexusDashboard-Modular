// ─── Radiance (vLLM Radiance systemd unit) ─────────────────────────────
// Page + dashboard card for the `radiance` module. Everything comes from
// /api/radiance (unit state via systemctl, the effective environment, the
// drop-in overrides, /metrics + /version + /v1/models, and the engine's own
// startup lines); actions are systemctl verbs and the drop-in editor.

function rdPct(x) { return x == null ? '—' : `${Math.round(x * 100)}%`; }
function rdNum(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 1 : d); }
function rdStateBadge(st) {
  if (!st.installed) return '<span class="status-badge gray">not installed</span>';
  if (st.running) return st.metrics ? '<span class="status-badge green">serving</span>' : '<span class="status-badge yellow">active · engine loading</span>';
  const cls = st.active === 'failed' ? 'red' : st.active === 'activating' ? 'yellow' : 'gray';
  return `<span class="status-badge ${cls}">${escapeHtml(st.active || 'inactive')}${st.sub && st.sub !== st.active ? ' · ' + escapeHtml(st.sub) : ''}</span>`;
}
function rdSourceBadge(s) {
  return s === 'override' ? '<span class="status-badge yellow" title="set by the dashboard drop-in">override</span>'
    : s === 'launcher' ? '<span class="status-badge gray" title="pinned by the serve-tpN.sh wrapper the unit starts through">launcher</span>'
    : s === 'gpus' ? '<span class="status-badge gray" title="implied by the GPUS list (the launcher default)">from GPUS</span>'
    : s === 'unit' ? '<span class="status-badge gray" title="set in the unit file">unit</span>'
    : '<span class="status-badge gray" style="opacity:.6" title="serve-mxfp4.sh default">default</span>';
}
const RD_GROUPS = [['shape', 'Serving shape'], ['paths', 'Checkpoints & paths'], ['serving', 'Serving'], ['advanced', 'Engine flags (RADIANCE_*)'], ['other', 'Other unit settings (read-only)']];

function rdInput(k, editable, models) {
  const v = k.value == null ? '' : String(k.value);
  if (k.type === 'readonly') return `<span class="mono">${escapeHtml(v)}</span>`;
  const dis = editable ? '' : 'disabled';
  const id = `rd-k-${k.key}`;
  if (k.type === 'enum') {
    return `<select id="${id}" data-key="${k.key}" class="rd-knob" ${dis}>
      <option value="" ${v === '' ? 'selected' : ''}>(default: ${escapeHtml(k.default)})</option>
      ${k.choices.map(c => `<option value="${escapeHtml(c)}" ${v === c ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('')}</select>`;
  }
  if (k.type === 'int' || k.type === 'float') {
    return `<input id="${id}" data-key="${k.key}" class="rd-knob mono" type="number" min="${k.range[0]}" max="${k.range[1]}" ${k.type === 'float' ? 'step="0.01"' : 'step="1"'} value="${escapeHtml(v)}" placeholder="${escapeHtml(k.default)}" ${dis}>`;
  }
  const list = (k.type === 'path' && (k.key === 'SNAP' || k.key === 'DRAFTER') && models.length) ? `list="rd-models"` : '';
  return `<input id="${id}" data-key="${k.key}" class="rd-knob mono" type="text" value="${escapeHtml(v)}" placeholder="${escapeHtml(k.default)}" ${list} ${dis} style="min-width:${k.type === 'path' || k.type === 'names' ? '28em' : '10em'}">`;
}

async function page_radiance() {
  const el = $('page-content');
  el.innerHTML = '<h2>Radiance</h2><p class="help">Loading…</p>';
  let st;
  try { st = await API.get('/api/radiance'); }
  catch (e) { el.innerHTML = `<h2>Radiance</h2><div class="error">${escapeHtml(e.message)}</div>`; return; }
  const admin = currentRole === 'admin';
  let html = '<h2>Radiance <span class="help">vLLM Radiance inference server</span></h2>';

  if (!st.installed) {
    html += `<div class="alert alert-warning">No <code>${escapeHtml(st.unit)}.service</code> on this node. The module expects the vllm-radiance launcher's systemd unit (set <code>DASHBOARD_RADIANCE_UNIT</code> if yours is named differently).</div>`;
    el.innerHTML = html; return;
  }
  const m = st.metrics, c = st.conflict || {}, su = st.startup || {};
  const conflictNote = c.present && c.active ? ` — <strong>${escapeHtml(c.unit)} is running and will be STOPPED</strong> (the unit declares Conflicts=)` : '';

  // ── Unit card
  html += `<div class="card"><h3>Unit <code>${escapeHtml(st.unit)}.service</code> ${rdStateBadge(st)}</h3>
    <table class="table kv"><tbody>
      <tr><td>Since</td><td>${escapeHtml(st.since || '—')}${st.nrestarts ? ` · <span class="status-badge yellow">${st.nrestarts} systemd restart(s)</span>` : ''}</td></tr>
      <tr><td>Container</td><td><code>${escapeHtml(st.container)}</code> · image <code>${escapeHtml(st.image || '?')}</code> · port ${st.port}${st.tp ? ` · TP=${escapeHtml(st.tp)}` : ''}${st.env.GPUS ? ` · GPUs ${escapeHtml(st.env.GPUS)}` : ''}</td></tr>
      <tr><td>Checkpoint</td><td><code>${escapeHtml(st.env.SNAP || '(default)')}</code>${st.env.SPEC_METHOD === 'mtp' ? ' · mtp' : ` · drafter <code>${escapeHtml(st.env.DRAFTER || '(default)')}</code>`}</td></tr>
      <tr><td>${escapeHtml(c.unit || 'llama-server')}</td><td>${c.present ? `${c.active ? '<span class="status-badge red">running</span>' : '<span class="status-badge gray">stopped</span>'} · ${c.enabled ? '<strong>enabled at boot</strong>' : 'disabled at boot'} — conflicts with this unit (same port and GPU)` : 'not installed'}</td></tr>
    </tbody></table>
    ${admin ? `<div class="toolbar">
      ${st.running
        ? `<button class="btn btn-sm btn-outline" onclick="rdAction('stop')">Stop</button>
           <button class="btn btn-sm btn-outline" onclick="rdAction('restart')" title="Warm start ≈ ${Math.round(st.warm_start_s / 60)} min (compile cache hit); a cold compile takes longer">Restart</button>`
        : `<button class="btn btn-sm" onclick="rdAction('start')" title="Warm start ≈ ${Math.round(st.warm_start_s / 60)} min">Start${c.active ? ' (stops ' + escapeHtml(c.unit) + ')' : ''}</button>`}
      <span style="margin-left:auto"></span>
      <label class="help"><input type="checkbox" ${st.enabled ? 'checked' : ''} onchange="rdAction(this.checked ? 'enable' : 'disable')"> Enabled at boot</label>
    </div>${conflictNote ? `<p class="help">Start / restart${conflictNote}.</p>` : ''}` : ''}
  </div>`;

  // ── Server card
  html += '<div class="card"><h3>Server</h3>';
  if (!st.running) html += '<p class="help">Not running.</p>';
  else {
    const mods = (st.models || []).map(x => `<code>${escapeHtml(x.id)}</code>`).join(' ');
    html += `<table class="table kv"><tbody>
      <tr><td>Model ids</td><td>${mods || '<span class="help">(engine not answering yet)</span>'}${st.models && st.models[0] && st.models[0].max_model_len ? ` · max_model_len ${Number(st.models[0].max_model_len).toLocaleString()}` : ''}</td></tr>
      <tr><td>vLLM</td><td>${escapeHtml(st.version || '?')} · image <code>${escapeHtml(st.image || '?')}</code></td></tr>
      <tr><td>Engine</td><td>${su.ready ? '<span class="status-badge green">ready</span>' : `<span class="status-badge yellow">loading</span> <span class="help">warm start ≈ ${Math.round(st.warm_start_s / 60)} min</span>`}
        ${su.kv_tokens ? ` · KV cache <strong>${su.kv_tokens.toLocaleString()}</strong> tokens` : ''}${su.max_concurrency ? ` · max concurrency <strong>${su.max_concurrency}×</strong> at ${su.concurrency_at.toLocaleString()} tokens/request` : ''}
        ${su.weights_gib ? ` · weights ${su.weights_gib} GiB in ${rdNum(su.load_s, 0)} s` : ''}${su.init_s ? ` · engine init ${rdNum(su.init_s, 0)} s` : ''}${su.errors ? ` · <span class="status-badge red">${su.errors} error line(s) in the log</span>` : ''}</td></tr>
    </tbody></table>`;
    if (su.lines && su.lines.length) html += `<details><summary class="help">The engine's own startup lines (last start)</summary><pre class="log" style="white-space:pre-wrap;max-height:30vh;overflow:auto">${escapeHtml(su.lines.join('\n'))}</pre></details>`;
  }
  html += '</div>';

  // ── Throughput card
  html += '<div class="card"><h3>Throughput</h3>';
  if (!m) html += `<p class="help">${st.running ? `No metrics yet (${escapeHtml(st.metrics_error || 'scrape failed')}) — the engine is still loading.` : 'Not running.'}</p>`;
  else html += `<div class="stat-row">
      <div class="stat"><div class="stat-v">${m.requests_running}</div><div class="stat-l">running</div></div>
      <div class="stat"><div class="stat-v">${m.requests_waiting}</div><div class="stat-l">waiting</div></div>
      <div class="stat"><div class="stat-v">${rdNum(m.kv_cache_pct, 1)}%</div><div class="stat-l">KV cache used</div></div>
      <div class="stat"><div class="stat-v">${rdPct(m.draft_accept_ratio)}</div><div class="stat-l">draft acceptance</div></div>
      <div class="stat"><div class="stat-v">${rdPct(m.prefix_cache_hit_ratio)}</div><div class="stat-l">prefix-cache hits</div></div>
    </div>
    <p class="help">${m.requests_total.toLocaleString()} requests · ${m.generation_tokens_total.toLocaleString()} tokens generated · ${m.prompt_tokens_total.toLocaleString()} prompt tokens (${m.prompt_tokens_cached_total.toLocaleString()} cached)${m.preemptions_total ? ` · <strong>${m.preemptions_total} preemptions</strong>` : ''}</p>
    <div id="rd-spark"></div>`;
  html += '</div>';

  // ── Boot parameters
  const knobs = st.knobs || [], models = st.models_available || [];
  html += `<div class="card"><h3>Boot parameters</h3>
    <p class="help">Every value is an <code>Environment=</code> read by <code>serve-mxfp4.sh</code> at start. Changes go to a drop-in
      <code>${escapeHtml(st.dropin)}</code> (the unit file is never edited; a later assignment wins) and apply on the next start —
      a restart is a warm start of ≈ ${Math.round(st.warm_start_s / 60)} min. Leave a field empty to use the unit's / launcher's value.
      ${st.editable ? '' : `<br><strong>Read-only on this node:</strong> the dashboard is not root and <code>${escapeHtml(st.helper || '')}</code> is not installed — run the installer with <code>--helpers-only</code>.`}
      ${st.other_dropins && st.other_dropins.length ? `<br>Other drop-ins also apply: <code>${st.other_dropins.map(escapeHtml).join('</code>, <code>')}</code>.` : ''}</p>
    ${models.length ? `<datalist id="rd-models">${models.map(p => `<option value="${escapeHtml(p)}">`).join('')}</datalist>` : ''}`;
  const canEdit = admin && st.editable;
  for (const [g, label] of RD_GROUPS) {
    const rows = knobs.filter(k => k.group === g);
    if (!rows.length && g !== 'advanced') continue;
    html += `<h4>${label}</h4><table class="table"><thead><tr><th>Parameter</th><th>Value</th><th>Source</th><th>Unit file</th><th>Launcher default</th></tr></thead><tbody>`;
    for (const k of rows) {
      html += `<tr><td><code title="${escapeHtml(k.help)}">${escapeHtml(k.key)}</code><div class="help" style="max-width:26em">${escapeHtml(k.help)}</div></td>
        <td>${rdInput(k, canEdit, models)}</td><td>${rdSourceBadge(k.source)}</td>
        <td class="mono">${escapeHtml(k.unit_value == null ? '—' : k.unit_value)}</td><td class="mono">${escapeHtml(k.default || '—')}</td></tr>`;
    }
    if (g === 'advanced' && canEdit) html += `<tr><td><input id="rd-adv-key" class="mono" type="text" placeholder="RADIANCE_…" style="width:16em"></td>
        <td><input id="rd-adv-val" class="mono" type="text" placeholder="value" style="width:10em"> <button class="btn btn-sm btn-outline" onclick="rdAddFlag()">Add flag</button></td><td colspan="3" class="help">Engine flags from DOCKERHUB.md. Values are short tokens, never free text; <code>EXTRA</code> (raw vllm arguments) is deliberately not offered.</td></tr>`;
    html += '</tbody></table>';
  }
  if (canEdit) html += `<div class="toolbar">
      <button class="btn btn-sm" onclick="rdApply(false)" title="Write the drop-in + daemon-reload; takes effect on the next start">Apply</button>
      <button class="btn btn-sm" onclick="rdApply(true)" title="Write the drop-in, daemon-reload and restart the unit now">Apply &amp; restart</button>
      <span style="margin-left:auto"></span>
      <button class="btn btn-sm btn-outline" onclick="rdReset()" ${st.dropin_present ? '' : 'disabled'} title="Remove the dashboard drop-in — the unit file's own values apply again">Reset overrides</button>
    </div>`;
  if (st.dropin_text) html += `<details><summary class="help">Current drop-in</summary><pre class="log" style="white-space:pre-wrap">${escapeHtml(st.dropin_text)}</pre></details>`;
  html += '</div>';

  html += `<div class="toolbar"><button class="btn btn-sm btn-outline" onclick="rdLogs('container')">Container log</button>
    <button class="btn btn-sm btn-outline" onclick="rdLogs('journal')">Unit journal</button></div>`;
  el.innerHTML = html;
  if (m) rdFillSpark(st.unit);
}

async function rdFillSpark(unit) {
  try {
    const r = await API.get(`/api/history?metric=radiance_tokens&label=${encodeURIComponent(unit)}&since=86400`);
    const pts = (r.points || []).filter(p => p && p[1] != null);
    const box = $('rd-spark');
    if (box && pts.length > 2) box.innerHTML = `<span class="help">tokens generated per tick, last 24 h</span> ${sparkline(pts)}`;
  } catch (e) { /* history off or empty */ }
}

async function rdAction(action) {
  const warn = { stop: 'Stop the vLLM Radiance server? Clients on this node lose the inference API until it is started again.',
                 restart: 'Restart the server now? A warm start takes about three minutes; in-flight requests are dropped.' }[action];
  if (warn && !confirm(warn)) return;
  try {
    const r = await API.post('/api/radiance/action', { action });
    if (r && r.note) alert(r.note);
  } catch (e) { alert(e.message); }
  page_radiance();
}

function rdCollect() {
  const overrides = {};
  document.querySelectorAll('.rd-knob').forEach(i => { overrides[i.dataset.key] = i.value.trim(); });
  return overrides;
}

async function rdApply(restart, confirmed) {
  if (restart && !confirmed && !confirm('Apply the parameters and restart the server now? Warm start ≈ 3 min; in-flight requests are dropped.')) return;
  const body = { overrides: rdCollect(), restart: !!restart, confirm: !!confirmed };
  try {
    const r = await API.post('/api/radiance/params', body);
    if (r && r.needs_confirm) {
      if (confirm(r.warning + '\n\nKeep the pin and apply anyway?')) return rdApply(restart, true);
      return;
    }
    if (r && r.restart_error) alert('Parameters written, but the restart failed: ' + r.restart_error);
  } catch (e) { alert(e.message); }
  page_radiance();
}

async function rdReset() {
  if (!confirm('Remove every dashboard override? The unit file\'s own values apply on the next start.')) return;
  const restart = confirm('Also restart the server now so they take effect? (Cancel = apply on the next start.)');
  try { await API.post('/api/radiance/params/reset', { restart }); }
  catch (e) { alert(e.message); }
  page_radiance();
}

function rdAddFlag() {
  const k = ($('rd-adv-key').value || '').trim(), v = ($('rd-adv-val').value || '').trim();
  if (!/^RADIANCE_[A-Z0-9_]{1,48}$/.test(k)) { alert('Flag names look like RADIANCE_SOMETHING'); return; }
  if (!/^[A-Za-z0-9._,:/-]{1,64}$/.test(v)) { alert('Value must be a short token (letters, digits . _ , : / -)'); return; }
  const tbody = $('rd-adv-key').closest('tbody');
  tbody.insertAdjacentHTML('afterbegin', `<tr><td><code>${escapeHtml(k)}</code></td><td><input data-key="${escapeHtml(k)}" class="rd-knob mono" type="text" value="${escapeHtml(v)}" style="width:10em"></td><td>${rdSourceBadge('override')} <span class="help">(not applied yet)</span></td><td>—</td><td>—</td></tr>`);
  $('rd-adv-key').value = ''; $('rd-adv-val').value = '';
}

async function rdLogs(source) {
  try {
    const r = await API.get(`/api/radiance/logs?source=${encodeURIComponent(source)}&tail=400`);
    openModal(`Radiance ${source === 'journal' ? 'unit journal' : 'container log'} — ${r.name}`, `<pre class="log" style="max-height:60vh;overflow:auto">${escapeHtml(r.logs || '(empty)')}</pre>`, { wide: true });
  } catch (e) { alert(e.message); }
}

// Dashboard card — from the /api/summary block (unit state + one /metrics
// scrape; nothing that waits on the engine).
function dashcard_radiance(ctx) {
  const r = ctx.s.radiance;
  if (!r) return '';            // no unit here — no card
  const dot = !r.running ? (r.state === 'failed' ? 'red' : 'gray') : r.ready ? 'green' : 'yellow';
  const val = r.running
    ? (r.ready ? `<span title="${escapeHtml(r.served_name ? 'served as ' + r.served_name : '')}">${escapeHtml(r.model || '?')}</span>` : '<span class="card-unit">loading</span>')
    : `<span class="card-unit">${escapeHtml(r.state || 'stopped')}</span>`;
  const sub = r.ready
    ? `${r.requests_running || 0} running${r.requests_waiting ? ` · <strong>${r.requests_waiting} waiting</strong>` : ''} · KV ${rdNum(r.kv_cache_pct, 0)}%${r.draft_accept_ratio != null ? ` · accept ${rdPct(r.draft_accept_ratio)}` : ''}`
    : `${escapeHtml(r.checkpoint || '')}${r.tp ? ` · TP=${escapeHtml(r.tp)}` : ''}`;
  return `
    <div class="card card-link" onclick="showPage('radiance')">
      <div class="card-head"><span class="status-dot ${dot}"></span>Radiance</div>
      <div class="card-value" style="font-size:1.2em">${val}</div>
      <div class="card-sub">${sub}</div>
      <div class="card-sub">${escapeHtml(r.image ? r.image.split('/').pop() : '')}${r.enabled === false ? ' · <span class="status-badge yellow">not at boot</span>' : ''}${r.has_overrides ? ' · <span class="status-badge yellow">overrides</span>' : ''}${r.nrestarts ? ` · ${r.nrestarts} restarts` : ''}</div>
    </div>`;
}
