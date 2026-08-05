// Declarative plugin widgets — the frontend of the plugin.yaml tier.
//
// Pages arrive as sanitized JSON in the /api/modules/nav manifest
// (ui_pages: argv/sudo/timeout stripped server-side); this file manufactures
// a page_<uid> function per declared page and renders the closed widget set.
// The frontend NEVER sends commands — only the page/widget indices baked
// into each widget (_pi/_wi), resolved server-side against the manifest.
//
// XSS posture (applies to every renderer below, and to any widget type added
// later): manifest strings are third-party input. Free text -> escapeHtml or
// textContent, never raw innerHTML; identifiers/indices -> validated or
// numeric-coerced before interpolation; URLs -> scheme-checked; no widget
// type accepts raw HTML; tables render DECLARED columns only.

function registerDeclarativePages(mods) {
  (mods || []).forEach(m => {
    if (!NAV_ID_RE.test(String(m.id || ''))) return;
    (m.ui_pages || []).forEach(pg => {
      if (!PAGE_ID_RE.test(String(pg.id || ''))) return;
      if (typeof window['page_' + pg.id] !== 'function')  // real JS always wins
        window['page_' + pg.id] = () => renderDeclarativePage(m.id, pg);
      else if (!window['page_' + pg.id]._declarative)
        console.warn('declarative page skipped (name taken):', pg.id);
      window['page_' + pg.id]._declarative = true;
    });
  });
}

async function renderDeclarativePage(modId, pg) {
  if (pg.admin_only && currentRole !== 'admin') return adminOnlyPage(pg.label || pg.id);
  const timers = [];
  _pageCleanup = () => timers.forEach(clearInterval);
  const ws = (pg.widgets || []).filter(w => !(w.admin_only && currentRole !== 'admin'));
  $('page-content').innerHTML = `<h2>${escapeHtml(pg.label || pg.id)}</h2>` +
    ws.map((w, i) =>
      `${w.title ? `<h3>${escapeHtml(w.title)}</h3>` : ''}` +
      `<div class="widget-body" id="wb-${i}"></div>`).join('');
  ws.forEach((w, i) => {
    const draw = () => {
      const el = $(`wb-${i}`);
      if (!el) return;                       // navigated away
      const r = WIDGET_RENDERERS[w.type];
      if (r) r(el, modId, w);
      else el.innerHTML = '<p class="help">Unknown widget type</p>';
    };
    draw();
    const s = Number(w.refresh_seconds) || 0;
    if (s > 0) timers.push(setInterval(draw, Math.max(5, s) * 1000));
  });
}

function _wgUrl(modId, w) {
  return `/api/plugin/${modId}/widget/${Number(w._pi) || 0}/${Number(w._wi) || 0}`;
}

function _wgError(el, msg) {
  el.innerHTML = `<div class="alert alert-warning">${escapeHtml(msg || 'failed')}</div>`;
}

// minimal escaped markup for `markdown` widgets: paragraphs, **bold**,
// `code`. Escape FIRST; no raw-HTML pathway exists.
function _wgMarkdown(text) {
  return String(text || '').split(/\n\s*\n/).map(par =>
    `<p class="help">${escapeHtml(par)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\n/g, '<br>')}</p>`).join('');
}

const WIDGET_RENDERERS = {
  markdown(el, modId, w) {
    el.innerHTML = _wgMarkdown(w.content);
  },

  async service_status(el, modId, w) {
    let d = null;
    try { d = await API.get(_wgUrl(modId, w)); } catch (e) { return _wgError(el, e.message); }
    if (!d.success) return _wgError(el, d.error);
    const up = d.active === 'active';
    const ctl = currentRole === 'admin' ? `
      <div class="toolbar" style="margin-top:8px">
        <button class="btn btn-sm" onclick="wgSvc('${jsArg(d.unit)}','start')">Start</button>
        <button class="btn btn-sm" onclick="wgSvc('${jsArg(d.unit)}','stop')">Stop</button>
        <button class="btn btn-sm" onclick="wgSvc('${jsArg(d.unit)}','restart')">Restart</button>
      </div>` : '';
    el.innerHTML = `
      <div class="card">
        <div class="card-head"><span class="status-dot ${up ? 'green' : 'red'}"></span>${escapeHtml(d.unit)}</div>
        <div class="card-value" style="font-size:1.2em">${escapeHtml(d.active)}</div>
        <div class="card-sub">boot: ${escapeHtml(d.enabled)}</div>
        ${ctl}
      </div>`;
  },

  async command_table(el, modId, w) {
    let d = null;
    try { d = await API.get(_wgUrl(modId, w)); } catch (e) { return _wgError(el, e.message); }
    if (!d.success) return _wgError(el, d.error);
    const head = (d.columns || []).map(c => `<th>${escapeHtml(c)}</th>`).join('');
    const body = (d.rows || []).map(r =>
      `<tr>${r.map(c => `<td>${escapeHtml(String(c))}</td>`).join('')}</tr>`).join('') ||
      `<tr><td colspan="${(d.columns || []).length || 1}">${escapeHtml(w.empty || 'No data')}</td></tr>`;
    const note = (d.truncated ? 'output truncated · ' : '') +
                 (d.parse_errors ? `${d.parse_errors} unparsed line(s)` : '');
    el.innerHTML = `<table class="table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>` +
      (note ? `<p class="help">${escapeHtml(note)}</p>` : '');
  },

  action_button(el, modId, w) {
    const cls = w.danger ? 'btn btn-sm btn-danger' : 'btn btn-sm';
    el.innerHTML = `<div class="toolbar">
      <button class="${cls}" onclick="wgAction('${jsArg(modId)}',${Number(w._pi) || 0},${Number(w._wi) || 0},this)">${escapeHtml(w.label || 'Run')}</button>
      <span class="help wg-action-result"></span></div>`;
    el.querySelector('button')._wg = w;   // confirm text looked up on click
  },

  async log_tail(el, modId, w) {
    let d = null;
    try { d = await API.get(_wgUrl(modId, w)); } catch (e) { return _wgError(el, e.message); }
    if (!d.success) return _wgError(el, d.error);
    let pre = el.querySelector('pre.raw-output');
    if (!pre) {
      el.innerHTML = '<pre class="raw-output" style="max-height:380px;overflow:auto"></pre>';
      pre = el.querySelector('pre.raw-output');
    }
    pre.textContent = d.logs || 'No log entries.';   // textContent, never HTML
    pre.scrollTop = pre.scrollHeight;
  },

  link(el, modId, w) {
    if (!/^https?:\/\//.test(String(w.url || ''))) return _wgError(el, 'invalid link');
    el.innerHTML = `<p><a class="btn btn-sm btn-outline" target="_blank" rel="noopener"
      href="${escapeHtml(w.url)}">${escapeHtml(w.label || w.url)} ↗</a></p>`;
  },

  iframe(el, modId, w) {
    const src = String(w.src || w.url || '');
    if (!/^https?:\/\//.test(src) && !(src.startsWith('/') && !src.startsWith('//')))
      return _wgError(el, 'invalid iframe source');
    const h = Math.min(2000, Math.max(100, Number(w.height) || 480));
    el.innerHTML = `<iframe class="widget-frame" sandbox="allow-scripts allow-same-origin allow-forms"
      style="width:100%;border:0;height:${h}px" src="${escapeHtml(src)}"></iframe>`;
  },
};

async function wgSvc(unit, action) {
  try {
    await API.post(`/api/service/${encodeURIComponent(unit)}/${action}`, {});
    setTimeout(() => renderPage(currentPage), 600);
  } catch (e) { alert(e.message); }
}

async function wgAction(modId, pi, wi, btn) {
  const w = (btn && btn._wg) || {};
  if (w.confirm !== false && !confirm(`Run "${w.label || 'action'}"?`)) return;
  const out = btn.parentElement.querySelector('.wg-action-result');
  out.textContent = 'running…';
  try {
    const r = await API.post(`/api/plugin/${modId}/action/${pi}/${wi}`, {});
    out.textContent = r.success ? '✓ done' : '✗ failed';
    if (r.output || r.error)
      openModal(w.label || 'Action result',
        `<pre class="raw-output">${escapeHtml(r.output || r.error)}</pre>`);
  } catch (e) {
    out.textContent = '✗ ' + e.message;
  }
}

// Dashboard card for declarative plugins (service_status only in v1): reuses
// the summary payload the dashboard already fetched (ctx.svc keys are module
// ids — the plugin's service entry rides SYSTEM_SERVICES like any builtin's).
async function renderDeclarativeCard(modId, spec, ctx) {
  if (!spec || spec.type !== 'service_status') return '';
  const svc = (ctx && ctx.svc && ctx.svc[modId]) || null;
  const m = ((uiManifest && uiManifest.modules) || []).find(x => x.id === modId);
  const first = m && m.ui_pages && m.ui_pages[0] && m.ui_pages[0].id;
  if (!svc || !first || !PAGE_ID_RE.test(first)) return '';
  const up = svc.active === 'active';
  return `
    <div class="card card-link" onclick="showPage('${first}')">
      <div class="card-head"><span class="status-dot ${up ? 'green' : 'red'}"></span>${escapeHtml(m.label || modId)}</div>
      <div class="card-value" style="font-size:1.2em">${up ? 'up' : 'down'}</div>
      <div class="card-sub">${escapeHtml(svc.name || spec.unit || '')}</div>
    </div>`;
}
