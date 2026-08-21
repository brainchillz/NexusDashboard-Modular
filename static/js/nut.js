// NUT (Network UPS Tools) — two modules, two pages, one file.
//
//   page_nut     the `nut` module: the UPS SERVER half (ups.conf devices,
//                upsd listeners, upsd.users), on the node with the cable.
//   page_upsmon  the `upsmon` module: the CLIENT half (MONITOR lines,
//                shutdown timing, the notification matrix), on every node
//                the UPS feeds.
//
// They share this file because they share a vocabulary — status flags,
// battery formatting, the nut.conf MODE picker — not because they share a
// toggle: each page is hidden by its own module id (data-module in the nav
// manifest), so a client node can run one without the other.
//
// Passwords are WRITE-ONLY throughout: the API never sends one back, so an
// edit form starts blank and "leave blank to keep the current password" is
// the literal behaviour, not a hint.
const NUTAPI = '/api/nut';
const UPSMONAPI = '/api/upsmon';

let _nutState = null;        // last GET /api/nut
let _upsmonState = null;     // last GET /api/upsmon
let _nutEditDevice = null;   // device name the modal is editing (null = add)
let _nutEditUser = null;
let _upsmonEditSystem = null;

// ─── Shared presentation ────────────────────────────────────────────
// ups.status flags, worst first — the badge shows the one that matters.
const UPS_FLAG_CLASS = {
  OB: 'red', LB: 'red', FSD: 'red', ALARM: 'red', OVER: 'red', OFF: 'red',
  RB: 'yellow', CAL: 'yellow', BYPASS: 'yellow', DISCHRG: 'yellow',
  TRIM: 'yellow', BOOST: 'yellow', TEST: 'yellow',
  OL: 'green', CHRG: 'green', HB: 'green',
};
const UPS_FLAG_ORDER = ['FSD', 'LB', 'OB', 'ALARM', 'OVER', 'OFF', 'RB',
                        'CAL', 'BYPASS', 'DISCHRG', 'TRIM', 'BOOST', 'TEST',
                        'CHRG', 'HB', 'OL'];

function nutStatusBadge(live) {
  if (!live) return '';
  if (!live.reachable)
    return `<span class="status-badge red">unreachable</span>`;
  const flags = live.status || [];
  if (!flags.length) return '<span class="status-badge gray">no status</span>';
  const worst = UPS_FLAG_ORDER.find(f => flags.includes(f)) || flags[0];
  const cls = UPS_FLAG_CLASS[worst] || 'gray';
  return `<span class="status-badge ${cls}">${escapeHtml(live.status_text || flags.join(' '))}</span>`;
}

function nutRuntime(sec) {
  if (sec == null) return '—';
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${sec % 60}s`;
}

function nutNum(v, unit, digits) {
  if (v == null) return '—';
  return `${Number(v).toFixed(digits == null ? 0 : digits)}${unit || ''}`;
}

// The battery/load block shared by both pages' device rows.
function nutLiveCells(live) {
  if (!live || !live.reachable)
    return `<td colspan="3"><span class="help">${escapeHtml((live && live.error) || 'no data')}</span></td>`;
  return `
    <td>${live.charge == null ? '—' : usageBar(live.charge)}</td>
    <td>${nutRuntime(live.runtime)}</td>
    <td>${nutNum(live.load, '%')}${live.realpower_nominal && live.load != null
        ? ` <span class="help">(~${Math.round(live.realpower_nominal * live.load / 100)}W)</span>` : ''}</td>`;
}

function nutServiceBadge(svc) {
  if (!svc) return '';
  const active = svc.active === 'active';
  const cls = active ? 'green' : svc.active === 'inactive' ? 'gray' : 'red';
  return `<span class="status-badge ${cls}">${escapeHtml(svc.active)}</span>` +
    (svc.enabled === 'enabled' ? '' :
      ` <span class="help">(${escapeHtml(svc.enabled)} at boot)</span>`);
}

async function nutServiceAction(unit, action) {
  try {
    await API.post(`/api/service/${encodeURIComponent(unit)}/${action}`, {});
  } catch (e) { alert(e.message); }
}

// nut.conf MODE picker — the same file from either page, so the helper text
// says what the OTHER half needs too.
function nutModeBlock(state, apiBase) {
  const modes = state.modes || [];
  return `
    <h3 style="margin-top:24px">NUT mode <span class="help">(nut.conf)</span></h3>
    <p class="help">Decides which NUT daemons this host starts:
      <code>standalone</code> = UPS attached, no other node uses it;
      <code>netserver</code> = UPS attached and shared over the network;
      <code>netclient</code> = no UPS attached, monitors another host's;
      <code>none</code> = NUT installed but inert. One setting per host —
      both UPS pages write the same file.</p>
    <div class="toolbar">
      <select id="nut-mode" class="form-control" style="max-width:220px">
        ${modes.map(m => `<option value="${escapeHtml(m)}"
          ${state.mode === m ? 'selected' : ''}>${escapeHtml(m)}</option>`).join('')}
      </select>
      <button class="btn" onclick="nutSaveMode('${jsArg(apiBase)}')">Apply mode</button>
    </div>`;
}

async function nutSaveMode(apiBase) {
  try {
    await API.post(apiBase + '/mode', { mode: $('nut-mode').value });
    alert('NUT mode applied. A daemon that was not already running is not '
        + 'started by this change — enable it on the Services page.');
    renderPage(currentPage);
  } catch (e) { alert(e.message); }
}

// Shared "NUT isn't here" page body. `what` names the half that is missing so
// the operator installs the right package.
function nutMissingPage(title, what, pkg) {
  $('page-content').innerHTML = `<h2>${escapeHtml(title)}</h2>
    <div class="alert alert-info">${escapeHtml(what)} is not installed on this host,
      so there is nothing to manage here. Install it
      (<code>apt install ${escapeHtml(pkg)}</code> or
      <code>dnf install ${escapeHtml(pkg)}</code>) and reload this page.</div>`;
}

function nutNotEditable(kind) {
  return `<div class="alert alert-warning">Changes are disabled: the root-owned
    NUT helper is not installed on this node. NUT's config files are
    <code>root:nut 0640</code>, so the dashboard needs it to read or write
    them. Fresh installs ship it; existing nodes get it from
    <code>deploy/fleet-deploy.sh --helpers</code>. ${escapeHtml(kind)} is shown
    read-only.</div>`;
}

// ─── UPS Server page (`nut` module) ─────────────────────────────────
async function page_nut() {
  if (currentRole !== 'admin') return adminOnlyPage('UPS Server');
  const s = await API.get(NUTAPI);
  _nutState = s;
  if (!s.installed && !(s.devices || []).length)
    return nutMissingPage('UPS Server', 'The NUT server (upsd)', 'nut-server');

  const devRows = (s.devices || []).map((d, i) => `<tr>
      <td><strong>${escapeHtml(d.name)}</strong>
        ${d.desc ? `<br><span class="help">${escapeHtml(d.desc)}</span>` : ''}</td>
      <td><code>${escapeHtml(d.driver)}</code><br><span class="help">${escapeHtml(d.port)}</span></td>
      <td>${nutStatusBadge(d.live)}</td>
      ${nutLiveCells(d.live)}
      <td>
        <button class="btn btn-sm btn-outline" onclick="nutVarsModal('${jsArg(d.name)}')">Variables</button>
        ${s.editable ? `<button class="btn btn-sm btn-outline" onclick="nutDeviceModal(${i})">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="nutDeviceDelete('${jsArg(d.name)}')">Delete</button>` : ''}</td>
    </tr>`).join('');

  const listenRows = (s.listen || []).map(l =>
    `<tr><td><code>${escapeHtml(l.address)}</code></td>
         <td>${escapeHtml(l.port || '3493')}</td></tr>`).join('');

  const userRows = (s.users || []).map((u, i) => `<tr>
      <td><strong>${escapeHtml(u.name)}</strong></td>
      <td>${u.password_set ? '<span class="status-badge green">set</span>'
                           : '<span class="status-badge red">missing</span>'}</td>
      <td>${escapeHtml(u.upsmon || '—')}</td>
      <td>${escapeHtml(u.actions || '')} ${escapeHtml(u.instcmds || '')}</td>
      <td>${s.editable ? `<button class="btn btn-sm btn-outline" onclick="nutUserModal(${i})">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="nutUserDelete('${jsArg(u.name)}')">Delete</button>` : ''}</td>
    </tr>`).join('');

  $('page-content').innerHTML = `
    <h2>UPS Server ${nutServiceBadge(s.services.server)}</h2>
    <p class="help">The NUT server half: one driver process per UPS attached to
      <em>this</em> machine, published to the network by <code>upsd</code> on
      port 3493. Other nodes read it with the UPS Monitor page. Config lives in
      <code>${escapeHtml(s.conf_dir)}</code>.</p>
    ${s.editable ? '' : nutNotEditable('Configuration')}
    ${s.config_readable ? '' : `<div class="alert alert-warning">The NUT config
      files could not be read on this node.</div>`}

    <h3>UPS devices <span class="help">(ups.conf)</span></h3>
    <table class="table">
      <thead><tr><th>Name</th><th>Driver / port</th><th>Status</th><th>Battery</th>
        <th>Runtime</th><th>Load</th><th></th></tr></thead>
      <tbody>${devRows || `<tr><td colspan="7">No UPS devices configured — add the
        one plugged into this machine.</td></tr>`}</tbody>
    </table>
    ${s.editable ? `<div class="toolbar">
      <button class="btn" onclick="nutDeviceModal()">+ Add UPS</button>
      <button class="btn btn-outline" onclick="nutServiceAction('nut-driver.target','restart')">Restart drivers</button>
    </div>` : ''}

    <h3 style="margin-top:24px">Server settings <span class="help">(upsd.conf)</span></h3>
    <p class="help">upsd listens on <code>127.0.0.1</code> only until a LAN
      address is added here — that is what lets other nodes monitor this UPS.
      Changing a listener restarts upsd.</p>
    <table class="table" style="max-width:420px">
      <thead><tr><th>Listen address</th><th>Port</th></tr></thead>
      <tbody>${listenRows || '<tr><td colspan="2">Defaults (localhost only)</td></tr>'}</tbody>
    </table>
    ${s.editable ? `<div class="toolbar">
      <button class="btn" onclick="nutServerModal()">Edit server settings</button></div>` : ''}

    <h3 style="margin-top:24px">Users <span class="help">(upsd.users)</span></h3>
    <p class="help">Credentials clients authenticate with. The usual shape is one
      <code>primary</code> user for this host's own upsmon (it is allowed to
      order the shutdown) and one <code>secondary</code> for every other node.</p>
    <table class="table">
      <thead><tr><th>User</th><th>Password</th><th>upsmon role</th><th>Actions / instcmds</th><th></th></tr></thead>
      <tbody>${userRows || '<tr><td colspan="5">No users defined — no client can connect.</td></tr>'}</tbody>
    </table>
    ${s.editable ? `<div class="toolbar">
      <button class="btn" onclick="nutUserModal()">+ Add user</button></div>` : ''}

    ${nutModeBlock(s, NUTAPI)}

    <h3 style="margin-top:24px">Services</h3>
    <table class="table" style="max-width:640px">
      <tbody>
        ${[['server', 'nut-server', 'UPS data server (upsd)'],
           ['enumerator', 'nut-driver-enumerator', 'Driver unit enumerator'],
           ['monitor', 'nut-monitor', 'UPS monitor (upsmon)']].map(([k, unit, label]) => `
          <tr><td>${escapeHtml(label)}<br><span class="help"><code>${escapeHtml(unit)}</code></span></td>
              <td>${nutServiceBadge(s.services[k])}</td>
              <td><button class="btn btn-sm btn-outline" onclick="nutServiceAction('${jsArg(unit)}','restart')">Restart</button></td></tr>`).join('')}
      </tbody>
    </table>`;
}

async function nutVarsModal(name) {
  let data;
  try { data = await API.get(`${NUTAPI}/ups/${encodeURIComponent(name)}`); }
  catch (e) { alert(e.message); return; }
  const rows = Object.keys(data.vars).sort().map(k =>
    `<tr><td><code>${escapeHtml(k)}</code></td><td>${escapeHtml(data.vars[k])}</td></tr>`).join('');
  openModal(`${name} — all variables`, `
    <p class="help">Everything this UPS reports, straight from
      <code>upsc</code>. Read-only.</p>
    <table class="table"><thead><tr><th>Variable</th><th>Value</th></tr></thead>
      <tbody>${rows}</tbody></table>`, { wide: true });
}

function nutExtraRows(extras) {
  return (extras || []).map((e, i) => `
    <div class="form-row" id="nut-extra-${i}" style="display:flex;gap:8px;margin-bottom:6px">
      <input class="form-control nut-extra-key" placeholder="vendorid" value="${escapeHtml(e.key)}">
      <input class="form-control nut-extra-val" placeholder="0764" value="${escapeHtml(e.value)}">
      <button class="btn btn-sm btn-danger" onclick="this.parentNode.remove()">&times;</button>
    </div>`).join('');
}

function nutAddExtraRow() {
  const box = $('nut-extras');
  const d = document.createElement('div');
  d.style.cssText = 'display:flex;gap:8px;margin-bottom:6px';
  d.innerHTML = `<input class="form-control nut-extra-key" placeholder="vendorid">
    <input class="form-control nut-extra-val" placeholder="0764">
    <button class="btn btn-sm btn-danger" onclick="this.parentNode.remove()">&times;</button>`;
  box.appendChild(d);
}

function nutDeviceModal(idx) {
  const s = _nutState || {};
  const d = idx !== undefined ? s.devices[idx] : null;
  _nutEditDevice = d ? d.name : null;
  const drivers = s.drivers || [];
  openModal(d ? `Edit UPS — ${d.name}` : 'Add UPS', `
    <div class="form-group"><label>Name <span class="help">(how NUT and every
      client refers to this UPS — it also names its systemd unit)</span></label>
      <input id="nut-dev-name" class="form-control" placeholder="cyberpower"
        value="${d ? escapeHtml(d.name) : ''}" ${d ? 'disabled' : ''}></div>
    <div class="form-group"><label>Driver</label>
      <select id="nut-dev-driver" class="form-control">
        ${drivers.map(x => `<option value="${escapeHtml(x)}"
          ${d && d.driver === x ? 'selected' : ''}>${escapeHtml(x)}</option>`).join('')}
      </select>
      <p class="help">Only drivers installed on this host are listed.
        <code>usbhid-ups</code> covers most modern USB units (APC, CyberPower,
        Eaton); <code>nutdrv_qx</code> covers the Megatec/Q1 clones.</p></div>
    <div class="form-group"><label>Port</label>
      <input id="nut-dev-port" class="form-control" placeholder="auto"
        value="${d ? escapeHtml(d.port) : 'auto'}">
      <p class="help"><code>auto</code> for USB, a device node like
        <code>/dev/ttyUSB0</code> for serial, or a hostname for the network
        drivers.</p></div>
    <div class="form-group"><label>Description</label>
      <input id="nut-dev-desc" class="form-control" placeholder="CyberPower in the rack"
        value="${d ? escapeHtml(d.desc) : ''}"></div>
    <div class="form-group"><label>Extra driver parameters</label>
      <p class="help">Anything else the driver needs — <code>vendorid</code>,
        <code>productid</code>, <code>offdelay</code>, SNMP <code>community</code>.
        Existing parameters are preserved; clearing a row removes it.</p>
      <div id="nut-extras">${nutExtraRows(d && d.extras)}</div>
      <button class="btn btn-sm btn-outline" onclick="nutAddExtraRow()">+ Parameter</button></div>
    <div class="alert alert-info">Saving restarts this host's UPS drivers, which
      briefly interrupts readings (monitoring clients ride it out).</div>
    <button class="btn" onclick="nutDeviceSave()">${d ? 'Save' : 'Add'}</button>
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>`);
}

function nutCollectExtras() {
  const keys = document.querySelectorAll('#nut-extras .nut-extra-key');
  const vals = document.querySelectorAll('#nut-extras .nut-extra-val');
  const out = [];
  keys.forEach((k, i) => {
    const key = k.value.trim();
    if (key) out.push({ key: key, value: (vals[i] ? vals[i].value : '').trim() });
  });
  return out;
}

async function nutDeviceSave() {
  const body = {
    name: _nutEditDevice || $('nut-dev-name').value.trim(),
    driver: $('nut-dev-driver').value,
    port: $('nut-dev-port').value.trim(),
    desc: $('nut-dev-desc').value.trim(),
    extras: nutCollectExtras(),
  };
  try {
    await API.post(NUTAPI + (_nutEditDevice ? '/device/update' : '/device'), body);
    closeModal();
    page_nut();
  } catch (e) { alert(e.message); }
}

async function nutDeviceDelete(name) {
  if (!confirm(`Remove UPS "${name}" from ups.conf? Its driver stops and any `
             + `client monitoring it loses contact.`)) return;
  try { await API.post(NUTAPI + '/device/delete', { name: name }); page_nut(); }
  catch (e) { alert(e.message); }
}

function nutServerModal() {
  const s = _nutState || {};
  const listen = (s.listen || []).length ? s.listen : [{ address: '127.0.0.1', port: '3493' }];
  openModal('Server settings', `
    <div class="form-group"><label>Listen addresses</label>
      <p class="help">One per line as <code>address port</code> (port optional,
        default 3493). Keep <code>127.0.0.1</code> so this host's own upsmon
        still connects, and add the LAN address other nodes should reach.</p>
      <textarea id="nut-listen" class="form-control" rows="4">${
        escapeHtml(listen.map(l => `${l.address}${l.port ? ' ' + l.port : ''}`).join('\n'))}</textarea></div>
    <div class="form-group"><label>MAXAGE <span class="help">(seconds a reading
      may go stale before upsd calls the data bad — blank uses NUT's default of 15)</span></label>
      <input id="nut-maxage" class="form-control" value="${escapeHtml(s.maxage || '')}"></div>
    <div class="form-group"><label>MAXCONN <span class="help">(maximum client
      connections — blank uses NUT's default)</span></label>
      <input id="nut-maxconn" class="form-control" value="${escapeHtml(s.maxconn || '')}"></div>
    <div class="alert alert-info">Saving restarts upsd — LISTEN sockets are bound
      at start, so a reload would not pick up an address change.</div>
    <button class="btn" onclick="nutServerSave()">Save</button>
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>`);
}

async function nutServerSave() {
  const listen = $('nut-listen').value.split('\n').map(l => l.trim()).filter(Boolean)
    .map(l => { const p = l.split(/\s+/); return { address: p[0], port: p[1] || '' }; });
  try {
    await API.post(NUTAPI + '/server', {
      listen: listen,
      maxage: $('nut-maxage').value.trim(),
      maxconn: $('nut-maxconn').value.trim(),
    });
    closeModal();
    page_nut();
  } catch (e) { alert(e.message); }
}

function nutUserModal(idx) {
  const s = _nutState || {};
  const u = idx !== undefined ? s.users[idx] : null;
  _nutEditUser = u ? u.name : null;
  openModal(u ? `Edit user — ${u.name}` : 'Add user', `
    <div class="form-group"><label>User name</label>
      <input id="nut-user-name" class="form-control" placeholder="upsmon"
        value="${u ? escapeHtml(u.name) : ''}" ${u ? 'disabled' : ''}></div>
    <div class="form-group"><label>Password</label>
      <input id="nut-user-pass" type="password" class="form-control" autocomplete="new-password"
        placeholder="${u && u.password_set ? 'unchanged' : ''}">
      <p class="help">${u && u.password_set
        ? 'Leave blank to keep the current password.'
        : 'Required.'} Clients must be given the same value.</p></div>
    <div class="form-group"><label>upsmon role</label>
      <select id="nut-user-role" class="form-control">
        <option value="">none — plain read-only client</option>
        <option value="primary" ${u && u.upsmon === 'primary' ? 'selected' : ''}>primary — may order the shutdown</option>
        <option value="secondary" ${u && u.upsmon === 'secondary' ? 'selected' : ''}>secondary — shuts itself down only</option>
      </select>
      <p class="help">Exactly one host should hold the primary credential: the one
        that powers the UPS off last.</p></div>
    <div class="form-group"><label>actions <span class="help">(e.g. <code>SET FSD</code>)</span></label>
      <input id="nut-user-actions" class="form-control" value="${u ? escapeHtml(u.actions || '') : ''}"></div>
    <div class="form-group"><label>instcmds <span class="help">(e.g. <code>ALL</code>
      or <code>test.battery.start</code>)</span></label>
      <input id="nut-user-instcmds" class="form-control" value="${u ? escapeHtml(u.instcmds || '') : ''}"></div>
    <button class="btn" onclick="nutUserSave()">${u ? 'Save' : 'Add'}</button>
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>`);
}

async function nutUserSave() {
  const body = {
    name: _nutEditUser || $('nut-user-name').value.trim(),
    password: $('nut-user-pass').value,
    upsmon: $('nut-user-role').value,
    actions: $('nut-user-actions').value.trim(),
    instcmds: $('nut-user-instcmds').value.trim(),
  };
  try { await API.post(NUTAPI + '/user', body); closeModal(); page_nut(); }
  catch (e) { alert(e.message); }
}

async function nutUserDelete(name) {
  if (!confirm(`Delete upsd user "${name}"? Any client using it loses access.`)) return;
  try { await API.post(NUTAPI + '/user/delete', { name: name }); page_nut(); }
  catch (e) { alert(e.message); }
}

// ─── UPS Monitor page (`upsmon` module) ─────────────────────────────
async function page_upsmon() {
  if (currentRole !== 'admin') return adminOnlyPage('UPS Monitor');
  const s = await API.get(UPSMONAPI);
  _upsmonState = s;
  if (!s.installed && !(s.monitors || []).length)
    return nutMissingPage('UPS Monitor', 'The NUT client (upsmon)', 'nut-client');

  const monRows = (s.monitors || []).map((m, i) => `<tr>
      <td><strong>${escapeHtml(m.ups)}</strong><br><span class="help">${escapeHtml(m.host)}</span></td>
      <td>${nutStatusBadge(m.live)}</td>
      ${nutLiveCells(m.live)}
      <td>${escapeHtml(m.type)} <span class="help">· power value ${escapeHtml(m.powervalue)}</span></td>
      <td>${escapeHtml(m.user)} ${m.password_set
        ? '' : '<span class="status-badge red">no password</span>'}</td>
      <td>${s.editable ? `<button class="btn btn-sm btn-outline" onclick="upsmonMonitorModal(${i})">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="upsmonMonitorDelete('${jsArg(m.system)}')">Delete</button>` : ''}</td>
    </tr>`).join('');

  const st = s.settings || {};
  const setting = (key, label, help) => `<tr>
      <td>${escapeHtml(label)}<br><span class="help">${help}</span></td>
      <td><code>${escapeHtml(st[key] || '')}</code>${st[key] ? '' :
        ' <span class="help">NUT default</span>'}</td></tr>`;

  $('page-content').innerHTML = `
    <h2>UPS Monitor ${nutServiceBadge(s.service)}</h2>
    <p class="help">Watches one or more UPSes — local or on another host — and
      shuts this machine down while there is still battery left. Every node on
      protected power wants this, whether or not it has a UPS attached.</p>
    ${s.editable ? '' : nutNotEditable('Configuration')}

    <h3>Monitored UPSes <span class="help">(upsmon.conf MONITOR)</span></h3>
    <table class="table">
      <thead><tr><th>UPS</th><th>Status</th><th>Battery</th><th>Runtime</th>
        <th>Load</th><th>Role</th><th>Login</th><th></th></tr></thead>
      <tbody>${monRows || `<tr><td colspan="8">Nothing monitored — add the UPS
        this machine is plugged into.</td></tr>`}</tbody>
    </table>
    ${s.editable ? `<div class="toolbar">
      <button class="btn" onclick="upsmonMonitorModal()">+ Monitor a UPS</button>
      <button class="btn btn-outline" onclick="nutServiceAction('nut-monitor','restart')">Restart upsmon</button>
    </div>` : ''}

    <h3 style="margin-top:24px">Shutdown &amp; timing</h3>
    <table class="table" style="max-width:720px"><tbody>
      ${setting('shutdowncmd', 'Shutdown command',
                'Run as root when the battery runs out. This is the whole point of upsmon.')}
      ${setting('minsupplies', 'MINSUPPLIES',
                'How many power sources must stay up before this host may keep running.')}
      ${setting('pollfreq', 'POLLFREQ', 'Seconds between normal polls.')}
      ${setting('pollfreqalert', 'POLLFREQALERT', 'Seconds between polls while on battery.')}
      ${setting('hostsync', 'HOSTSYNC', 'Seconds a primary waits for secondaries to disconnect.')}
      ${setting('deadtime', 'DEADTIME', 'Seconds of silence before a UPS is declared dead.')}
      ${setting('rbwarntime', 'RBWARNTIME', 'Seconds between "replace battery" reminders.')}
      ${setting('nocommwarntime', 'NOCOMMWARNTIME', 'Seconds between "cannot reach UPS" warnings.')}
      ${setting('finaldelay', 'FINALDELAY', 'Seconds between the last warning and the shutdown.')}
      ${setting('notifycmd', 'NOTIFYCMD', 'Program run for events flagged EXEC (often upssched).')}
      ${setting('powerdownflag', 'POWERDOWNFLAG', 'File that tells the shutdown scripts to kill UPS power.')}
      ${setting('run_as_user', 'RUN_AS_USER', 'Unprivileged user upsmon drops to.')}
    </tbody></table>
    ${s.editable ? `<div class="toolbar">
      <button class="btn" onclick="upsmonSettingsModal()">Edit shutdown &amp; timing</button></div>` : ''}

    <h3 style="margin-top:24px">Notifications</h3>
    <p class="help"><code>SYSLOG</code> writes to the system log, <code>WALL</code>
      messages every logged-in terminal, <code>EXEC</code> runs NOTIFYCMD. An event
      with nothing ticked is written as <code>IGNORE</code>.</p>
    <table class="table" style="max-width:720px">
      <thead><tr><th>Event</th><th>SYSLOG</th><th>WALL</th><th>EXEC</th></tr></thead>
      <tbody>${(s.notify_events || []).map(ev => {
        const flags = (s.notify || {})[ev.event] || [];
        const cell = f => `<td><input type="checkbox" data-nf-event="${escapeHtml(ev.event)}"
          data-nf-flag="${f}" ${flags.includes(f) ? 'checked' : ''}
          ${s.editable ? '' : 'disabled'}></td>`;
        return `<tr><td>${escapeHtml(ev.event)}<br><span class="help">${escapeHtml(ev.label)}</span></td>
          ${cell('SYSLOG')}${cell('WALL')}${cell('EXEC')}</tr>`;
      }).join('')}</tbody>
    </table>
    ${s.editable ? `<div class="toolbar">
      <button class="btn" onclick="upsmonNotifySave()">Save notifications</button></div>` : ''}

    ${nutModeBlock(s, UPSMONAPI)}`;
}

function upsmonMonitorModal(idx) {
  const s = _upsmonState || {};
  const m = idx !== undefined ? s.monitors[idx] : null;
  _upsmonEditSystem = m ? m.system : null;
  openModal(m ? `Edit monitor — ${m.system}` : 'Monitor a UPS', `
    <div class="form-group"><label>UPS</label>
      <input id="ups-system" class="form-control" placeholder="cyberpower@192.0.2.5"
        value="${m ? escapeHtml(m.system) : ''}">
      <p class="help">The UPS name as the server knows it, then <code>@</code> and
        the host running upsd — <code>@localhost</code> when the UPS is attached
        here. Use a hostname or IP that will not change.</p></div>
    <div class="form-group"><label>Role</label>
      <select id="ups-type" class="form-control">
        <option value="secondary" ${!m || m.type !== 'primary' ? 'selected' : ''}>secondary — shut this host down only</option>
        <option value="primary" ${m && m.type === 'primary' ? 'selected' : ''}>primary — also power the UPS off at the end</option>
      </select>
      <p class="help">Only the host the UPS is physically attached to should be
        primary.</p></div>
    <div class="form-group"><label>Power value</label>
      <input id="ups-powervalue" class="form-control" value="${m ? escapeHtml(m.powervalue) : '1'}">
      <p class="help">How many of this host's power supplies this UPS feeds. 1 for
        an ordinary machine; 0 for a UPS you want to watch but not depend on.</p></div>
    <div class="form-group"><label>User</label>
      <input id="ups-user" class="form-control" placeholder="upsmon-secondary"
        value="${m ? escapeHtml(m.user) : ''}">
      <p class="help">Must match a user in the server's <code>upsd.users</code>,
        with the matching primary/secondary role.</p></div>
    <div class="form-group"><label>Password</label>
      <input id="ups-pass" type="password" class="form-control" autocomplete="new-password"
        placeholder="${m && m.password_set ? 'unchanged' : ''}">
      <p class="help">${m && m.password_set
        ? 'Leave blank to keep the current password.' : 'Required.'}</p></div>
    <div class="alert alert-info">Saving restarts upsmon — a reload does not apply
      MONITOR changes, so restarting is the only way the change really takes
      effect.</div>
    <button class="btn" onclick="upsmonMonitorSave()">${m ? 'Save' : 'Add'}</button>
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>`);
}

async function upsmonMonitorSave() {
  const body = {
    system: $('ups-system').value.trim(),
    type: $('ups-type').value,
    powervalue: $('ups-powervalue').value.trim(),
    user: $('ups-user').value.trim(),
    password: $('ups-pass').value,
  };
  if (_upsmonEditSystem) body.original = _upsmonEditSystem;
  try {
    await API.post(UPSMONAPI + (_upsmonEditSystem ? '/monitor/update' : '/monitor'), body);
    closeModal();
    page_upsmon();
  } catch (e) { alert(e.message); }
}

async function upsmonMonitorDelete(system) {
  if (!confirm(`Stop monitoring ${system}? This host will no longer shut itself `
             + `down when that UPS runs out of battery.`)) return;
  try { await API.post(UPSMONAPI + '/monitor/delete', { system: system }); page_upsmon(); }
  catch (e) { alert(e.message); }
}

function upsmonSettingsModal() {
  const st = (_upsmonState || {}).settings || {};
  const num = (key, label, help) => `
    <div class="form-group"><label>${escapeHtml(label)}</label>
      <input id="ups-${key}" class="form-control" value="${escapeHtml(st[key] || '')}">
      <p class="help">${help} Blank uses NUT's own default.</p></div>`;
  openModal('Shutdown & timing', `
    <div class="form-group"><label>Shutdown command</label>
      <input id="ups-shutdowncmd" class="form-control" placeholder="/usr/sbin/shutdown -h +0"
        value="${escapeHtml(st.shutdowncmd || '')}">
      <p class="help">Run as root when the UPS says the battery is gone. An
        absolute path with plain arguments — no shell operators.</p></div>
    ${num('minsupplies', 'MINSUPPLIES', 'Power sources that must stay up.')}
    ${num('pollfreq', 'POLLFREQ', 'Seconds between polls on line power.')}
    ${num('pollfreqalert', 'POLLFREQALERT', 'Seconds between polls on battery.')}
    ${num('hostsync', 'HOSTSYNC', 'Seconds a primary waits for secondaries.')}
    ${num('deadtime', 'DEADTIME', 'Seconds before an unresponsive UPS is called dead.')}
    ${num('rbwarntime', 'RBWARNTIME', 'Seconds between replace-battery warnings.')}
    ${num('nocommwarntime', 'NOCOMMWARNTIME', 'Seconds between no-contact warnings.')}
    ${num('finaldelay', 'FINALDELAY', 'Seconds between the final warning and shutdown.')}
    <div class="form-group"><label>NOTIFYCMD</label>
      <input id="ups-notifycmd" class="form-control" placeholder="/usr/sbin/upssched"
        value="${escapeHtml(st.notifycmd || '')}">
      <p class="help">Runs for every event flagged EXEC below. Its own config
        (<code>upssched.conf</code>) is not managed here.</p></div>
    <div class="form-group"><label>POWERDOWNFLAG</label>
      <input id="ups-powerdownflag" class="form-control" placeholder="/etc/killpower"
        value="${escapeHtml(st.powerdownflag || '')}"></div>
    <div class="form-group"><label>RUN_AS_USER</label>
      <input id="ups-run_as_user" class="form-control" placeholder="nut"
        value="${escapeHtml(st.run_as_user || '')}"></div>
    <div class="alert alert-info">Saving restarts upsmon.</div>
    <button class="btn" onclick="upsmonSettingsSave()">Save</button>
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>`);
}

async function upsmonSettingsSave() {
  const keys = ['minsupplies', 'pollfreq', 'pollfreqalert', 'hostsync', 'deadtime',
                'rbwarntime', 'nocommwarntime', 'finaldelay', 'shutdowncmd',
                'notifycmd', 'powerdownflag', 'run_as_user'];
  const body = {};
  keys.forEach(k => { body[k] = $('ups-' + k).value.trim(); });
  try { await API.post(UPSMONAPI + '/settings', body); closeModal(); page_upsmon(); }
  catch (e) { alert(e.message); }
}

async function upsmonNotifySave() {
  const notify = {};
  document.querySelectorAll('[data-nf-event]').forEach(cb => {
    const ev = cb.dataset.nfEvent;
    if (!notify[ev]) notify[ev] = [];
    if (cb.checked) notify[ev].push(cb.dataset.nfFlag);
  });
  try {
    await API.post(UPSMONAPI + '/notify', { notify: notify });
    alert('Notification settings saved (upsmon restarted).');
    page_upsmon();
  } catch (e) { alert(e.message); }
}

// ─── Dashboard card ─────────────────────────────────────────────────
// Contributed by the `upsmon` module, not `nut`: the monitor half is what runs
// on every node, so this is the card that is actually useful fleet-wide.
function dashcard_upsmon(ctx) {
  const u = ctx.s.upsmon || {};
  if (!u.monitors) return '';        // nothing monitored — no card
  const bad = u.low_battery || u.on_battery;
  const dotCls = u.low_battery ? 'red' : u.on_battery ? 'yellow'
    : u.reachable ? 'green' : 'red';
  const headline = u.charge == null ? (u.reachable ? 'no reading' : 'unreachable')
    : `${Math.round(u.charge)}`;
  return `
    <div class="card card-link" onclick="showPage('upsmon')">
      <div class="card-head"><span class="status-dot ${dotCls}"></span>UPS</div>
      <div class="card-value">${escapeHtml(headline)}${u.charge == null ? ''
        : ' <span class="card-unit">% battery</span>'}</div>
      <div class="card-sub">${escapeHtml(u.status || (u.reachable ? '' : 'no contact'))}
        ${u.runtime != null ? ` · ${nutRuntime(u.runtime)} left` : ''}</div>
      <div class="card-sub">${escapeHtml(u.ups || '')}${bad ? ' · ON BATTERY' : ''}</div>
      ${u.charge == null ? '' : usageBar(u.charge)}
    </div>`;
}
