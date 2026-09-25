// ─── Halogen (halogen-flash-server compose stack) ──────────────────────
// Page + dashboard card for the `halogen` module. Everything comes from
// /api/halogen (docker state + /health + /metrics + /cache + the engine's
// own budget lines); actions are compose verbs and the boot toggle.

function hgHealthBadge(c) {
  if (!c) return '<span class="status-badge gray">missing</span>';
  if (!c.running) return `<span class="status-badge red">${escapeHtml(c.state || 'stopped')}</span>`;
  const h = c.health || 'running';
  const cls = h === 'healthy' ? 'green' : h === 'starting' ? 'yellow' : h === 'unhealthy' ? 'red' : 'green';
  return `<span class="status-badge ${cls}">${escapeHtml(h)}</span>`;
}

function hgPct(x) { return x == null ? '—' : `${Math.round(x * 100)}%`; }
function hgNum(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 1 : d); }
function hgSince(ts) {
  if (!ts || ts.startsWith('0001')) return '';
  const s = (Date.now() - Date.parse(ts)) / 1000;
  if (!(s > 0)) return '';
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

async function page_halogen() {
  const el = $('page-content');
  el.innerHTML = '<h2>Halogen</h2><p class="help">Loading…</p>';
  let st;
  try { st = await API.get('/api/halogen'); }
  catch (e) { el.innerHTML = `<h2>Halogen</h2><div class="error">${escapeHtml(e.message)}</div>`; return; }
  const admin = currentRole === 'admin';
  let html = '<h2>Halogen</h2>';

  if (!st.reachable) {
    html += `<div class="alert alert-warning">Docker is not reachable (${escapeHtml(st.error || 'no socket')}) — the Halogen stack runs under docker compose, so nothing can be read or controlled here.</div>`;
    el.innerHTML = html; return;
  }
  if (!st.deployed) {
    html += `<div class="alert alert-warning">No <code>${escapeHtml(st.project)}</code> compose stack on this node. `
      + (st.working_dir
        ? `Last seen at <code>${escapeHtml(st.working_dir)}</code> — <button class="btn btn-sm" onclick="hgAction('up')" ${admin ? '' : 'disabled'}>Bring it up</button>`
        : 'Deploy it with the installer from Halo-Halogen-Docker (the stack directory is learned from the compose labels the first time it runs).')
      + '</div>';
    el.innerHTML = html; return;
  }

  const c = st.containers || {}, eng = c.engine, api = c.api, h = st.health, m = st.metrics, cache = st.cache;
  const busyNote = st.running ? '' : ' (stack is not fully running)';

  // ── Stack card
  html += `<div class="card"><h3>Stack</h3>
    <table class="table"><thead><tr><th>Service</th><th>Container</th><th>Image</th><th>State</th><th>Up for</th><th>At boot</th></tr></thead><tbody>
    ${['engine', 'api'].map(s => { const x = c[s]; return `<tr><td><strong>${s}</strong></td>
      <td><code>${escapeHtml(x ? x.name : '—')}</code></td>
      <td>${escapeHtml(x ? (x.tag || x.image) : '—')}</td>
      <td>${hgHealthBadge(x)}</td>
      <td>${x && x.running ? escapeHtml(hgSince(x.started_at)) : '—'}</td>
      <td>${x ? escapeHtml(x.restart_policy || 'no') : '—'}</td></tr>`; }).join('')}
    </tbody></table>
    ${st.tags_match === false ? '<div class="alert alert-danger">engine and api run DIFFERENT images — set one HALOGEN_TAG in .env and recreate.</div>' : ''}
    <p class="help">Stack directory <code>${escapeHtml(st.working_dir || '?')}</code> · API <code>${escapeHtml(st.api_url || 'port not published')}</code>
      ${st.update_available ? ` · <span class="status-badge yellow" title="highest X.Y.Z tag at the registry">${escapeHtml(st.latest_tag)} available</span> <span class="help">— read the changelog, then <code>upgrade-halogen.sh ${escapeHtml(st.latest_tag)}</code> in the stack directory</span>`
        : st.latest_tag ? ` · <span class="status-badge green" title="highest tag at the registry is ${escapeHtml(st.latest_tag)}">latest</span>` : ''}</p>
    ${admin ? `<div class="toolbar">
      ${st.running
        ? `<button class="btn btn-sm btn-outline" onclick="hgAction('stop')">Stop</button>
           <button class="btn btn-sm btn-outline" onclick="hgAction('restart')" title="Restarts both containers — the engine reloads the model (~20 s warm, minutes cold)">Restart</button>
           <button class="btn btn-sm" onclick="hgAction('restart-api')" title="Restarts only the API front-end — no model reload">Restart API</button>`
        : `<button class="btn btn-sm" onclick="hgAction('start')">Start</button>`}
      <button class="btn btn-sm btn-outline" onclick="hgAction('up')" title="docker compose up -d — recreates containers whose compose config changed">Up (recreate)</button>
      <button class="btn btn-sm btn-danger" onclick="hgAction('down')" title="docker compose down — removes the containers (volumes untouched)">Down</button>
      <span style="margin-left:auto"></span>
      <label class="help"><input type="checkbox" ${st.boot_enabled ? 'checked' : ''} onchange="hgBoot(this.checked)"> Enabled at boot (restart policy <code>${escapeHtml(st.boot_enabled ? 'unless-stopped' : 'no')}</code>)</label>
    </div>` : ''}
  </div>`;

  // ── Server card (/health)
  html += '<div class="card"><h3>Server</h3>';
  if (!st.running) html += `<p class="help">Not running${busyNote}.</p>`;
  else if (!h) html += `<div class="alert alert-warning">API is up but <code>/health</code> failed: ${escapeHtml(st.health_error || 'no response')} — the engine is probably still loading.</div>`;
  else html += `<table class="table kv"><tbody>
      <tr><td>Model</td><td><code>${escapeHtml(h.model || '')}</code>${h.vision ? ' · vision' : ''}${h.prompt_cache ? ' · prompt cache' : ''}</td></tr>
      <tr><td>Version</td><td>api ${escapeHtml(h.api_version || '?')} / engine ${escapeHtml(h.engine_version || '?')} ${h.version_match ? '<span class="status-badge green">match</span>' : '<span class="status-badge red">MISMATCH</span>'}</td></tr>
      <tr><td>Engine</td><td>${h.engine_responds ? `responds (probe ${hgNum(h.probe_s, 2)} s)` : '<span class="status-badge red">not responding</span>'}
        · ${h.busy ? `<strong>busy</strong> for ${hgNum(h.busy_for_s, 0)} s` : 'idle'} · ${h.in_flight || 0} in flight, ${h.queued || 0} queued</td></tr>
      <tr><td>Context</td><td>${(h.context || 0).toLocaleString()} per request · ${h.slots} slots · KV pool ${(h.kv_pool_positions || 0).toLocaleString()} positions${h.rope_scaling ? ` · rope ${escapeHtml(String(h.rope_scaling))}` : ''}</td></tr>
      <tr><td>Request defaults</td><td>max_tokens ${h.max_tokens_default} (cap ${h.max_tokens_cap}) · reasoning effort <strong>${escapeHtml(h.reasoning_effort_default || '?')}</strong> <span class="help">(a request that sends its own value wins)</span> · drafter ${escapeHtml(h.drafter_default || '?')}</td></tr>
    </tbody></table>`;
  html += '</div>';

  // ── Throughput card (/metrics + /cache)
  html += '<div class="card"><h3>Throughput</h3>';
  if (!m) html += `<p class="help">${st.running ? `No metrics (${escapeHtml(st.metrics_error || 'scrape failed')})` : 'Not running'}.</p>`;
  else html += `<div class="stat-row">
      <div class="stat"><div class="stat-v">${hgNum(m.tps, 1)}</div><div class="stat-l">tokens/s (last scrape)</div></div>
      <div class="stat"><div class="stat-v">${m.requests_processing}</div><div class="stat-l">in flight</div></div>
      <div class="stat"><div class="stat-v">${m.requests_deferred}</div><div class="stat-l">queued</div></div>
      <div class="stat"><div class="stat-v">${hgPct(m.kv_cache_usage_ratio)}</div><div class="stat-l">KV pool used</div></div>
      <div class="stat"><div class="stat-v">${hgPct(m.draft_accept_ratio)}</div><div class="stat-l">draft acceptance</div></div>
      <div class="stat"><div class="stat-v">${cache ? hgPct(cache.hit_rate) : '—'}</div><div class="stat-l">prompt-cache hits</div></div>
    </div>
    <p class="help">${m.requests_total.toLocaleString()} requests · ${m.tokens_predicted_total.toLocaleString()} tokens generated · ${m.prompt_tokens_total.toLocaleString()} prompt tokens (${m.prompt_tokens_cached_total.toLocaleString()} served from cache)${m.structured_requests_total ? ` · ${m.structured_requests_total} structured` : ''}</p>
    <div id="hg-spark"></div>`;
  html += '</div>';

  // ── Engine budget (its own startup arithmetic)
  if (st.budget && st.budget.length) {
    html += `<div class="card"><h3>Engine memory budget</h3><p class="help">The engine's own accounting from its startup log — what the KV pool, slots and weights cost on this box.</p>
      <pre class="log" style="white-space:pre-wrap">${escapeHtml(st.budget.join('\n'))}</pre></div>`;
  }

  html += `<div class="toolbar"><button class="btn btn-sm btn-outline" onclick="hgLogs('engine')">Engine log</button>
    <button class="btn btn-sm btn-outline" onclick="hgLogs('api')">API log</button></div>`;
  el.innerHTML = html;
  if (m) hgFillSpark();
}

async function hgFillSpark() {
  try {
    const r = await API.get('/api/history?metric=halogen_tps&label=halogen&since=86400');
    const pts = (r.points || []).filter(p => p && p[1] != null);
    const box = $('hg-spark');
    if (box && pts.length > 2) box.innerHTML = `<span class="help">tokens/s, last 24 h</span> ${sparkline(pts)}`;
  } catch (e) { /* history off or empty — the card stands without it */ }
}

async function hgAction(action) {
  const warn = { down: 'Remove the Halogen containers (docker compose down)? The model unloads; volumes and the stack directory are untouched.',
                 restart: 'Restart the whole stack? The engine reloads the model (seconds warm, minutes from a cold page cache).',
                 stop: 'Stop the Halogen stack? Clients on this node lose the inference API until it is started again.' }[action];
  if (warn && !confirm(warn)) return;
  $('page-content').insertAdjacentHTML('afterbegin', `<div class="alert alert-info" id="hg-busy">Running docker compose ${escapeHtml(action)}… ${action === 'up' || action === 'start' || action === 'restart' ? 'the engine pins ~68 GiB of weights, so this can take a while.' : ''}</div>`);
  try {
    await API.post('/api/halogen/action', { action });
  } catch (e) { alert(e.message); }
  page_halogen();
}

async function hgBoot(enabled) {
  try { await API.post('/api/halogen/boot', { enabled }); }
  catch (e) { alert(e.message); }
  page_halogen();
}

async function hgLogs(service) {
  try {
    const r = await API.get(`/api/halogen/logs?service=${encodeURIComponent(service)}&tail=300`);
    openModal(`Halogen ${service} log — ${r.container}`, `<pre class="log" style="max-height:60vh;overflow:auto">${escapeHtml(r.logs || '(empty)')}</pre>`, { wide: true });
  } catch (e) { alert(e.message); }
}

// Dashboard card — from the /api/summary block (docker state + one /metrics
// scrape; no engine ping on the 30 s poll).
function dashcard_halogen(ctx) {
  const h = ctx.s.halogen;
  if (!h) return '';            // not deployed here — no card
  const dot = !h.running ? 'red' : h.healthy ? 'green' : 'yellow';
  const val = h.running && h.tps != null ? `${hgNum(h.tps, 1)} <span class="card-unit">tok/s</span>` : `<span class="card-unit">${h.running ? 'starting' : 'stopped'}</span>`;
  const sub = h.running
    ? `${h.requests_processing || 0} in flight${h.requests_deferred ? ` · <strong>${h.requests_deferred} queued</strong>` : ''} · KV ${hgPct(h.kv_cache_usage_ratio)}`
    : `engine ${escapeHtml(h.engine || '?')} · api ${escapeHtml(h.api || '?')}`;
  return `
    <div class="card card-link" onclick="showPage('halogen')">
      <div class="card-head"><span class="status-dot ${dot}"></span>Halogen</div>
      <div class="card-value">${val}</div>
      <div class="card-sub">${sub}</div>
      <div class="card-sub">${escapeHtml(h.image_tag ? 'image ' + h.image_tag : '')}${h.boot_enabled === false ? ' · <span class="status-badge yellow">not at boot</span>' : ''}${h.requests_total != null ? ` · ${h.requests_total} requests` : ''}</div>
    </div>`;
}
