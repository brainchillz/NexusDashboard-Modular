async function page_iscsi() {
  const [targets, backstores, sessions] = await Promise.all([
    API.get('/api/iscsi/targets'),
    API.get('/api/iscsi/backstores'),
    API.get('/api/iscsi/sessions')
  ]);
  iscsiBackstores = backstores.backstores || [];

  const targetRows = (targets.targets || []).map(iqn => {
    const q = jsArg(iqn);
    return `<tr>
      <td><code>${escapeHtml(iqn)}</code></td>
      <td>
        <button class="btn btn-sm" onclick="iscsiManage('${q}')">Manage</button>
        <button class="btn btn-sm btn-danger" onclick="iscsiDeleteTarget('${q}')">Delete</button>
      </td>
    </tr>`;
  }).join('');

  const bsRows = iscsiBackstores.map(b => `<tr>
      <td>${escapeHtml(b.type)}</td>
      <td>${escapeHtml(b.name)}</td>
      <td>${escapeHtml(b.size || '')}</td>
      <td>${b.in_use ? '<span class="status-badge gray">in use</span>' : '<span class="status-badge green">free</span>'}</td>
      <td><button class="btn btn-sm btn-danger" onclick="iscsiDeleteBackstore('${jsArg(b.type)}','${jsArg(b.name)}',${b.in_use ? 'true' : 'false'})">Delete</button></td>
    </tr>`).join('');

  const sessRows = (sessions.sessions || []).map(s => `<tr>
      <td><code>${escapeHtml(s.target)}</code></td>
      <td><code>${escapeHtml(s.initiator)}</code></td>
      <td>${escapeHtml(s.type)}</td>
    </tr>`).join('');

  $('page-content').innerHTML = `
    <h2>iSCSI Targets</h2>
    <div class="toolbar">
      <button class="btn" onclick="iscsiCreateTarget()">+ New Target</button>
      <button class="btn btn-outline" onclick="iscsiCreateBackstore()">+ New Backstore</button>
    </div>
    <p class="help">Config is saved automatically after every change.</p>
    <h3>Targets</h3>
    <table class="table">
      <thead><tr><th>Target IQN</th><th>Actions</th></tr></thead>
      <tbody>${targetRows || '<tr><td colspan="2">No targets configured</td></tr>'}</tbody>
    </table>
    <h3>Backstores</h3>
    <table class="table">
      <thead><tr><th>Type</th><th>Name</th><th>Size</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>${bsRows || '<tr><td colspan="5">No backstores</td></tr>'}</tbody>
    </table>
    <h3>Connected Initiators</h3>
    <table class="table">
      <thead><tr><th>Target</th><th>Initiator</th><th>Type</th></tr></thead>
      <tbody>${sessRows || '<tr><td colspan="3">No connected initiators</td></tr>'}</tbody>
    </table>
  `;
}

// ── Per-target management modal ──
async function iscsiManage(iqn) {
  let d;
  try { d = await API.get(`/api/iscsi/targets/${encodeURIComponent(iqn)}`); }
  catch(e) { alert(e.message); return; }
  const q = jsArg(iqn);

  const modeBadge = d.shared
    ? '<span class="status-badge green">Shared (any initiator)</span>'
    : '<span class="status-badge gray">Restricted (ACLs only)</span>';
  const authBadge = d.auth ? ' <span class="status-badge yellow">CHAP on</span>' : '';

  const lunRows = (d.luns || []).map(l => `<tr>
      <td>${escapeHtml(l.lun)}</td>
      <td>${escapeHtml(l.backstore || '')}</td>
      <td><button class="btn btn-sm btn-danger" onclick="iscsiDelLun('${q}','${jsArg(l.lun)}')">Delete</button></td>
    </tr>`).join('');

  const aclRows = (d.acls || []).map(a => `<tr>
      <td><code>${escapeHtml(a.initiator)}</code></td>
      <td>
        <button class="btn btn-sm" onclick="iscsiChap('${q}','${jsArg(a.initiator)}')">CHAP</button>
        <button class="btn btn-sm btn-danger" onclick="iscsiDelAcl('${q}','${jsArg(a.initiator)}')">Delete</button>
      </td>
    </tr>`).join('');

  const portalRows = (d.portals || []).map(p => `<tr>
      <td>${escapeHtml(p.portal)}</td>
      <td><button class="btn btn-sm btn-danger" onclick="iscsiDelPortal('${q}','${jsArg(p.ip)}','${jsArg(p.port)}')">Delete</button></td>
    </tr>`).join('');

  openModal('Manage ' + iqn, `
    <div class="form-group">Access mode: ${modeBadge}${authBadge}
      <button class="btn btn-sm btn-outline" style="margin-left:8px" onclick="iscsiSetMode('${q}','${d.shared ? 'restricted' : 'shared'}')">
        Switch to ${d.shared ? 'Restricted' : 'Shared'}</button>
    </div>
    <p class="help">${d.shared
      ? 'Shared: any initiator may connect and read/write — the usual default for Proxmox/VMware clusters. ACLs below are optional.'
      : 'Restricted: only the initiator ACLs below may connect (optionally with CHAP).'}</p>

    <h4>LUNs <button class="btn btn-sm" onclick="iscsiAddLun('${q}')">+ Add</button></h4>
    <table class="table"><thead><tr><th>LUN</th><th>Backstore</th><th></th></tr></thead>
      <tbody>${lunRows || '<tr><td colspan="3">No LUNs</td></tr>'}</tbody></table>

    <h4 style="margin-top:16px">Initiator ACLs <button class="btn btn-sm" onclick="iscsiAddAcl('${q}')">+ Add</button></h4>
    <table class="table"><thead><tr><th>Initiator IQN</th><th></th></tr></thead>
      <tbody>${aclRows || '<tr><td colspan="2">No ACLs</td></tr>'}</tbody></table>

    <h4 style="margin-top:16px">Portals <button class="btn btn-sm" onclick="iscsiAddPortal('${q}')">+ Add</button></h4>
    <table class="table"><thead><tr><th>Portal</th><th></th></tr></thead>
      <tbody>${portalRows || '<tr><td colspan="2">No portals</td></tr>'}</tbody></table>
  `, {wide:true});
}

async function iscsiSetMode(iqn, mode) {
  try {
    const r = await API.post(`/api/iscsi/targets/${encodeURIComponent(iqn)}/mode`, { mode });
    if (!r.success) alert(r.stderr || 'Failed');
    iscsiManage(iqn);
  } catch(e) { alert(e.message); }
}

async function iscsiDeleteTarget(iqn) {
  if (!confirm(`Delete iSCSI target "${iqn}"?`)) return;
  try { await API.delete(`/api/iscsi/targets/${encodeURIComponent(iqn)}`); page_iscsi(); }
  catch(e) { alert(e.message); }
}

async function iscsiDeleteBackstore(type, name, inUse) {
  const warn = inUse ? '\nThis backstore is attached to a LUN — detach it first or this will fail.' : '';
  if (!confirm(`Delete backstore "${type}/${name}"?${warn}`)) return;
  try { await API.delete(`/api/iscsi/backstores/${encodeURIComponent(type)}/${encodeURIComponent(name)}`); page_iscsi(); }
  catch(e) { alert(e.message); }
}

function iscsiAddLun(iqn) {
  const bsItems = iscsiBackstores.map(b =>
    ({ value: `${b.type}:${b.name}`, label: `${b.type}/${b.name}${b.in_use ? ' (in use)' : ''}` }));
  openModal('Add LUN(s)', `
    <p>Target: <strong>${escapeHtml(iqn)}</strong></p>
    <div class="form-group"><label>Backstores (check one or more)</label>
      ${checkboxList('lun-bs', bsItems, 'No backstores available')}
    </div>
    <p class="help">Each selected backstore is attached as its own LUN (auto-numbered).</p>
    <button class="btn" onclick="iscsiDoAddLun('${jsArg(iqn)}')">Add</button>
  `);
}

async function iscsiDoAddLun(iqn) {
  const sels = checkedValues('lun-bs').filter(Boolean);
  if (!sels.length) { alert('Select at least one backstore'); return; }
  try {
    for (const sel of sels) {
      const [backstore_type, backstore_name] = sel.split(':');
      const r = await API.post('/api/iscsi/luns', { iqn, backstore_type, backstore_name });
      if (!r.success) alert(`${backstore_name}: ${r.stderr || r.stdout || 'failed'}`);
    }
    iscsiManage(iqn);
  } catch(e) { alert(e.message); }
}

async function iscsiDelLun(iqn, lun) {
  if (!confirm(`Delete ${lun} from ${iqn}?`)) return;
  try {
    const r = await API.post('/api/iscsi/luns/delete', { iqn, lun });
    if (!r.success) alert(r.stderr || 'Failed');
    iscsiManage(iqn);
  } catch(e) { alert(e.message); }
}

function iscsiAddAcl(iqn) {
  openModal('Add Initiator ACL', `
    <p>Target: <strong>${escapeHtml(iqn)}</strong></p>
    <div class="form-group"><label>Initiator IQN</label><input id="acl-iqn" class="form-control" placeholder="iqn.1993-08.org.debian:01:pve1"></div>
    <p class="help">Add one ACL per hypervisor that should access this target. All ACLs share the same LUN(s).</p>
    <button class="btn" onclick="iscsiDoAddAcl('${jsArg(iqn)}')">Add ACL</button>
  `);
}

async function iscsiDoAddAcl(iqn) {
  const initiator_iqn = $('acl-iqn').value.trim();
  if (!initiator_iqn) { alert('Initiator IQN required'); return; }
  try {
    const r = await API.post('/api/iscsi/acls', { iqn, initiator_iqn });
    if (!r.success) { alert(r.stderr || r.stdout || 'Failed'); return; }
    iscsiManage(iqn);
  } catch(e) { alert(e.message); }
}

async function iscsiDelAcl(iqn, initiator_iqn) {
  if (!confirm(`Remove ACL ${initiator_iqn}?`)) return;
  try {
    const r = await API.post('/api/iscsi/acls/delete', { iqn, initiator_iqn });
    if (!r.success) alert(r.stderr || 'Failed');
    iscsiManage(iqn);
  } catch(e) { alert(e.message); }
}

function iscsiChap(iqn, initiator_iqn) {
  openModal('CHAP for ' + initiator_iqn, `
    <p>Target: <strong>${escapeHtml(iqn)}</strong></p>
    <div class="form-group"><label>CHAP username</label><input id="chap-user" class="form-control" autocomplete="off"></div>
    <div class="form-group"><label>CHAP password</label><input id="chap-pass" class="form-control" autocomplete="off"></div>
    <p class="help">Letters, digits and . _ : + - (12+ chars recommended). Enabling CHAP turns on authentication for this target.</p>
    <button class="btn" onclick="iscsiDoChap('${jsArg(iqn)}','${jsArg(initiator_iqn)}',false)">Set CHAP</button>
    <button class="btn btn-outline" onclick="iscsiDoChap('${jsArg(iqn)}','${jsArg(initiator_iqn)}',true)">Clear</button>
  `);
}

async function iscsiDoChap(iqn, initiator_iqn, clear) {
  const body = { iqn, initiator_iqn };
  if (clear) { body.clear = true; }
  else {
    body.userid = $('chap-user').value.trim();
    body.password = $('chap-pass').value.trim();
    if (!body.userid || !body.password) { alert('Username and password required'); return; }
  }
  try {
    const r = await API.post('/api/iscsi/acls/chap', body);
    if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; }
    closeModal(); iscsiManage(iqn);
  } catch(e) { alert(e.message); }
}

function iscsiAddPortal(iqn) {
  openModal('Add Portal', `
    <p>Target: <strong>${escapeHtml(iqn)}</strong></p>
    <div class="form-group"><label>IP Address</label><input id="portal-ip" class="form-control" value="0.0.0.0"></div>
    <div class="form-group"><label>Port</label><input id="portal-port" class="form-control" value="3260"></div>
    <button class="btn" onclick="iscsiDoAddPortal('${jsArg(iqn)}')">Add Portal</button>
  `);
}

async function iscsiDoAddPortal(iqn) {
  const ip = $('portal-ip').value.trim();
  const port = $('portal-port').value.trim();
  try {
    const r = await API.post('/api/iscsi/portals', { iqn, ip, port });
    if (!r.success) { alert(r.stderr || r.stdout || 'Failed'); return; }
    iscsiManage(iqn);
  } catch(e) { alert(e.message); }
}

async function iscsiDelPortal(iqn, ip, port) {
  if (!confirm(`Delete portal ${ip}:${port}?`)) return;
  try {
    const r = await API.post('/api/iscsi/portals/delete', { iqn, ip, port });
    if (!r.success) alert(r.stderr || 'Failed');
    iscsiManage(iqn);
  } catch(e) { alert(e.message); }
}

function iscsiCreateTarget() {
  openModal('Create iSCSI Target', `
    <p>An iSCSI Qualified Name (IQN) uniquely identifies a target.</p>
    <div class="form-group"><label>IQN</label><input id="is-iqn" class="form-control" value="iqn.2025-01.com.example:target1" placeholder="iqn.YEAR-MM.domain.reverse:identifier"></div>
    <div class="form-group"><label>Access mode</label>
      <select id="is-mode" class="form-control">
        <option value="shared">Shared — any initiator (Proxmox/VMware clusters)</option>
        <option value="restricted">Restricted — explicit ACLs only</option>
      </select>
    </div>
    <p class="help">Shared lets multiple hypervisors connect to the same target/LUN out of the box. Use Restricted (+ CHAP) for untrusted networks.</p>
    <button class="btn" onclick="iscsiDoCreateTarget()">Create Target</button>
  `);
}

async function iscsiDoCreateTarget() {
  const iqn = $('is-iqn').value.trim();
  const access_mode = $('is-mode').value;
  if (!iqn) { alert('IQN required'); return; }
  try {
    const r = await API.post('/api/iscsi/targets', { iqn, access_mode });
    closeModal();
    page_iscsi();
    if (!r.success) alert(r.stderr || r.stdout || 'Failed');
  } catch(e) { alert(e.message); }
}

async function iscsiCreateBackstore() {
  const [disks, zvols] = await Promise.all([API.get('/api/disks'), API.get('/api/zfs/zvols')]);
  const diskOpts = (disks.devices||[]).filter(d => d.type === 'disk').map(d =>
    `<option value="/dev/${d.name}">/dev/${d.name} (${d.size})</option>`
  ).join('');
  const zvolOpts = (zvols||[]).map(z =>
    `<option value="${escapeHtml(z.path)}">${escapeHtml(z.name)} (${escapeHtml(z.volsize)})</option>`
  ).join('');
  openModal('Create Backstore', `
    <div class="form-group"><label>Type</label>
      <select id="is-btype" class="form-control" onchange="iscsiBsType(this.value)">
        <option value="fileio">FileIO (file-backed)</option>
        <option value="block">Block device</option>
        <option value="zvol">ZFS volume (ZVOL)</option>
      </select>
    </div>
    <div class="form-group"><label>Name</label><input id="is-bname" class="form-control" placeholder="vmstore"></div>
    <div id="is-path-group">
      <div class="form-group"><label>File Path</label><input id="is-path" class="form-control" value="/var/lib/iscsi-disks/vmstore.img" placeholder="/path/to/file.img"></div>
      <div class="form-group"><label>Size</label><input id="is-size" class="form-control" placeholder="100G"></div>
    </div>
    <div id="is-dev-group" style="display:none">
      <div class="form-group"><label>Block Device</label>
        <select id="is-dev" class="form-control">${diskOpts || '<option value="">No free disks</option>'}</select>
      </div>
    </div>
    <div id="is-zvol-group" style="display:none">
      <div class="form-group"><label>ZFS Volume</label>
        <select id="is-zvol" class="form-control">${zvolOpts || '<option value="">No ZVOLs — create one on the ZFS page</option>'}</select>
      </div>
    </div>
    <button class="btn" onclick="iscsiDoCreateBackstore()">Create Backstore</button>
  `);
}

function iscsiBsType(v) {
  $('is-path-group').style.display = v === 'fileio' ? 'block' : 'none';
  $('is-dev-group').style.display = v === 'block' ? 'block' : 'none';
  $('is-zvol-group').style.display = v === 'zvol' ? 'block' : 'none';
}

async function iscsiDoCreateBackstore() {
  const sel = $('is-btype').value;
  const name = $('is-bname').value.trim();
  let type = sel, path = '', size = '';
  if (sel === 'fileio') { path = $('is-path').value.trim(); size = $('is-size').value.trim(); }
  else if (sel === 'block') { type = 'block'; path = $('is-dev')?.value || ''; }
  else if (sel === 'zvol') { type = 'block'; path = $('is-zvol')?.value || ''; }
  if (!name) { alert('Name required'); return; }
  if (!path) { alert('Select/enter a backing path'); return; }
  try {
    const r = await API.post('/api/iscsi/backstores', { type, name, path, size });
    closeModal();
    page_iscsi();
    if (!r.success) alert(r.stderr || r.stdout || 'Failed');
  } catch(e) { alert(e.message); }
}

// ─── NFS ────────────────────────────────────────────────
const NFS_DEFAULT_OPTS = 'rw,sync,no_subtree_check,no_root_squash';
let nfsExports = [];

async function page_nfs() {
  const [exports, exportfs, clients] = await Promise.all([
    API.get('/api/nfs/exports'),
    API.get('/api/nfs/exportfs'),
    API.get('/api/nfs/clients')
  ]);
  nfsExports = exports;
  let rows = exports.map(ex => {
    const clientStr = (ex.clients || []).map(c => `${c.host}(${c.options})`).join(', ');
    return `<tr>
      <td>${escapeHtml(ex.path)}</td>
      <td>${escapeHtml(clientStr)}</td>
      <td>
        <button class="btn btn-sm" onclick="nfsEditExport('${jsArg(ex.path)}')">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="nfsDeleteExport('${jsArg(ex.path)}')">Remove</button>
      </td>
    </tr>`;
  }).join('');
  $('page-content').innerHTML = `
    <h2>NFS Exports</h2>
    <div class="toolbar"><button class="btn" onclick="nfsExportModal()">+ New Export</button></div>
    <table class="table">
      <thead><tr><th>Path</th><th>Clients</th><th>Actions</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="3">No exports configured</td></tr>'}</tbody>
    </table>
    <h3>Active Client Mounts</h3>
    <pre class="raw-output">${escapeHtml(clients.clients || 'No active client mounts')}</pre>
    <h3>Active Exports</h3>
    <pre class="raw-output">${escapeHtml(exportfs.exports || 'No active exports')}</pre>
  `;
}

// Build one client row (host + options + remove). Used for create and edit.
function nfsClientRowHtml(host, options) {
  return `<div class="nf-client-row" style="display:flex;gap:8px;margin-bottom:6px">
    <input class="form-control nf-host" placeholder="* or 192.168.1.0/24" value="${escapeHtml(host || '')}" style="flex:1">
    <input class="form-control nf-opts" placeholder="${NFS_DEFAULT_OPTS}" value="${escapeHtml(options || '')}" style="flex:2">
    <button class="btn btn-sm btn-danger" onclick="this.parentNode.remove()">✕</button>
  </div>`;
}

function nfsAddClientRow(host, options) {
  const div = document.createElement('div');
  div.innerHTML = nfsClientRowHtml(host, options);
  $('nf-clients').appendChild(div.firstElementChild);
}

function nfsExportModal(existing) {
  const isEdit = !!existing;
  const clients = (existing && existing.clients && existing.clients.length)
    ? existing.clients
    : [{ host: '*', options: NFS_DEFAULT_OPTS }];
  openModal(isEdit ? 'Edit NFS Export' : 'Create NFS Export', `
    <div class="form-group"><label>Path to export</label>
      <input id="nf-path" class="form-control" placeholder="/srv/nfs/share"
        value="${escapeHtml(existing ? existing.path : '')}" ${isEdit ? 'readonly' : ''}></div>
    <label>Clients <button class="btn btn-sm" onclick="nfsAddClientRow('','${NFS_DEFAULT_OPTS}')">+ Add client</button></label>
    <div id="nf-clients" style="margin:8px 0">${clients.map(c => nfsClientRowHtml(c.host, c.options)).join('')}</div>
    <p class="help">One row per client/network. Common options:
      <code>rw</code>/<code>ro</code>, <code>sync</code>/<code>async</code>,
      <code>no_root_squash</code>/<code>root_squash</code>/<code>all_squash</code>,
      <code>no_subtree_check</code>.</p>
    <button class="btn" onclick="nfsSaveExport()">${isEdit ? 'Save Export' : 'Create Export'}</button>
  `);
}

function nfsEditExport(path) {
  const ex = nfsExports.find(e => e.path === path);
  nfsExportModal(ex || { path, clients: [] });
}

async function nfsSaveExport() {
  const path = $('nf-path').value.trim();
  if (!path) { alert('Path required'); return; }
  const clients = [];
  document.querySelectorAll('#nf-clients .nf-client-row').forEach(row => {
    const host = row.querySelector('.nf-host').value.trim();
    const options = row.querySelector('.nf-opts').value.trim() || NFS_DEFAULT_OPTS;
    if (host) clients.push({ host, options });
  });
  if (!clients.length) { alert('Add at least one client'); return; }
  try {
    const r = await API.post('/api/nfs/exports', { path, clients });
    closeModal();
    page_nfs();
    if (!r.success) alert(r.stderr || 'Failed');
  } catch(e) { alert(e.message); }
}

async function nfsDeleteExport(path) {
  if (!confirm(`Remove NFS export "${path}"?`)) return;
  try {
    await API.delete(`/api/nfs/exports/${encodeURIComponent(path)}`);
    page_nfs();
  } catch(e) { alert(e.message); }
}

// ─── SMB ────────────────────────────────────────────────
let smbState = { users: [], groups: [] };

async function page_smb() {
  const [shares, status, users, groups, homes, glob, registry] = await Promise.all([
    API.get('/api/smb/shares'),
    API.get('/api/smb/status'),
    API.get('/api/smb/users'),
    API.get('/api/smb/groups'),
    API.get('/api/smb/homes'),
    API.get('/api/smb/global'),
    API.get('/api/smb/registry')
  ]);
  smbState = { users, groups, shares, glob, registry };
  const showBackend = registry.accessible && (registry.enabled || registry.share_count > 0);

  const shareRows = shares.map(s => {
    const vfsBadges = Object.entries(s.vfs || {}).filter(([, on]) => on)
      .map(([k]) => `<span class="status-badge gray" style="font-size:10px">${k === 'shadow_copy' ? 'prev-ver' : k === 'time_machine' ? 'timemachine' : k}</span>`).join(' ');
    const disabled = s.available === 'no';
    const backendBadge = showBackend ? ` <span class="status-badge ${s.backend === 'registry' ? 'yellow' : 'gray'}" style="font-size:10px">${s.backend === 'registry' ? 'registry' : 'smb.conf'}</span>` : '';
    return `<tr${disabled ? ' style="opacity:0.55"' : ''}>
      <td><strong>${escapeHtml(s.name)}</strong>${backendBadge}${disabled ? ' <span class="status-badge gray">disabled</span>' : ''}</td>
      <td>${escapeHtml(s.path)}</td>
      <td>${escapeHtml(s.read_only)}</td>
      <td>${escapeHtml(s.valid_users || '-')} ${vfsBadges}</td>
      <td>
        <button class="btn btn-sm" onclick="smbShareModal('${jsArg(s.name)}')">Edit</button>
        <button class="btn btn-sm" onclick="smbToggleShare('${jsArg(s.name)}')">${disabled ? 'Enable' : 'Disable'}</button>
        <button class="btn btn-sm btn-danger" onclick="smbDeleteShare('${jsArg(s.name)}')">Remove</button>
      </td>
    </tr>`;
  }).join('');

  const userRows = users.map(u => `<tr>
      <td>${escapeHtml(u.username)}</td>
      <td><span class="status-badge ${u.enabled ? 'green' : 'gray'}">${u.enabled ? 'enabled' : 'disabled'}</span></td>
      <td>
        <button class="btn btn-sm" onclick="smbUserToggle('${jsArg(u.username)}',${u.enabled})">${u.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn btn-sm" onclick="smbUserPassword('${jsArg(u.username)}')">Password</button>
        <button class="btn btn-sm btn-danger" onclick="smbDeleteUser('${jsArg(u.username)}')">Delete</button>
      </td>
    </tr>`).join('');

  const groupRows = groups.map(g => `<tr>
      <td>${escapeHtml(g.name)}</td>
      <td>${g.members.map(escapeHtml).join(', ') || '—'}</td>
      <td>
        <button class="btn btn-sm" onclick="smbGroupMembers('${jsArg(g.name)}')">Members</button>
        <button class="btn btn-sm btn-danger" onclick="smbDeleteGroup('${jsArg(g.name)}')">Delete</button>
      </td>
    </tr>`).join('');

  $('page-content').innerHTML = `
    <h2>SMB/CIFS</h2>
    <div class="toolbar">
      <button class="btn" onclick="smbCreateShare()">+ New Share</button>
      <button class="btn btn-outline" onclick="smbDatasetWizard()">+ Share on ZFS Dataset</button>
      <button class="btn btn-outline" onclick="smbGlobalModal()">Samba Settings</button>
    </div>
    <table class="table">
      <thead><tr><th>Share</th><th>Path</th><th>Read Only</th><th>Valid Users / Features</th><th>Actions</th></tr></thead>
      <tbody>${shareRows || '<tr><td colspan="5">No shares configured</td></tr>'}</tbody>
    </table>

    <h3>Home Directories</h3>
    <p class="help">Auto-expose each user's home directory as <code>\\\\server\\username</code>.</p>
    <button class="btn ${homes.enabled ? 'btn-danger' : ''}" onclick="smbToggleHomes(${homes.enabled})">
      ${homes.enabled ? 'Disable' : 'Enable'} Home Directories</button>
    <span class="status-badge ${homes.enabled ? 'green' : 'gray'}" style="margin-left:8px">${homes.enabled ? 'enabled' : 'disabled'}</span>

    <h3 style="margin-top:24px">Users <button class="btn btn-sm" onclick="smbAddUser()">+ Add User</button></h3>
    <table class="table">
      <thead><tr><th>Username</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>${userRows || '<tr><td colspan="3">No SMB users</td></tr>'}</tbody>
    </table>

    <h3 style="margin-top:24px">Groups <button class="btn btn-sm" onclick="smbAddGroup()">+ Add Group</button></h3>
    <p class="help">Use a group in a share's Valid Users as <code>@groupname</code>.</p>
    <table class="table">
      <thead><tr><th>Group</th><th>Members</th><th>Actions</th></tr></thead>
      <tbody>${groupRows || '<tr><td colspan="3">No groups</td></tr>'}</tbody>
    </table>

    <h3 style="margin-top:24px">Active Connections</h3>
    <table class="table">
      <thead><tr><th>User</th><th>Machine</th><th>Dialect</th><th>Encryption</th></tr></thead>
      <tbody>${(status.sessions || []).map(c => `<tr><td>${escapeHtml(c.username)}</td><td>${escapeHtml(c.machine)}</td><td>${escapeHtml(c.dialect)}</td><td>${escapeHtml(c.encryption)}</td></tr>`).join('') || '<tr><td colspan="4">No active sessions</td></tr>'}</tbody>
    </table>
    <p class="help">${(status.tcons || []).length} share connection(s), ${status.open_files || 0} open file(s).</p>
  `;
}

// ── Global Samba settings ──
function smbGlobalModal() {
  const g = smbState.glob || {};
  const opt = (cur, val, label) => `<option value="${val}" ${cur === val ? 'selected' : ''}>${label}</option>`;
  openModal('Samba Global Settings', `
    <div class="form-group"><label>Workgroup</label><input id="g-workgroup" class="form-control" value="${escapeHtml(g.workgroup || '')}" placeholder="WORKGROUP"></div>
    <div class="form-group"><label>Server description</label><input id="g-string" class="form-control" value="${escapeHtml(g['server string'] || '')}"></div>
    <div class="form-group"><label>Guest mapping</label>
      <select id="g-mtg" class="form-control">
        ${opt(g['map to guest'], '', '(leave default)')}${opt(g['map to guest'], 'Never', 'Never (no guest)')}
        ${opt(g['map to guest'], 'Bad User', 'Bad User (unknown user → guest)')}${opt(g['map to guest'], 'Bad Password', 'Bad Password')}
      </select></div>
    <div class="form-group"><label>Minimum protocol</label>
      <select id="g-minproto" class="form-control">
        ${opt(g['server min protocol'], '', '(leave default)')}${opt(g['server min protocol'], 'NT1', 'SMB1 / NT1 (legacy + some guest setups; insecure)')}
        ${opt(g['server min protocol'], 'SMB2', 'SMB2')}${opt(g['server min protocol'], 'SMB3', 'SMB3 (most secure)')}
      </select>
      <p class="help">SMB1 is insecure but needed for some old devices and certain unauthenticated guest access.</p>
    </div>
    <div class="form-group"><label>Encryption</label>
      <select id="g-enc" class="form-control">
        ${opt(g['smb encrypt'], '', '(leave default)')}${opt(g['smb encrypt'], 'off', 'Off')}${opt(g['smb encrypt'], 'desired', 'Desired')}${opt(g['smb encrypt'], 'required', 'Required')}
      </select></div>
    <div class="form-group"><label>Server signing</label>
      <select id="g-sign" class="form-control">
        ${opt(g['server signing'], '', '(leave default)')}${opt(g['server signing'], 'auto', 'Auto')}${opt(g['server signing'], 'mandatory', 'Mandatory')}${opt(g['server signing'], 'disabled', 'Disabled')}
      </select></div>
    <button class="btn" onclick="smbSaveGlobal()">Save Settings</button>
  `);
}

async function smbSaveGlobal() {
  try {
    const r = await API.post('/api/smb/global', {
      workgroup: $('g-workgroup').value, 'server string': $('g-string').value,
      'map to guest': $('g-mtg').value, 'server min protocol': $('g-minproto').value,
      'smb encrypt': $('g-enc').value, 'server signing': $('g-sign').value
    });
    if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; }
    closeModal();
    page_smb();
  } catch(e) { alert(e.message); }
}

// ── New share on a ZFS dataset (wizard) ──
async function smbDatasetWizard() {
  let targets = [];
  try { targets = await API.get('/api/zfs/datasets/all'); } catch(e) {}
  const pools = targets.filter(t => t.is_pool);
  const poolOpts = pools.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join('');
  openModal('New Share on a ZFS Dataset', `
    <p class="help">Creates a dataset, shares it, and (optionally) turns on auto-snapshots + Previous Versions — the full stack in one step.</p>
    <div class="form-group"><label>Pool</label><select id="wz-pool" class="form-control">${poolOpts || '<option value="">No pools</option>'}</select></div>
    <div class="form-group"><label>Dataset / share name</label><input id="wz-name" class="form-control" placeholder="media"></div>
    <div class="form-group"><label>Quota (optional)</label><input id="wz-quota" class="form-control" placeholder="500G"></div>
    <div class="form-group"><label>Valid users (optional; <code>@group</code> allowed)</label><input id="wz-users" class="form-control"></div>
    <label style="display:block"><input type="checkbox" id="wz-recycle" checked> Recycle bin</label>
    <label style="display:block"><input type="checkbox" id="wz-snap" checked> Auto-snapshots (keep 14 daily / 8 weekly)</label>
    <label style="display:block"><input type="checkbox" id="wz-shadow" checked> Previous Versions (needs auto-snapshots)</label>
    <label style="display:block"><input type="checkbox" id="wz-tm"> macOS / Time Machine</label>
    <button class="btn" onclick="smbDoDatasetWizard()">Create</button>
  `);
}

async function smbDoDatasetWizard() {
  const pool = $('wz-pool').value, name = $('wz-name').value.trim();
  if (!pool || !name) { alert('Pool and name required'); return; }
  const dataset = `${pool}/${name}`;
  const path = `/${dataset}`;
  try {
    let r = await API.post('/api/zfs/datasets', { name: dataset, properties: { compression: 'lz4' } });
    if (!r.success) { alert('Dataset: ' + (r.stderr || 'failed')); return; }
    const quota = $('wz-quota').value.trim();
    if (quota) await API.put(`/api/zfs/datasets/${encodeURIComponent(dataset)}/properties`, { property: 'quota', value: quota });
    const wantSnap = $('wz-snap').checked;
    const wantShadow = $('wz-shadow').checked && wantSnap;
    r = await API.post('/api/smb/shares', {
      name, path, read_only: 'no', valid_users: $('wz-users').value,
      vfs: { recycle: $('wz-recycle').checked, shadow_copy: wantShadow, time_machine: $('wz-tm').checked }
    });
    if (!r.success) { alert('Share: ' + (r.error || r.stderr || 'failed')); return; }
    if (wantSnap) {
      await API.post('/api/snapshots/schedules', {
        dataset, recursive: false, enabled: true, keep: { hourly: 0, daily: 14, weekly: 8, monthly: 0 }
      });
    }
    closeModal();
    alert(`Share "${name}" created on ${dataset}.`);
    page_smb();
  } catch(e) { alert(e.message); }
}

function smbCreateShare() { smbShareModal(); }

function smbShareModal(name) {
  const s = name ? (smbState.shares.find(x => x.name === name) || {}) : {};
  const v = s.vfs || {};
  const val = (k, d = '') => escapeHtml(s[k] != null ? s[k] : d);
  const sel = (cur, opt) => cur === opt ? 'selected' : '';
  const chk = b => b ? 'checked' : '';
  const reg = smbState.registry || {};
  // Registry-backed nodes (Cockpit file-sharing style): choose the store on
  // create, keep the share where it lives on edit.
  let backendField = '';
  if (name) {
    backendField = `<input type="hidden" id="sm-backend" value="${s.backend || 'file'}">`;
  } else if (reg.accessible && reg.enabled) {
    backendField = `<div class="form-group"><label>Store in</label>
      <select id="sm-backend" class="form-control">
        <option value="registry">Samba registry (Cockpit-compatible)</option>
        <option value="file">smb.conf</option>
      </select></div>`;
  }
  openModal(name ? 'Edit Share: ' + name : 'Create SMB Share', `
    <div class="form-group"><label>Share Name</label><input id="sm-name" class="form-control" value="${val('name')}" ${name ? 'readonly' : ''} placeholder="sharename"></div>
    ${backendField}
    <div class="form-group"><label>Path</label><input id="sm-path" class="form-control" value="${val('path')}" placeholder="/srv/smb/share"></div>
    <div class="form-group"><label>Comment</label><input id="sm-comment" class="form-control" value="${val('comment')}"></div>
    <div style="display:flex;gap:12px;flex-wrap:wrap">
      <div class="form-group"><label>Read only</label><select id="sm-ro" class="form-control"><option value="no" ${sel(s.read_only, 'no')}>No</option><option value="yes" ${sel(s.read_only, 'yes')}>Yes</option></select></div>
      <div class="form-group"><label>Guest OK</label><select id="sm-guest" class="form-control"><option value="no" ${sel(s.guest_ok, 'no')}>No</option><option value="yes" ${sel(s.guest_ok, 'yes')}>Yes</option></select></div>
      <div class="form-group"><label>Browseable</label><select id="sm-browse" class="form-control"><option value="yes" ${sel(s.browseable, 'yes')}>Yes</option><option value="no" ${sel(s.browseable, 'no')}>No</option></select></div>
    </div>
    <h4>Access control <span class="help">(users, or <code>@group</code>; space/comma separated)</span></h4>
    <div class="form-group"><label>Valid users</label><input id="sm-valid" class="form-control" value="${val('valid_users')}" placeholder="alice @staff"></div>
    <div style="display:flex;gap:12px;flex-wrap:wrap">
      <div class="form-group" style="flex:1"><label>Write list</label><input id="sm-write" class="form-control" value="${val('write_list')}"></div>
      <div class="form-group" style="flex:1"><label>Read-only list</label><input id="sm-read" class="form-control" value="${val('read_list')}"></div>
    </div>
    <div style="display:flex;gap:12px;flex-wrap:wrap">
      <div class="form-group" style="flex:1"><label>Hosts allow</label><input id="sm-hallow" class="form-control" value="${val('hosts_allow')}" placeholder="192.168.1.0/24"></div>
      <div class="form-group" style="flex:1"><label>Hosts deny</label><input id="sm-hdeny" class="form-control" value="${val('hosts_deny')}"></div>
    </div>
    <div style="display:flex;gap:12px;flex-wrap:wrap">
      <div class="form-group"><label>Force user</label><input id="sm-fuser" class="form-control" value="${val('force_user')}"></div>
      <div class="form-group"><label>Force group</label><input id="sm-fgroup" class="form-control" value="${val('force_group')}"></div>
      <div class="form-group"><label>Create mask</label><input id="sm-cmask" class="form-control" value="${val('create_mask')}" placeholder="0664" style="width:90px"></div>
      <div class="form-group"><label>Dir mask</label><input id="sm-dmask" class="form-control" value="${val('directory_mask')}" placeholder="2775" style="width:90px"></div>
    </div>
    <h4>Features</h4>
    <label style="display:block"><input type="checkbox" id="sm-recycle" ${chk(v.recycle)}> Recycle bin (deleted files kept in .recycle)</label>
    <label style="display:block"><input type="checkbox" id="sm-shadow" ${chk(v.shadow_copy)}> Previous Versions (shadow copy over ZFS auto-snapshots)</label>
    <label style="display:block"><input type="checkbox" id="sm-tm" ${chk(v.time_machine)}> macOS / Time Machine support</label>
    <label style="display:block"><input type="checkbox" id="sm-audit" ${chk(v.audit)}> Access audit logging</label>
    <p class="help" style="margin-top:8px">Users in the lists must exist (Users section below). Previous Versions needs an enabled Auto-Snapshot schedule on this dataset.</p>
    <button class="btn" onclick="smbSaveShare()">${name ? 'Save' : 'Create'} Share</button>
  `);
}

async function smbSaveShare() {
  const name = $('sm-name').value.trim();
  const path = $('sm-path').value.trim();
  if (!name || !path) { alert('Name and path required'); return; }
  try {
    const r = await API.post('/api/smb/shares', {
      name, path,
      backend: $('sm-backend') ? $('sm-backend').value : '',
      comment: $('sm-comment').value, read_only: $('sm-ro').value, guest_ok: $('sm-guest').value,
      browseable: $('sm-browse').value, valid_users: $('sm-valid').value, write_list: $('sm-write').value,
      read_list: $('sm-read').value, hosts_allow: $('sm-hallow').value, hosts_deny: $('sm-hdeny').value,
      force_user: $('sm-fuser').value, force_group: $('sm-fgroup').value,
      create_mask: $('sm-cmask').value, directory_mask: $('sm-dmask').value,
      vfs: { recycle: $('sm-recycle').checked, shadow_copy: $('sm-shadow').checked,
             time_machine: $('sm-tm').checked, audit: $('sm-audit').checked }
    });
    if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; }
    closeModal();
    page_smb();
  } catch(e) { alert(e.message); }
}

async function smbToggleShare(name) {
  try {
    const r = await API.post(`/api/smb/shares/${encodeURIComponent(name)}/toggle`, {});
    if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; }
    page_smb();
  } catch(e) { alert(e.message); }
}

async function smbDeleteShare(name) {
  if (!confirm(`Remove SMB share "${name}"?`)) return;
  try {
    await API.delete(`/api/smb/shares/${encodeURIComponent(name)}`);
    page_smb();
  } catch(e) { alert(e.message); }
}

// ── Users ──
function smbAddUser() {
  openModal('Create SMB User', `
    <div class="form-group"><label>Username</label><input id="su-name" class="form-control" placeholder="username"></div>
    <div class="form-group"><label>Password</label><input id="su-pass" class="form-control" type="password"></div>
    <p class="help">Any password is accepted — no complexity rules.</p>
    <button class="btn" onclick="smbDoAddUser()">Create User</button>
  `);
}

async function smbDoAddUser() {
  const username = $('su-name').value.trim();
  const password = $('su-pass').value;
  if (!username || !password) { alert('Username and password required'); return; }
  try {
    const r = await API.post('/api/smb/users', { username, password });
    closeModal();
    page_smb();
    if (!r.success) alert(r.stderr || 'Failed');
  } catch(e) { alert(e.message); }
}

async function smbUserToggle(username, enabled) {
  try {
    await API.post(`/api/smb/users/${encodeURIComponent(username)}/${enabled ? 'disable' : 'enable'}`, {});
    page_smb();
  } catch(e) { alert(e.message); }
}

function smbUserPassword(username) {
  openModal('Set password: ' + username, `
    <div class="form-group"><label>New password</label><input id="su-newpass" class="form-control" type="password"></div>
    <button class="btn" onclick="smbDoUserPassword('${jsArg(username)}')">Set Password</button>
  `);
}

async function smbDoUserPassword(username) {
  const password = $('su-newpass').value;
  if (!password) { alert('Password required'); return; }
  try {
    const r = await API.post(`/api/smb/users/${encodeURIComponent(username)}/password`, { password });
    if (!r.success) { alert(r.stderr || 'Failed'); return; }
    closeModal();
    alert('Password updated.');
  } catch(e) { alert(e.message); }
}

async function smbDeleteUser(username) {
  if (!confirm(`Delete SMB user "${username}"? (Unix account is left in place.)`)) return;
  try {
    await API.delete(`/api/smb/users/${encodeURIComponent(username)}`);
    page_smb();
  } catch(e) { alert(e.message); }
}

// ── Groups ──
function smbAddGroup() {
  openModal('Create Group', `
    <div class="form-group"><label>Group name</label><input id="sg-name" class="form-control" placeholder="staff"></div>
    <button class="btn" onclick="smbDoAddGroup()">Create Group</button>
  `);
}

async function smbDoAddGroup() {
  const name = $('sg-name').value.trim();
  if (!name) { alert('Name required'); return; }
  try {
    const r = await API.post('/api/smb/groups', { name });
    if (!r.success) { alert(r.stderr || 'Failed'); return; }
    closeModal();
    page_smb();
  } catch(e) { alert(e.message); }
}

async function smbDeleteGroup(name) {
  if (!confirm(`Delete group "${name}"?`)) return;
  try {
    await API.delete(`/api/smb/groups/${encodeURIComponent(name)}`);
    page_smb();
  } catch(e) { alert(e.message); }
}

function smbGroupMembers(name) {
  const g = smbState.groups.find(x => x.name === name) || { members: [] };
  const memberList = g.members.length
    ? g.members.map(m => `<li>${escapeHtml(m)} <button class="btn btn-sm btn-danger" onclick="smbGroupMember('${jsArg(name)}','${jsArg(m)}','remove')">remove</button></li>`).join('')
    : '<li>(no members)</li>';
  const userOpts = smbState.users.map(u => `<option value="${escapeHtml(u.username)}">${escapeHtml(u.username)}</option>`).join('');
  openModal('Members of ' + name, `
    <ul style="list-style:none;padding:0">${memberList}</ul>
    <div class="form-group" style="display:flex;gap:8px;align-items:center">
      <select id="sg-member" class="form-control">${userOpts || '<option value="">No SMB users</option>'}</select>
      <button class="btn btn-sm" onclick="smbGroupMember('${jsArg(name)}',$('sg-member').value,'add')">Add</button>
    </div>
  `);
}

async function smbGroupMember(group, username, action) {
  if (!username) return;
  try {
    await API.post(`/api/smb/groups/${encodeURIComponent(group)}/members`, { username, action });
    await page_smb();
    smbGroupMembers(group);
  } catch(e) { alert(e.message); }
}

async function smbToggleHomes(enabled) {
  if (!confirm(`${enabled ? 'Disable' : 'Enable'} home directory shares?`)) return;
  try {
    const r = await API.post('/api/smb/homes', { enabled: !enabled });
    if (!r.success) { alert(r.stderr || 'Failed'); return; }
    page_smb();
  } catch(e) { alert(e.message); }
}

// ─── DLNA Media (MiniDLNA) ──────────────────────────────
let _dlnaMedia = [];
const DLNA_TYPES = [['', 'All'], ['A', 'Audio'], ['V', 'Video'], ['P', 'Pictures']];

function dlnaMediaRows() {
  if (!_dlnaMedia.length) return '<tr><td colspan="3" class="help">No media directories yet — add one below.</td></tr>';
  return _dlnaMedia.map((m, i) => `
    <tr>
      <td><select class="form-control" onchange="_dlnaMedia[${i}].type=this.value" style="max-width:130px">
        ${DLNA_TYPES.map(([v, l]) => `<option value="${v}" ${m.type === v ? 'selected' : ''}>${l}</option>`).join('')}
      </select></td>
      <td><input class="form-control" value="${escapeHtml(m.path || '')}" placeholder="/srv/media"
                 oninput="_dlnaMedia[${i}].path=this.value"></td>
      <td><button class="btn btn-sm btn-danger" onclick="dlnaDelMedia(${i})">Remove</button></td>
    </tr>`).join('');
}
function dlnaAddMedia() { _dlnaMedia.push({ type: '', path: '' }); $('dlna-media-body').innerHTML = dlnaMediaRows(); }
function dlnaDelMedia(i) { _dlnaMedia.splice(i, 1); $('dlna-media-body').innerHTML = dlnaMediaRows(); }

// "Media library" summary from minidlna's files.db (counts + db size).
function dlnaLibraryCard(lib) {
  if (!lib || !lib.available) {
    const why = lib && lib.path
      ? `No media database yet at <code>${escapeHtml(lib.path)}</code> — it appears after the first scan.`
      : 'Media database statistics are unavailable.';
    return `<div class="card"><h3>Media library</h3><p class="help">${why}</p></div>`;
  }
  const tile = (label, val) => `
    <div class="res-item" style="flex:1;text-align:center">
      <div class="card-value">${Number(val || 0).toLocaleString()}</div>
      <div class="res-label">${label}</div>
    </div>`;
  return `
    <div class="card">
      <h3>Media library</h3>
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">
        ${tile('Audio', lib.audio)}${tile('Video', lib.video)}${tile('Image', lib.image)}
      </div>
      <div class="res-label">Database</div>
      <p class="help" style="margin-top:2px"><code>${escapeHtml(lib.path)}</code>
        (${fmtBytesIEC(lib.size)}, ${Number(lib.objects || 0).toLocaleString()} objects)</p>
    </div>`;
}

async function page_minidlna() {
  const d = await API.get('/api/minidlna');
  _dlnaMedia = (d.media_dirs || []).map(m => ({ type: m.type || '', path: m.path || '' }));
  const svc = d.service || {};
  const active = svc.active === 'active';
  const warn = d.configured ? '' : `
    <div class="alert alert-warning">
      <strong>MiniDLNA isn't installed on this host yet.</strong>
      Expected the <code>minidlna</code> package and <code>${escapeHtml(d.conf_path)}</code>.
      You can still edit settings below; they apply once the service exists.
    </div>`;
  $('page-content').innerHTML = `
    <h2>DLNA Media <span class="help" style="font-weight:400">(MiniDLNA / ReadyMedia)</span></h2>
    ${warn}
    ${dlnaLibraryCard(d.library)}
    <div class="card">
      <h3>Service</h3>
      <p>Status: <span class="status-badge ${active ? 'green' : 'red'}">${escapeHtml(svc.active || 'unknown')}</span>
         &nbsp;·&nbsp; Boot: <span class="status-badge ${svc.enabled === 'enabled' ? 'green' : 'gray'}">${escapeHtml(svc.enabled || 'disabled')}</span></p>
      <div class="toolbar">
        <button class="btn btn-sm" onclick="dlnaSvc('start')">Start</button>
        <button class="btn btn-sm btn-warning" onclick="dlnaSvc('stop')">Stop</button>
        <button class="btn btn-sm" onclick="dlnaSvc('restart')">Restart</button>
        <button class="btn btn-sm btn-outline" onclick="dlnaSvc('enable')">Enable</button>
        <button class="btn btn-sm btn-outline" onclick="dlnaSvc('disable')">Disable</button>
      </div>
    </div>
    <div class="card">
      <h3>Settings</h3>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div class="form-group" style="flex:2"><label>Friendly name</label>
          <input id="dlna-name" class="form-control" value="${escapeHtml(d.friendly_name || '')}" placeholder="Nexus Media"></div>
        <div class="form-group" style="flex:1"><label>Port</label>
          <input id="dlna-port" class="form-control" value="${escapeHtml(d.port || '8200')}" placeholder="8200"></div>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div class="form-group" style="flex:1"><label>Network interface(s) <span class="help">(blank = all)</span></label>
          <input id="dlna-iface" class="form-control" value="${escapeHtml(d.network_interface || '')}" placeholder="eth0, eth1"></div>
        <div class="form-group" style="flex:1"><label>Root container <span class="help">(blank = default)</span></label>
          <input id="dlna-container" class="form-control" value="${escapeHtml(d.root_container || '')}" placeholder="B"></div>
      </div>
      <div class="form-group">
        <label><input type="checkbox" id="dlna-inotify" ${d.inotify !== 'no' ? 'checked' : ''}> Watch directories for changes (inotify)</label>
      </div>
      <h4 style="margin:10px 0 6px">Media directories</h4>
      <table class="table"><thead><tr><th style="width:150px">Type</th><th>Path</th><th></th></tr></thead>
        <tbody id="dlna-media-body">${dlnaMediaRows()}</tbody></table>
      <div class="toolbar">
        <button class="btn btn-sm btn-outline" onclick="dlnaAddMedia()">Add Directory</button>
        <button class="btn" onclick="dlnaSave()">Save Settings</button>
      </div>
      <p class="help">Each directory may be tagged <strong>Audio</strong>, <strong>Video</strong>, or <strong>Pictures</strong> to
        restrict the content type minidlna indexes there (blank = all types). Saving rewrites
        <code>${escapeHtml(d.conf_path)}</code> and reloads the service.</p>
    </div>
    <div class="card">
      <h3>Database</h3>
      <div class="toolbar">
        <button class="btn" onclick="dlnaRescan()">Rescan</button>
        <button class="btn btn-danger" onclick="dlnaRebuildConfirm()">Force Rebuild</button>
      </div>
      <p class="help"><strong>Rescan</strong> restarts the service to re-read the config and pick up new files.
        <strong>Force Rebuild</strong> discards the media index (<code>${escapeHtml(d.cache_dir)}/files.db</code>) and
        re-scans every directory from scratch — use it when the library looks wrong. It briefly interrupts serving.</p>
    </div>`;
}

async function dlnaSvc(action) {
  try { await API.post(`/api/service/minidlna/${action}`, {}); page_minidlna(); }
  catch (e) { alert(e.message); }
}

async function dlnaSave() {
  const body = {
    friendly_name: $('dlna-name').value,
    port: $('dlna-port').value,
    network_interface: $('dlna-iface').value,
    root_container: $('dlna-container').value,
    inotify: $('dlna-inotify').checked,
    media_dirs: _dlnaMedia.filter(m => (m.path || '').trim()),
  };
  try {
    const r = await API.post('/api/minidlna', body);
    if (!r.success) { alert(r.error || 'Failed to save'); return; }
    page_minidlna();
  } catch (e) { alert(e.message); }
}

async function dlnaRescan() {
  try {
    const r = await API.post('/api/minidlna/rescan', {});
    if (!r.success) { alert(r.error || 'Rescan failed'); return; }
    alert('Rescan started — the service is re-reading its library.');
    page_minidlna();
  } catch (e) { alert(e.message); }
}

function dlnaRebuildConfirm() {
  confirmName({
    title: 'Force database rebuild',
    warning: 'This deletes the media index and re-scans every directory from scratch. DLNA serving is interrupted while it rebuilds.',
    name: 'rebuild', label: 'Type',
    button: 'Rebuild database',
    onConfirm: dlnaRebuild,
  });
}
async function dlnaRebuild() {
  try {
    const r = await API.post('/api/minidlna/rebuild', {});
    if (!r.success) { alert(r.error || 'Rebuild failed'); return; }
    closeModal();
    alert('Database rebuild started.');
    page_minidlna();
  } catch (e) { alert(e.message); }
}

// ─── Services ───────────────────────────────────────────
// ─── Scheduled Tasks ────────────────────────────────────
