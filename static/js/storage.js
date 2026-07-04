async function page_disks() {
  const [data, fsData] = await Promise.all([
    API.get('/api/disks'),
    API.get('/api/filesystems').catch(() => ({ filesystems: [], bases: ['/mnt', '/media'] })),
  ]);
  const devs = data.devices || [];
  const fsList = fsData.filesystems || [];
  _fsBases = fsData.bases || ['/mnt', '/media'];
  $('page-content').innerHTML = `
    <h2>Available Disks</h2>
    <table class="table">
      <thead><tr><th>Name</th><th>Size</th><th>Type</th><th>Usage</th><th>Model</th><th>Actions</th></tr></thead>
      <tbody>
        ${devs.map(d => {
          const usage = d.usage || '';
          const ucls = usage === 'Free' ? 'green' : /stale/i.test(usage) ? 'yellow' : 'gray';
          const usageCell = d.type === 'disk' ? `<span class="status-badge ${ucls}">${escapeHtml(usage)}</span>` : '-';
          const byId = d.by_id ? `<div class="help" style="font-size:11px" title="Stable identifier used for ZFS pool membership">${escapeHtml(d.by_id.replace('/dev/disk/by-id/',''))}</div>` : '';
          return `<tr>
          <td>${escapeHtml(d.name||'')}${byId}</td>
          <td>${escapeHtml(d.size||'')}</td>
          <td>${escapeHtml(d.type||'')}</td>
          <td>${usageCell}</td>
          <td>${escapeHtml(d.model||'')}</td>
          <td>${d.type === 'disk' ? `
            <button class="btn btn-sm" onclick="diskSmart('${jsArg(d.name)}')">SMART</button>
            <button class="btn btn-sm" onclick="diskLocate('${jsArg(d.name)}')">Locate</button>
            ${d.wipeable
              ? `<button class="btn btn-sm" onclick="diskFormat('${jsArg(d.name)}')">Format</button>
                 <button class="btn btn-sm btn-danger" onclick="diskWipe('${jsArg(d.name)}', ${JSON.stringify(d.md_stop || []).replace(/"/g, '&quot;')})">Wipe</button>`
              : `<span class="help" title="${escapeHtml(d.wipe_reason || '')}" style="font-size:11px">protected</span>`}
          ` : '-'}</td>
        </tr>`;
        }).join('')}
      </tbody>
    </table>

    <h2 style="margin-top:28px">Filesystems &amp; Mounts</h2>
    <p class="help">Standard formatted filesystems — including a plugged-in USB drive — that you can mount and (optionally) remount on boot. ZFS / LVM / RAID / swap members are managed on their own pages and are not shown here.</p>
    <table class="table">
      <thead><tr><th>Device</th><th>Size</th><th>Filesystem</th><th>Label</th><th>Mounted at</th><th>Actions</th></tr></thead>
      <tbody>
        ${fsList.length ? fsList.map(f => {
          const tran = f.tran === 'usb' ? ` <span class="status-badge yellow" title="Removable / USB">USB</span>` : '';
          const boot = f.fstab ? ` <span class="status-badge gray" title="Has an /etc/fstab entry (mounts on boot)">boot</span>` : '';
          let actions = '-';
          if (!f.mounted) {
            actions = `<button class="btn btn-sm" onclick="fsMount('${jsArg(f.name)}', ${JSON.stringify(f.label||'').replace(/"/g,'&quot;')})">Mount</button>`;
          } else if (f.unmountable) {
            actions = `<button class="btn btn-sm btn-outline" onclick="fsUnmount('${jsArg(f.name)}', ${f.fstab ? 'true' : 'false'})">Unmount</button>`;
          } else if (f.system) {
            actions = `<span class="help" style="font-size:11px" title="System mount">system</span>`;
          }
          return `<tr>
            <td>${escapeHtml(f.name||'')}${tran}${escapeHtml(f.model ? ' · '+f.model : '')}</td>
            <td>${escapeHtml(f.size||'')}</td>
            <td>${escapeHtml(f.fstype||'')}</td>
            <td>${escapeHtml(f.label||'')}</td>
            <td>${f.mounted ? escapeHtml(f.mountpoint||'') + boot : '<span class="help" style="font-size:11px">not mounted</span>'+boot}</td>
            <td>${actions}</td>
          </tr>`;
        }).join('') : '<tr><td colspan="6"><span class="help">No standard filesystems found.</span></td></tr>'}
      </tbody>
    </table>
    <pre class="raw-output">${escapeHtml(data.scsi_info||'')}</pre>
  `;
}

async function diskSmart(dev) {
  openModal('SMART: ' + dev, '<div class="loading">Loading…</div>');
  try {
    const s = await API.get(`/api/disks/${encodeURIComponent(dev)}/smart`);
    const hb = s.health === 'PASSED' ? 'green' : s.health === 'FAILED' ? 'red' : 'gray';
    const row = (k, v) => (v === undefined || v === null || v === '') ? '' :
      `<tr><td>${k}</td><td>${escapeHtml(String(v))}</td></tr>`;
    let rows = '';
    rows += row('Model', s.model);
    rows += row('Serial', s.serial);
    rows += row('Firmware', s.firmware);
    rows += row('Rotation', s.rotation_rate ? s.rotation_rate + ' rpm' : (s.model ? 'SSD' : ''));
    rows += row('Temperature', s.temperature_c != null ? s.temperature_c + ' °C' : '');
    rows += row('Power-on hours', s.power_on_hours);
    rows += row('Reallocated sectors', s.reallocated);
    rows += row('Pending sectors', s.pending);
    rows += row('Offline uncorrectable', s.uncorrectable);
    rows += row('Media errors (NVMe)', s.media_errors);
    rows += row('Wear used (NVMe)', s.percentage_used != null ? s.percentage_used + ' %' : '');
    rows += row('Critical warning (NVMe)', s.critical_warning);
    const msgs = (s.messages || []).length ? `<p class="help">${escapeHtml(s.messages.join('; '))}</p>` : '';
    openModal('SMART: ' + dev, `
      <p>Overall health: <span class="status-badge ${hb}">${escapeHtml(s.health || 'unknown')}</span></p>
      ${msgs}
      <table class="table"><tbody>${rows || '<tr><td>No SMART data available</td></tr>'}</tbody></table>
    `);
  } catch(e) {
    openModal('SMART: ' + dev, `<div class="error">${escapeHtml(e.message)}</div>`);
  }
}

function diskLocate(dev) {
  openModal('Locate ' + dev, `
    <p>Flash <code>${escapeHtml(dev)}</code>'s activity light (and the enclosure locate
       LED if the hardware supports it) so you can find the drive physically.
       This is read-only and safe to run on any disk.</p>
    <div class="form-group"><label>Duration (seconds)</label><input id="loc-secs" class="form-control" value="20"></div>
    <button class="btn" onclick="diskDoLocate('${jsArg(dev)}')">Start</button>
    <button class="btn btn-outline" onclick="diskStopLocate('${jsArg(dev)}')">Stop</button>
  `);
}

async function diskDoLocate(dev) {
  const seconds = parseInt($('loc-secs').value) || 20;
  try {
    const r = await API.post(`/api/disks/${encodeURIComponent(dev)}/locate`, { seconds });
    alert(r.message || 'Locating…');
    closeModal();
  } catch(e) { alert(e.message); }
}

async function diskStopLocate(dev) {
  try {
    await API.post(`/api/disks/${encodeURIComponent(dev)}/locate`, { stop: true });
    alert('Locate stopped.');
    closeModal();
  } catch(e) { alert(e.message); }
}

function diskWipe(dev, mdStop) {
  const mdWarn = (mdStop && mdStop.length)
    ? `<div class="alert alert-warning">This disk is held by a stale RAID array (<code>${escapeHtml(mdStop.join(', '))}</code>). The wipe will stop it first.</div>`
    : '';
  openModal('Wipe disk ' + dev, `
    <div class="alert alert-warning"><strong>Destructive &amp; irreversible.</strong>
      This erases the partition table and all filesystem / RAID signatures on
      <code>/dev/${escapeHtml(dev)}</code>, leaving it blank like a new disk.</div>
    ${mdWarn}
    <div class="form-group">
      <label>Type the disk name <code>${escapeHtml(dev)}</code> to confirm</label>
      <input id="wipe-confirm" class="form-control" autocomplete="off" spellcheck="false">
    </div>
    <button class="btn btn-danger" onclick="diskDoWipe('${jsArg(dev)}')">Wipe Disk</button>
  `);
}

async function diskDoWipe(dev) {
  if ($('wipe-confirm').value.trim() !== dev) { alert('Type the disk name exactly to confirm.'); return; }
  try {
    const r = await API.post(`/api/disks/${encodeURIComponent(dev)}/wipe`, {});
    closeModal();
    if (!r.success) {
      const failed = (r.steps || []).filter(s => s.success === false).map(s => `${s.step}: ${s.stderr || ''}`);
      alert('Wipe completed with errors:\n' + (failed.join('\n') || JSON.stringify(r)));
    } else {
      alert(`Disk ${dev} wiped — it is now blank.`);
    }
    page_disks();
  } catch(e) { alert(e.message); }
}

function diskFormat(dev) {
  openModal('Format disk ' + dev, `
    <div class="alert alert-warning"><strong>Destructive &amp; irreversible.</strong>
      This writes a new partition table to <code>/dev/${escapeHtml(dev)}</code> and
      creates a single full-disk filesystem — everything currently on the disk is erased.</div>
    <div class="form-group">
      <label>Filesystem type</label>
      <select id="fmt-fstype" class="form-control">
        <option value="ext4">ext4 (Linux)</option>
        <option value="xfs">xfs (Linux)</option>
        <option value="vfat">vfat / FAT32 (cross-platform, USB)</option>
        <option value="exfat">exFAT (cross-platform, large files)</option>
      </select>
    </div>
    <div class="form-group">
      <label>Label (optional)</label>
      <input id="fmt-label" class="form-control" autocomplete="off" spellcheck="false" placeholder="e.g. backup">
    </div>
    <div class="form-group">
      <label>Type the disk name <code>${escapeHtml(dev)}</code> to confirm</label>
      <input id="fmt-confirm" class="form-control" autocomplete="off" spellcheck="false">
    </div>
    <button class="btn btn-danger" onclick="diskDoFormat('${jsArg(dev)}')">Format Disk</button>
  `);
}

async function diskDoFormat(dev) {
  if ($('fmt-confirm').value.trim() !== dev) { alert('Type the disk name exactly to confirm.'); return; }
  const fstype = $('fmt-fstype').value;
  const label = $('fmt-label').value.trim();
  try {
    const r = await API.post(`/api/disks/${encodeURIComponent(dev)}/format`, { fstype, label });
    closeModal();
    if (!r.success) {
      const failed = (r.steps || []).filter(s => s.success === false).map(s => `${s.step}: ${s.stderr || ''}`);
      alert('Format completed with errors:\n' + (failed.join('\n') || JSON.stringify(r)));
    } else {
      alert(`Disk ${dev} formatted as ${fstype} (partition ${r.partition}). You can now mount it below.`);
    }
    page_disks();
  } catch(e) { alert(e.message); }
}

function fsMount(part, label) {
  const suggested = (label || part).replace(/[^A-Za-z0-9_.-]/g, '').replace(/^[^A-Za-z0-9]+/, '') || part;
  const baseOpts = _fsBases.map(b => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join('');
  openModal('Mount ' + part, `
    <p>Mount <code>/dev/${escapeHtml(part)}</code> so its files are accessible.</p>
    <div class="form-group">
      <label>Mount under</label>
      <select id="mnt-base" class="form-control">${baseOpts}</select>
    </div>
    <div class="form-group">
      <label>Folder name</label>
      <input id="mnt-name" class="form-control" autocomplete="off" spellcheck="false" value="${escapeHtml(suggested)}">
      <span class="help" style="font-size:11px">The filesystem will appear at <code>&lt;base&gt;/&lt;name&gt;</code>.</span>
    </div>
    <div class="form-group">
      <label><input type="checkbox" id="mnt-fstab"> Mount on boot (add to /etc/fstab, by UUID, with <code>nofail</code>)</label>
    </div>
    <button class="btn" onclick="fsDoMount('${jsArg(part)}')">Mount</button>
  `);
}

async function fsDoMount(part) {
  const name = $('mnt-name').value.trim();
  const base = $('mnt-base').value;
  const fstab = $('mnt-fstab').checked;
  if (!name) { alert('Enter a folder name.'); return; }
  try {
    const r = await API.post(`/api/filesystems/${encodeURIComponent(part)}/mount`, { name, base, fstab });
    closeModal();
    if (fstab && r.fstab === false) {
      alert(`Mounted at ${r.mountpoint}, but the boot entry could not be added: ` +
            ((r.fstab_detail && (r.fstab_detail.stderr || '').trim()) || 'unknown error'));
    } else {
      alert(`Mounted at ${r.mountpoint}${fstab ? ' (and added to fstab)' : ''}.`);
    }
    page_disks();
  } catch(e) { alert(e.message); }
}

function fsUnmount(part, hasFstab) {
  openModal('Unmount ' + part, `
    <p>Unmount <code>/dev/${escapeHtml(part)}</code>. Make sure nothing is using it first.</p>
    ${hasFstab ? `<div class="form-group"><label><input type="checkbox" id="umnt-fstab" checked> Also remove its /etc/fstab boot entry</label></div>` : ''}
    <button class="btn btn-outline" onclick="fsDoUnmount('${jsArg(part)}')">Unmount</button>
  `);
}

async function fsDoUnmount(part) {
  const remove_fstab = $('umnt-fstab') ? $('umnt-fstab').checked : false;
  try {
    await API.post(`/api/filesystems/${encodeURIComponent(part)}/unmount`, { remove_fstab });
    closeModal();
    page_disks();
  } catch(e) { alert(e.message); }
}

// ─── ZFS ────────────────────────────────────────────────
async function zfsRefresh() {
  const [pools, detail, arc] = await Promise.all([
    API.get('/api/zfs/pools'),
    API.get('/api/zfs/pools/detail'),
    API.get('/api/zfs/arc').catch(() => ({ available: false })),
  ]);
  const pD = detail || {};
  let html = '<h2>ZFS Pools</h2>';
  html += '<div class="toolbar"><button class="btn" onclick="zfsCreatePool()">+ New Pool</button>'
        + ' <button class="btn btn-outline" onclick="zfsImportModal()">Import Pool</button></div>';
  html += zfsArcCard(arc);

  for (const p of pools) {
    const pd = pD[p.name] || {};
    const configRows = (pd.config || []).map(l => `<div class="zfs-vdev">${escapeHtml(l)}</div>`).join('');
    const cap = parseInt(p.cap) || 0;
    const state = pd.state || p.health;
    const scanning = /(scrub|resilver) in progress/i.test(pd.scan || '');
    const errors = (pd.errors && pd.errors !== 'No known data errors') ? pd.errors : '';
    html += `<div class="pool-card">
      <div class="pool-header">
        <strong class="pool-name">${escapeHtml(p.name)}</strong>
        <span class="status-badge ${state === 'ONLINE' ? 'green' : 'red'}">${escapeHtml(state)}</span>
        ${pd.unstable ? `<span class="status-badge yellow" title="Members are referenced by kernel device names (e.g. /dev/nvme0n1), which can be reordered on reboot and make the pool appear DEGRADED. Click Stabilize to re-import by /dev/disk/by-id.">⚠ kernel names</span>` : ''}
        <span class="pool-stats">${escapeHtml(p.alloc)} / ${escapeHtml(p.size)}</span>
        <span class="pool-stats" id="fc-${escapeHtml(p.name)}"></span>
        <button class="btn btn-sm" onclick="zfsPoolDetail('${jsArg(p.name)}')">Manage</button>
        ${scanning
          ? `<button class="btn btn-sm btn-warning" onclick="zfsScrub('${jsArg(p.name)}','stop')">Stop Scrub</button>`
          : `<button class="btn btn-sm btn-outline" onclick="zfsScrub('${jsArg(p.name)}','start')">Scrub</button>`}
        <button class="btn btn-sm btn-outline" onclick="zfsTrim('${jsArg(p.name)}')">Trim</button>
        ${pd.unstable ? `<button class="btn btn-sm btn-warning" onclick="zfsStabilizePool('${jsArg(p.name)}')">Stabilize</button>` : ''}
        <button class="btn btn-sm btn-outline" onclick="zfsExportPool('${jsArg(p.name)}')">Export</button>
        <button class="btn btn-sm btn-danger" onclick="zfsDestroyPool('${jsArg(p.name)}')">Destroy</button>
      </div>
      ${usageBar(cap)}
      ${pd.scan ? `<div class="pool-scan"><strong>Scan:</strong> ${escapeHtml(pd.scan)}</div>` : ''}
      ${errors ? `<div class="pool-errors"><strong>Errors:</strong> ${escapeHtml(errors)}</div>` : ''}
      ${configRows}
    </div>`;
  }
  $('page-content').innerHTML = html || '<h2>ZFS Pools</h2><p>No pools created yet.</p>' + '<div class="toolbar"><button class="btn" onclick="zfsCreatePool()">+ New Pool</button></div>';
  fillPoolForecasts(pools);
}

async function page_zfs() { await zfsRefresh(); }

// ARC is a RAM cache present on any ZFS host, with or without a cache device.
function zfsArcCard(arc) {
  if (!arc || !arc.available) return '';
  const pct = arc.c_max > 0 ? (arc.size / arc.c_max) * 100 : 0;
  const bits = [];
  if (arc.hit_ratio != null) bits.push(`hit ratio ${arc.hit_ratio}%`);
  if (arc.l2_present) bits.push(`L2ARC ${fmtBytes(arc.l2_size)}`);
  return `<div class="card" style="margin-bottom:16px">
    <h3>ARC (RAM cache)</h3>
    <p>${fmtBytes(arc.size)} <span class="help">of ${fmtBytes(arc.c_max)} max</span></p>
    ${usageBar(pct)}
    ${bits.length ? `<p class="help">${bits.join(' · ')}</p>` : ''}
  </div>`;
}

async function zfsScrub(pool, action) {
  if (action === 'stop' && !confirm(`Stop the scrub on "${pool}"?`)) return;
  try {
    const r = await API.post(`/api/zfs/pools/${encodeURIComponent(pool)}/scrub`, { action });
    if (!r.success) alert(r.stderr || 'Scrub command failed');
    zfsRefresh();
  } catch(e) { alert(e.message); }
}

async function zfsTrim(pool) {
  if (!confirm(`Start TRIM on "${pool}"? This reclaims unused blocks on SSD/thin-provisioned vdevs.`)) return;
  try {
    const r = await API.post(`/api/zfs/pools/${encodeURIComponent(pool)}/trim`, { action: 'start' });
    if (!r.success) alert(r.stderr || 'Trim failed — the pool\'s vdevs may not support TRIM.');
    else zfsRefresh();
  } catch(e) { alert(e.message); }
}

// Remove a device — for cache (L2ARC) / log (SLOG) / spare (and evacuable data
// vdevs on supported layouts). Distinct from Detach (which splits a mirror).
async function zfsRemove(pool, dev) {
  if (!confirm(`Remove device "${dev}" from "${pool}"?\n\nUse this for cache (L2ARC), log (SLOG), or spare devices. Removing a data vdev is only possible on some pool layouts and evacuates its data first.`)) return;
  try {
    const r = await API.post(`/api/zfs/pools/${encodeURIComponent(pool)}/device`, { action: 'remove', device: dev });
    if (!r.success) alert(r.stderr || 'Remove failed — the device may not be removable from this pool layout.');
    zfsPoolDetail(pool);
  } catch(e) { alert(e.message); }
}

async function zfsKeyLoad(name) {
  const passphrase = prompt(`Enter passphrase to unlock "${name}":`);
  if (!passphrase) return;
  try {
    const r = await API.post(`/api/zfs/datasets/${encodeURIComponent(name)}/key/load`, { passphrase });
    if (!r.success) alert(r.stderr || 'Failed to load key (wrong passphrase?)');
    zfsPoolDetail(name.split('/')[0]);
  } catch(e) { alert(e.message); }
}

async function zfsKeyUnload(name) {
  if (!confirm(`Lock "${name}"? Its data becomes inaccessible until unlocked again.`)) return;
  try {
    const r = await API.post(`/api/zfs/datasets/${encodeURIComponent(name)}/key/unload`, {});
    if (!r.success) alert(r.stderr || 'Failed to lock (dataset may be mounted / in use)');
    zfsPoolDetail(name.split('/')[0]);
  } catch(e) { alert(e.message); }
}

async function zfsCreatePool() {
  const disks = await API.get('/api/disks');
  const free = (disks.devices||[]).filter(d => d.type === 'disk' && d.usage === 'Free').map(d =>
    ({ value: `/dev/${d.name}`, label: `/dev/${d.name} (${d.size})` }));
  openModal('Create ZFS Pool', `
    <div class="form-group"><label>Pool Name</label><input id="zp-name" class="form-control" placeholder="mypool"></div>
    <div class="form-group"><label>Data RAID Type</label>
      <select id="zp-type" class="form-control">
        <option value="">Striped (RAID0)</option>
        <option value="mirror">Mirror (RAID1)</option>
        <option value="raidz">RAIDZ1</option>
        <option value="raidz2">RAIDZ2</option>
        <option value="raidz3">RAIDZ3</option>
      </select>
    </div>
    <div class="form-group"><label>Data Disks</label>
      ${checkboxList('zp-disks', free, 'No free disks — wipe a disk on the Disks page first')}
    </div>
    <details style="margin:8px 0">
      <summary class="help" style="cursor:pointer">Cache / Log / Spare devices (optional)</summary>
      <div class="form-group" style="margin-top:8px"><label>Cache (L2ARC)</label>${checkboxList('zp-cache', free, 'No free disks')}</div>
      <div class="form-group"><label>Log (SLOG)</label>${checkboxList('zp-log', free, 'No free disks')}</div>
      <div class="form-group"><label>Hot spares</label>${checkboxList('zp-spare', free, 'No free disks')}</div>
      <p class="help">A disk may be assigned to only one role. Cache and spare are single devices (not mirrored/raidz).</p>
    </details>
    <p class="help">Only free (unused) disks are shown. Check any combination, in any order.</p>
    <button class="btn" onclick="zfsDoCreate()">Create Pool</button>
  `);
}

async function zfsDoCreate() {
  const name = $('zp-name').value.trim();
  const type = $('zp-type').value;
  const data = checkedValues('zp-disks');
  const cache = checkedValues('zp-cache');
  const log = checkedValues('zp-log');
  const spare = checkedValues('zp-spare');
  if (!name || data.length === 0) { alert('Name and at least one data disk required'); return; }
  // A disk may be used in only one role.
  const all = [...data, ...cache, ...log, ...spare];
  if (new Set(all).size !== all.length) { alert('A disk is selected for more than one role.'); return; }
  const vdevs = [{ role: '', type, disks: data }];
  if (cache.length) vdevs.push({ role: 'cache', type: '', disks: cache });
  if (log.length)   vdevs.push({ role: 'log',   type: '', disks: log });
  if (spare.length) vdevs.push({ role: 'spare', type: '', disks: spare });
  try {
    const r = await API.post('/api/zfs/pools', { name, vdevs });
    closeModal();
    if (r.success) zfsRefresh();
    else alert(r.stderr || 'Failed');
  } catch(e) { alert(e.message); }
}

function zfsDestroyPool(name) {
  confirmName({
    title: 'Destroy pool ' + name,
    name,
    warning: `This destroys pool <code>${escapeHtml(name)}</code> and <strong>all data, datasets and snapshots</strong> on it. There is no undo.`,
    label: 'Type the pool name',
    button: 'Destroy Pool',
    onConfirm: async () => {
      try {
        await API.delete(`/api/zfs/pools/${encodeURIComponent(name)}`);
        closeModal(); zfsRefresh();
      } catch(e) { alert(e.message); }
    },
  });
}

async function zfsExportPool(name) {
  if (!confirm(`Export pool "${name}"? It will be detached from this host (data is preserved and can be re-imported here or on another machine).`)) return;
  try {
    const r = await API.post(`/api/zfs/pools/${encodeURIComponent(name)}/export`, {});
    if (!r.success) { alert(r.stderr || r.error || 'Export failed'); return; }
    zfsRefresh();
  } catch(e) { alert(e.message); }
}

async function zfsStabilizePool(name) {
  if (!confirm(`Stabilize pool "${name}"?\n\nThis re-imports the pool using stable /dev/disk/by-id device paths so it won't appear DEGRADED when kernel device names (e.g. /dev/nvme0n1) get reordered on reboot.\n\nThe pool is briefly exported and re-imported — any active I/O to it will pause for a moment. Proceed?`)) return;
  try {
    const r = await API.post(`/api/zfs/pools/${encodeURIComponent(name)}/stabilize`, {});
    if (!r.success) { alert(r.error || (r.stderr || 'Stabilize failed')); }
    zfsRefresh();
  } catch(e) { alert(e.message); }
}

async function zfsImportModal() {
  openModal('Import Pool', '<div class="loading">Scanning for importable pools…</div>');
  let pools = [];
  try { pools = await API.get('/api/zfs/pools/importable'); } catch(e) {}
  const rows = pools.map(p => `<tr>
      <td><code>${escapeHtml(p.name)}</code></td>
      <td><span class="status-badge ${p.state === 'ONLINE' ? 'green' : 'yellow'}">${escapeHtml(p.state || '?')}</span></td>
      <td><code>${escapeHtml(p.id || '')}</code></td>
      <td><button class="btn btn-sm" onclick="zfsDoImport('${jsArg(p.id || p.name)}')">Import</button></td>
    </tr>`).join('');
  openModal('Import Pool', `
    ${pools.length ? `<table class="table"><thead><tr><th>Pool</th><th>State</th><th>ID</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
                   : '<p>No importable pools found on attached devices.</p>'}
    <p class="help">Lists pools present on disks but not currently imported (e.g. moved from another host or previously exported).</p>`);
}

async function zfsDoImport(ident) {
  try {
    const r = await API.post('/api/zfs/pools/import', { name: ident });
    if (!r.success) { alert(r.stderr || r.error || 'Import failed'); return; }
    closeModal(); zfsRefresh();
  } catch(e) { alert(e.message); }
}

async function zfsPoolDetail(pool) {
  const [datasets, snaps, detail] = await Promise.all([
    API.get(`/api/zfs/pools/${encodeURIComponent(pool)}/datasets`),
    API.get(`/api/zfs/snapshots?pool=${encodeURIComponent(pool)}`),
    API.get('/api/zfs/pools/detail')
  ]);
  const pd = (detail || {})[pool] || {};
  const devRows = (pd.config || []).map(line => {
    const parts = line.split(/\s+/);
    const dev = parts[0] || '';
    const state = parts[1] || '';
    // The pool row and vdev containers (mirror-0, raidz1-0, spares, cache, logs)
    // aren't replaceable leaf devices.
    const container = dev === pool || /^(mirror|raidz|spare|cache|log|replacing)/i.test(dev);
    const acts = container ? '' : `
        <button class="btn btn-sm" onclick="zfsDevice('${jsArg(pool)}','offline','${jsArg(dev)}')">Offline</button>
        <button class="btn btn-sm" onclick="zfsDevice('${jsArg(pool)}','online','${jsArg(dev)}')">Online</button>
        <button class="btn btn-sm" onclick="zfsReplace('${jsArg(pool)}','${jsArg(dev)}')">Replace</button>
        <button class="btn btn-sm btn-danger" onclick="zfsDevice('${jsArg(pool)}','detach','${jsArg(dev)}')">Detach</button>
        <button class="btn btn-sm btn-danger" onclick="zfsRemove('${jsArg(pool)}','${jsArg(dev)}')">Remove</button>`;
    const badge = state ? `<span class="status-badge ${state === 'ONLINE' ? 'green' : 'red'}">${escapeHtml(state)}</span>` : '';
    return `<tr><td${container ? '' : ' style="padding-left:20px"'}><code>${escapeHtml(dev)}</code></td><td>${badge}</td><td>${acts}</td></tr>`;
  }).join('');

  let dsRows = datasets.map(ds => {
    const u = parseSize(ds.used), a = parseSize(ds.available);
    const pct = (u + a) > 0 ? (u / (u + a)) * 100 : 0;
    const enc = ds.encryption && ds.encryption !== 'off';
    const locked = ds.keystatus === 'unavailable';
    const encBadge = enc ? ` <span class="status-badge ${locked ? 'red' : 'green'}" title="encryption: ${escapeHtml(ds.encryption)}">${locked ? '🔒 locked' : '🔓'}</span>` : '';
    const keyBtn = enc ? (locked
      ? `<button class="btn btn-sm" onclick="zfsKeyLoad('${jsArg(ds.name)}')">Unlock</button>`
      : `<button class="btn btn-sm btn-outline" onclick="zfsKeyUnload('${jsArg(ds.name)}')">Lock</button>`) : '';
    return `
    <tr>
      <td>${escapeHtml(ds.name)}${encBadge}</td>
      <td>${escapeHtml(ds.used)}</td>
      <td>${escapeHtml(ds.available)}</td>
      <td style="min-width:120px">${usageBar(pct)}</td>
      <td>${escapeHtml(ds.mountpoint)}</td>
      <td>${escapeHtml(ds.compression)}</td>
      <td>
        ${keyBtn}
        <button class="btn btn-sm" onclick="zfsSnapshot('${jsArg(ds.name)}')">Snap</button>
        <button class="btn btn-sm" onclick="zfsProps('${jsArg(ds.name)}')">Props</button>
        <button class="btn btn-sm" onclick="zfsRenameDataset('${jsArg(ds.name)}')">Rename</button>
        <button class="btn btn-sm btn-danger" onclick="zfsDestroyDataset('${jsArg(ds.name)}')">Del</button>
      </td>
    </tr>`;
  }).join('');

  let snapRows = snaps.map(s => `
    <tr>
      <td>${escapeHtml(s.name)}</td>
      <td>${escapeHtml(s.used)}</td>
      <td>${escapeHtml(s.written || '-')}</td>
      <td>${escapeHtml(s.creation)}</td>
      <td>
        <button class="btn btn-sm" onclick="snapBrowse('${jsArg(s.name)}','')">Browse</button>
        <button class="btn btn-sm" onclick="snapDiff('${jsArg(s.name)}')">Diff</button>
        <button class="btn btn-sm btn-warning" onclick="zfsRollback('${jsArg(s.name)}')">Rollback</button>
        <button class="btn btn-sm" onclick="zfsClone('${jsArg(s.name)}')">Clone</button>
        <button class="btn btn-sm btn-danger" onclick="zfsDestroySnap('${jsArg(s.name)}')">Del</button>
      </td>
    </tr>
  `).join('');

  openModal(`Pool: ${pool}`, `
    <h4>Devices</h4>
    <div class="toolbar" style="margin-bottom:8px">
      <button class="btn btn-sm" onclick="zfsAddVdev('${jsArg(pool)}')">+ Add Device</button>
    </div>
    <table class="table">
      <thead><tr><th>Device</th><th>State</th><th>Actions</th></tr></thead>
      <tbody>${devRows || '<tr><td colspan="3">No device info</td></tr>'}</tbody>
    </table>
    <h4 style="margin-top:20px">Datasets</h4>
    <div class="toolbar" style="margin-bottom:8px">
      <button class="btn btn-sm" onclick="zfsCreateDataset('${jsArg(pool)}')">+ Dataset</button>
    </div>
    <table class="table">
      <thead><tr><th>Name</th><th>Used</th><th>Available</th><th>Usage</th><th>Mount</th><th>Compress</th><th>Actions</th></tr></thead>
      <tbody>${dsRows || '<tr><td colspan="7">No datasets</td></tr>'}</tbody>
    </table>
    <h4 style="margin-top:20px">Snapshots</h4>
    <div class="toolbar" style="margin-bottom:8px">
      <button class="btn btn-sm" onclick="zfsSnapshot('${jsArg(pool)}')">+ Snapshot</button>
    </div>
    <table class="table">
      <thead><tr><th>Name</th><th>Used</th><th>Written</th><th>Created</th><th>Actions</th></tr></thead>
      <tbody>${snapRows || '<tr><td colspan="5">No snapshots</td></tr>'}</tbody>
    </table>
  `, {wide:true});
}

async function zfsCreateDataset(pool) {
  openModal('Create Dataset', `
    <div class="form-group"><label>Type</label>
      <select id="zd-type" class="form-control" onchange="document.getElementById('zd-vol-group').style.display=this.value==='volume'?'block':'none'">
        <option value="filesystem">Filesystem</option>
        <option value="volume">Volume (ZVOL — block device, e.g. for iSCSI)</option>
      </select>
    </div>
    <div class="form-group"><label>Name</label><input id="zd-name" class="form-control" value="${pool}/" placeholder="${pool}/data"></div>
    <div id="zd-vol-group" style="display:none">
      <div class="form-group"><label>Volume Size</label><input id="zd-volsize" class="form-control" placeholder="e.g. 10G"></div>
    </div>
    <div class="form-group"><label>Compression</label>
      <select id="zd-compress" class="form-control"><option value="">Default</option><option value="on">On</option><option value="lz4">LZ4</option><option value="zstd">ZSTD</option><option value="gzip">GZIP</option></select>
    </div>
    <div class="form-group"><label>Quota (filesystem only, optional)</label><input id="zd-quota" class="form-control" placeholder="e.g. 10G"></div>
    <div class="form-group"><label>Reservation (filesystem only, optional)</label><input id="zd-reserve" class="form-control" placeholder="e.g. 5G"></div>
    <div class="form-group"><label><input type="checkbox" id="zd-enc" onchange="document.getElementById('zd-enc-group').style.display=this.checked?'block':'none'"> Encrypt this dataset (passphrase)</label></div>
    <div id="zd-enc-group" style="display:none">
      <div class="form-group"><label>Algorithm</label>
        <select id="zd-enc-algo" class="form-control">
          <option value="aes-256-gcm">aes-256-gcm (default)</option>
          <option value="aes-128-gcm">aes-128-gcm</option>
          <option value="aes-256-ccm">aes-256-ccm</option>
        </select>
      </div>
      <div class="form-group"><label>Passphrase (min 8 characters)</label><input id="zd-enc-pass" class="form-control" type="password" autocomplete="new-password"></div>
      <p class="help">Encryption can only be set at creation. Keep the passphrase safe — it's required to unlock the data after a reboot.</p>
    </div>
    <button class="btn" onclick="zfsDoCreateDataset()">Create</button>
  `);
}

async function zfsDoCreateDataset() {
  const name = $('zd-name').value.trim();
  const isVol = $('zd-type')?.value === 'volume';
  const props = {};
  const comp = $('zd-compress')?.value;
  if (comp) props.compression = comp;
  if (!isVol) {
    if ($('zd-quota')?.value) props.quota = $('zd-quota').value;
    if ($('zd-reserve')?.value) props.reservation = $('zd-reserve').value;
  }
  if (!name) { alert('Name required'); return; }
  const body = { name, properties: props };
  if (isVol) {
    const volsize = $('zd-volsize').value.trim();
    if (!volsize) { alert('Volume size required'); return; }
    body.volsize = volsize;
  }
  if ($('zd-enc') && $('zd-enc').checked) {
    const pass = $('zd-enc-pass').value || '';
    if (pass.length < 8) { alert('Encryption passphrase must be at least 8 characters.'); return; }
    body.encryption = $('zd-enc-algo').value;
    body.keyformat = 'passphrase';
    body.passphrase = pass;
  }
  try {
    const r = await API.post('/api/zfs/datasets', body);
    if (!r.success) { alert(r.stderr || 'Failed'); return; }
    closeModal();
    zfsRefresh();
  } catch(e) { alert(e.message); }
}

async function zfsRenameDataset(name) {
  openModal('Rename Dataset', `
    <p>Renaming <code>${escapeHtml(name)}</code></p>
    <div class="form-group"><label>New name</label><input id="zrn-new" class="form-control" value="${escapeHtml(name)}"></div>
    <button class="btn" onclick="zfsDoRenameDataset('${jsArg(name)}')">Rename</button>
  `);
}

async function zfsDoRenameDataset(name) {
  const new_name = $('zrn-new').value.trim();
  if (!new_name || new_name === name) { closeModal(); return; }
  try {
    const r = await API.post('/api/zfs/datasets/rename', { name, new_name });
    if (!r.success) { alert(r.stderr || 'Failed'); return; }
    closeModal();
    zfsRefresh();
  } catch(e) { alert(e.message); }
}

const ZFS_EDITABLE_PROPS = ['compression', 'recordsize', 'atime', 'readonly', 'quota', 'reservation', 'sync'];

async function zfsProps(name) {
  const props = await API.get(`/api/zfs/datasets/${encodeURIComponent(name)}/properties`);
  const rows = ZFS_EDITABLE_PROPS.map(p => `
    <div class="form-group" style="display:flex;gap:8px;align-items:center">
      <label style="width:120px;margin:0">${p}</label>
      <input id="zp-${p}" class="form-control" value="${escapeHtml(props[p] || '')}">
      <button class="btn btn-sm" onclick="zfsDoSetProp('${jsArg(name)}','${p}')">Set</button>
    </div>`).join('');
  openModal(`Properties: ${name}`, `<p class="help">Edit a value and click Set.</p>${rows}`);
}

async function zfsDoSetProp(name, property) {
  const value = $('zp-' + property).value.trim();
  try {
    const r = await API.put(`/api/zfs/datasets/${encodeURIComponent(name)}/properties`, { property, value });
    if (!r.success) alert(r.stderr || 'Failed');
    else alert(`${property} set to ${value}`);
  } catch(e) { alert(e.message); }
}

function zfsDestroyDataset(name) {
  confirmName({
    title: 'Destroy dataset ' + name,
    name,
    warning: `This destroys dataset <code>${escapeHtml(name)}</code> and <strong>all its data and snapshots</strong> (and any child datasets). There is no undo.`,
    label: 'Type the dataset name',
    button: 'Destroy Dataset',
    onConfirm: async () => {
      try {
        await API.delete(`/api/zfs/datasets/${encodeURIComponent(name)}`);
        closeModal(); zfsRefresh();
      } catch(e) { alert(e.message); }
    },
  });
}

async function zfsSnapshot(name) {
  const def = name.includes('@') ? '' : name.split('/').pop() || 'snap';
  openModal('Create Snapshot', `
    <p>Dataset: <strong>${escapeHtml(name)}</strong></p>
    <div class="form-group"><label>Snapshot Name</label><input id="zs-name" class="form-control" value="snap-${Date.now()}"></div>
    <div class="form-group"><label><input type="checkbox" id="zs-recursive"> Recursive (snapshot child datasets too)</label></div>
    <button class="btn" onclick="zfsDoSnapshot('${jsArg(name)}')">Create Snapshot</button>
  `);
}

async function zfsDoSnapshot(dataset) {
  const snap_name = $('zs-name').value.trim();
  const recursive = $('zs-recursive')?.checked || false;
  try {
    const r = await API.post('/api/zfs/snapshots', { dataset, snap_name, recursive });
    if (!r.success) { alert(r.stderr || 'Failed'); return; }
    closeModal();
    zfsRefresh();
  } catch(e) { alert(e.message); }
}

function zfsClone(snap) {
  openModal('Clone Snapshot', `
    <p>Cloning <code>${escapeHtml(snap)}</code> into a new dataset.</p>
    <div class="form-group"><label>New dataset name</label><input id="zcl-target" class="form-control" placeholder="pool/clone"></div>
    <button class="btn" onclick="zfsDoClone('${jsArg(snap)}')">Clone</button>
  `);
}

async function zfsDoClone(snap) {
  const target = $('zcl-target').value.trim();
  if (!target) { alert('Target name required'); return; }
  try {
    const r = await API.post('/api/zfs/snapshots/clone', { snapshot: snap, target });
    if (!r.success) { alert(r.stderr || 'Failed'); return; }
    closeModal();
    zfsRefresh();
  } catch(e) { alert(e.message); }
}

function zfsRollback(snap) {
  const shortName = snap.split('@')[1] || snap;
  confirmName({
    title: 'Rollback to ' + snap,
    name: shortName,
    warning: `This rolls <code>${escapeHtml(snap.split('@')[0])}</code> back to <code>@${escapeHtml(shortName)}</code> and <strong>permanently discards all changes (and newer snapshots) made since</strong>. There is no undo.`,
    label: 'Type the snapshot name',
    button: 'Roll Back',
    onConfirm: async () => {
      try {
        await API.post('/api/zfs/snapshots/rollback', { snapshot: snap });
        closeModal(); zfsRefresh();
        alert('Rollback successful');
      } catch(e) { alert(e.message); }
    },
  });
}

async function zfsDestroySnap(name) {
  if (!confirm(`Delete snapshot "${name}"?`)) return;
  try {
    await API.delete(`/api/zfs/snapshots/${encodeURIComponent(name)}`);
    zfsRefresh();
  } catch(e) { alert(e.message); }
}

// ─── Snapshot browse / diff / restore ───────────────────
async function snapBrowse(snap, path) {
  openModal(`Browse: ${snap}`, '<div class="loading">Loading…</div>');
  let data;
  try { data = await API.get(`/api/zfs/snapshots/${snap}/browse?path=${encodeURIComponent(path)}`); }
  catch(e) { openModal(`Browse: ${snap}`, `<p class="error">${escapeHtml(e.message)}</p>`); return; }
  const up = path
    ? `<div style="margin-bottom:6px"><a href="#" onclick="snapBrowse('${jsArg(snap)}','${jsArg(path.split('/').slice(0,-1).join('/'))}');return false">⬆ up</a></div>`
    : '';
  const rows = (data.entries || []).map(e => {
    const child = path ? path + '/' + e.name : e.name;
    const name = e.type === 'dir'
      ? `<a href="#" onclick="snapBrowse('${jsArg(snap)}','${jsArg(child)}');return false">&#128193; ${escapeHtml(e.name)}/</a>`
      : escapeHtml(e.name);
    return `<tr><td>${name}</td><td>${e.type === 'dir' ? '' : fmtBytes(e.size)}</td>
      <td><button class="btn btn-sm" onclick="snapRestore('${jsArg(snap)}','${jsArg(child)}')">Restore</button></td></tr>`;
  }).join('');
  openModal(`Browse: ${snap}`, `
    <p class="help">/${escapeHtml(path)}</p>
    ${up}
    <table class="table"><thead><tr><th>Name</th><th>Size</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="3">(empty)</td></tr>'}</tbody></table>`);
}

function snapRestore(snap, path) {
  openModal('Restore from snapshot', `
    <p>Restore <code>${escapeHtml(path)}</code><br>from <code>${escapeHtml(snap)}</code>?</p>
    <div class="toolbar">
      <button class="btn" onclick="snapDoRestore('${jsArg(snap)}','${jsArg(path)}','copy')">Restore a copy (safe)</button>
      <button class="btn btn-warning" onclick="snapDoRestore('${jsArg(snap)}','${jsArg(path)}','inplace')">Overwrite in place</button>
    </div>
    <p class="help">"Restore a copy" writes beside the original with a timestamp suffix and never overwrites.
      "Overwrite in place" replaces the current file/dir.</p>`);
}

async function snapDoRestore(snap, path, mode) {
  if (mode === 'inplace' && !confirm('Overwrite the current file in place? This cannot be undone.')) return;
  try {
    const r = await API.post(`/api/zfs/snapshots/${snap}/restore`, { path, mode });
    if (!r.success) { alert(r.error || 'Restore failed'); return; }
    closeModal();
    alert('Restored to:\n' + (r.restored_to || path));
  } catch(e) { alert(e.message); }
}

async function snapDiff(snap, to) {
  const ds = snap.split('@')[0];
  const pool = ds.split('/')[0];
  openModal(`Diff: ${snap}`, '<div class="loading">Computing diff…</div>');
  let snaps = [];
  try { snaps = await API.get(`/api/zfs/snapshots?pool=${encodeURIComponent(pool)}`); } catch(e) {}
  const sibs = snaps.filter(s => s.name.split('@')[0] === ds && s.name !== snap).map(s => s.name);
  const toVal = to || '';
  let data;
  try { data = await API.get(`/api/zfs/snapshots/diff?from=${encodeURIComponent(snap)}&to=${encodeURIComponent(toVal)}`); }
  catch(e) { openModal(`Diff: ${snap}`, `<p class="error">${escapeHtml(e.message)}</p>`); return; }
  if (!data.success) { openModal(`Diff: ${snap}`, `<p class="error">${escapeHtml(data.error || 'diff failed')}</p>`); return; }
  const sym = { added: '+', removed: '−', modified: '∆', renamed: '→' };
  const cls = { added: 'green', removed: 'red', modified: 'yellow', renamed: 'gray' };
  const rows = (data.changes || []).map(c => `<tr>
      <td><span class="status-badge ${cls[c.change] || 'gray'}">${sym[c.change] || escapeHtml(c.change)}</span></td>
      <td><code>${escapeHtml(c.path)}${c.path_to ? ' → ' + escapeHtml(c.path_to) : ''}</code></td>
    </tr>`).join('');
  const opts = ['<option value="">(live filesystem)</option>'].concat(
    sibs.map(s => `<option value="${escapeHtml(s)}" ${s === toVal ? 'selected' : ''}>${escapeHtml(s.split('@')[1])}</option>`)).join('');
  openModal(`Diff: ${snap}`, `
    <div class="form-group"><label>Compare to</label>
      <select class="form-control" onchange="snapDiff('${jsArg(snap)}', this.value)">${opts}</select></div>
    <p class="help">${data.count} change(s)</p>
    <table class="table"><thead><tr><th></th><th>Path</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="2">No differences</td></tr>'}</tbody></table>`);
}

// ─── ZFS replication ────────────────────────────────────
let replJobs = [];

async function page_replication() {
  const data = await API.get('/api/zfs/replication');
  replJobs = data.jobs || [];
  const rows = replJobs.map(j => {
    const st = j.last_status === 'ok' ? 'green' : (j.last_status === 'error' ? 'red' : 'gray');
    return `<tr>
      <td><code>${escapeHtml(j.source)}</code></td>
      <td><code>${escapeHtml(j.user)}@${escapeHtml(j.host)}:${escapeHtml(j.target)}</code></td>
      <td>${j.recursive ? 'yes' : 'no'}</td>
      <td><span class="status-badge ${j.enabled ? 'green' : 'gray'}">${j.enabled ? 'enabled' : 'disabled'}</span></td>
      <td><span class="status-badge ${st}">${escapeHtml(j.last_status || 'never')}</span>
          ${j.last_error ? ` <span title="${escapeHtml(j.last_error)}">&#9888;</span>` : ''}
          <br><span class="help">${escapeHtml(j.last_run || '')}</span></td>
      <td>
        <button class="btn btn-sm" onclick="replRun('${jsArg(j.id)}')">Run now</button>
        <button class="btn btn-sm" onclick="replModal('${jsArg(j.id)}')">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="replDelete('${jsArg(j.id)}')">Delete</button>
      </td></tr>`;
  }).join('');
  const banner = data.timer_active
    ? '<div class="health-ok">✓ Scheduled replication is ON (hourly timer active)</div>'
    : '<div class="alert alert-info">Scheduled replication is OFF — runs only when at least one job is enabled.</div>';
  $('page-content').innerHTML = `
    <h2>ZFS Replication</h2>
    ${banner}
    <div class="toolbar"><button class="btn" onclick="replModal()">+ New Job</button>
      <button class="btn btn-outline" onclick="replShowKey()">Show SSH key</button></div>
    <table class="table">
      <thead><tr><th>Source</th><th>Destination</th><th>Recursive</th><th>Enabled</th><th>Last run</th><th>Actions</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6">No replication jobs yet. Click “+ New Job”.</td></tr>'}</tbody>
    </table>
    <p class="help">Pushes a dataset's snapshots to a remote ZFS host over SSH (a full stream first, then incrementals).
      The remote user needs passwordless <code>sudo zfs</code> and the dashboard's SSH public key in its
      <code>authorized_keys</code> — click “Show SSH key”. Create snapshots (or an Auto-Snapshot schedule) on the source first.</p>`;
}

async function replShowKey() {
  let d;
  try { d = await API.get('/api/zfs/replication'); } catch(e) { alert(e.message); return; }
  openModal('Replication SSH key', `
    <p>Install this public key in the remote user's <code>~/.ssh/authorized_keys</code>:</p>
    <textarea class="form-control" rows="4" readonly onclick="this.select()">${escapeHtml(d.pubkey || '')}</textarea>
    <p class="help">The remote user also needs passwordless <code>sudo zfs</code> (e.g. an /etc/sudoers.d entry).</p>
    <div class="toolbar"><button class="btn btn-outline" onclick="replRegenKey()">Regenerate key</button></div>`);
}

async function replRegenKey() {
  if (!confirm('Regenerate the replication SSH key? You must then re-install the new public key on every remote.')) return;
  try { await API.post('/api/zfs/replication/key/regenerate', {}); replShowKey(); } catch(e) { alert(e.message); }
}

async function replModal(id) {
  const j = replJobs.find(x => x.id === id) || {};
  let dsets = [];
  try { dsets = await API.get('/api/zfs/datasets/all'); } catch(e) {}
  const opts = (dsets || []).map(d => `<option value="${escapeHtml(d)}" ${d === j.source ? 'selected' : ''}>${escapeHtml(d)}</option>`).join('');
  openModal(id ? 'Edit Replication Job' : 'New Replication Job', `
    <input type="hidden" id="rj-id" value="${escapeHtml(j.id || '')}">
    <div class="form-group"><label>Source dataset</label><select id="rj-source" class="form-control">${opts}</select></div>
    <div class="form-group"><label>Remote host</label><input id="rj-host" class="form-control" value="${escapeHtml(j.host || '')}" placeholder="192.168.0.10"></div>
    <div class="form-group" style="display:flex;gap:8px">
      <div style="flex:2"><label>Remote user</label><input id="rj-user" class="form-control" value="${escapeHtml(j.user || '')}" placeholder="backup"></div>
      <div style="flex:1"><label>Port</label><input id="rj-port" class="form-control" value="${escapeHtml(j.port || 22)}"></div>
    </div>
    <div class="form-group"><label>Target dataset (on remote)</label><input id="rj-target" class="form-control" value="${escapeHtml(j.target || '')}" placeholder="tank/backups/mydata"></div>
    <div class="form-group"><label><input type="checkbox" id="rj-recursive" ${j.recursive ? 'checked' : ''}> Recursive (-R, include child datasets &amp; properties)</label></div>
    <div class="form-group"><label><input type="checkbox" id="rj-enabled" ${j.enabled !== false ? 'checked' : ''}> Enabled (include in scheduled runs)</label></div>
    <div class="toolbar">
      <button class="btn" onclick="replSave()">Save</button>
      <button class="btn btn-outline" onclick="replTest()">Test connection</button>
    </div>
    <div id="rj-test" class="help"></div>`);
}

async function replTest() {
  const host = $('rj-host').value.trim(), user = $('rj-user').value.trim(), port = $('rj-port').value.trim() || 22;
  if (!host || !user) { $('rj-test').textContent = 'Enter host and user first.'; return; }
  $('rj-test').textContent = 'Testing…';
  try {
    const r = await API.post('/api/zfs/replication/test', { host, user, port });
    $('rj-test').innerHTML = r.success
      ? `<span style="color:#6c6">✓ Connected — remote ${escapeHtml(r.remote_zfs)}</span>`
      : `<span style="color:#e66">✗ ${escapeHtml(r.error)}</span>`;
  } catch(e) { $('rj-test').innerHTML = `<span style="color:#e66">✗ ${escapeHtml(e.message)}</span>`; }
}

async function replSave() {
  const body = {
    id: $('rj-id').value || undefined,
    source: $('rj-source').value, host: $('rj-host').value.trim(),
    user: $('rj-user').value.trim(), port: parseInt($('rj-port').value) || 22,
    target: $('rj-target').value.trim(), recursive: $('rj-recursive').checked,
    enabled: $('rj-enabled').checked,
  };
  if (!body.source || !body.host || !body.user || !body.target) { alert('Source, host, user, and target are required'); return; }
  try {
    const r = await API.post('/api/zfs/replication', body);
    if (!r.success) { alert(r.error || 'Save failed'); return; }
    closeModal(); page_replication();
  } catch(e) { alert(e.message); }
}

async function replRun(id) {
  if (!confirm('Run this replication job now? A large initial sync may take a while.')) return;
  try {
    const r = await API.post(`/api/zfs/replication/${encodeURIComponent(id)}/run`, {});
    if (r.success) alert(r.nochange ? (r.message || 'Already up to date') : `Replicated @${r.snapshot} (${r.kind})`);
    else alert('Replication failed:\n' + (r.error || ''));
    page_replication();
  } catch(e) { alert(e.message); }
}

async function replDelete(id) {
  if (!confirm('Delete this replication job? (The remote data is left untouched.)')) return;
  try { await API.delete(`/api/zfs/replication/${encodeURIComponent(id)}`); page_replication(); } catch(e) { alert(e.message); }
}

// ─── Maintenance (scheduled scrubs + SMART self-tests) ──
let maintState = { scrubs: [], smart: [] };
let maintPools = [], maintDisks = [], maintTimerActive = false;

async function page_maintenance() {
  const [m, pools, disks] = await Promise.all([
    API.get('/api/maintenance'),
    API.get('/api/zfs/pools').catch(() => []),
    API.get('/api/disks').catch(() => ({ devices: [] })),
  ]);
  maintState = { scrubs: m.scrubs || [], smart: m.smart || [] };
  maintTimerActive = !!m.timer_active;
  maintPools = (pools || []).map(p => p.name);
  maintDisks = ((disks.devices) || []).filter(d => d.type === 'disk').map(d => d.name);
  renderMaintenance();
}

function maintAddScrub() {
  const pool = $('ms-pool').value;
  if (!pool) return;
  if (maintState.scrubs.some(s => s.pool === pool)) { alert('That pool already has a scrub schedule'); return; }
  maintState.scrubs.push({ pool, freq: $('ms-freq').value, last_run: '' });
  renderMaintenance();
}
function maintRemoveScrub(i) { maintState.scrubs.splice(i, 1); renderMaintenance(); }

function maintAddSmart() {
  const device = $('mt-dev').value;
  if (!device) return;
  const type = $('mt-type').value;
  if (maintState.smart.some(s => s.device === device && s.type === type)) { alert('That disk already has that test scheduled'); return; }
  maintState.smart.push({ device, type, freq: $('mt-freq').value, last_run: '' });
  renderMaintenance();
}
function maintRemoveSmart(i) { maintState.smart.splice(i, 1); renderMaintenance(); }

async function maintSave() {
  try {
    const r = await API.post('/api/maintenance', maintState);
    if (!r.success) { alert(r.error || 'Failed'); return; }
    page_maintenance();
  } catch(e) { alert(e.message); }
}

async function maintSmartNow(device, type) {
  if (!confirm(`Start a ${type} SMART self-test on ${device} now?`)) return;
  try {
    const r = await API.post('/api/maintenance/smart-test', { device, type });
    alert(r.success ? `Started ${type} test on ${device}. Re-check the disk's SMART details for results.`
                    : (r.stderr || r.error || 'Failed'));
  } catch(e) { alert(e.message); }
}

// ─── ZFS device management ──────────────────────────────
async function zfsDevice(pool, action, device) {
  if ((action === 'detach' || action === 'offline') &&
      !confirm(`${action[0].toUpperCase() + action.slice(1)} device "${device}" in pool "${pool}"?`)) return;
  try {
    const r = await API.post(`/api/zfs/pools/${encodeURIComponent(pool)}/device`, { action, device });
    if (!r.success) alert(r.stderr || 'Command failed');
    zfsPoolDetail(pool);
  } catch(e) { alert(e.message); }
}

function zfsReplace(pool, device) {
  openModal('Replace Device', `
    <p>Pool: <strong>${escapeHtml(pool)}</strong> — replacing <code>${escapeHtml(device)}</code></p>
    <div class="form-group"><label>New device</label><input id="zr-new" class="form-control" placeholder="/dev/sdX"></div>
    <p class="help">ZFS will resilver data onto the new device.</p>
    <button class="btn" onclick="zfsDoReplace('${jsArg(pool)}','${jsArg(device)}')">Replace</button>
  `);
}

async function zfsDoReplace(pool, device) {
  const new_device = $('zr-new').value.trim();
  if (!new_device) { alert('New device required'); return; }
  try {
    const r = await API.post(`/api/zfs/pools/${encodeURIComponent(pool)}/device`, { action: 'replace', device, new_device });
    if (!r.success) alert(r.stderr || 'Command failed');
    closeModal();
    zfsPoolDetail(pool);
  } catch(e) { alert(e.message); }
}

async function zfsAddVdev(pool) {
  const disks = await API.get('/api/disks');
  const diskItems = (disks.devices || []).filter(d => d.type === 'disk' && d.usage === 'Free').map(d =>
    ({ value: `/dev/${d.name}`, label: `/dev/${d.name} (${d.size})` }));
  openModal('Add Devices to ' + pool, `
    <div class="form-group"><label>Role</label>
      <select id="zv-role" class="form-control">
        <option value="">Data vdev (stripe)</option>
        <option value="mirror">Data vdev (mirror)</option>
        <option value="raidz">Data vdev (RAIDZ1)</option>
        <option value="raidz2">Data vdev (RAIDZ2)</option>
        <option value="raidz3">Data vdev (RAIDZ3)</option>
        <option value="cache">Cache (L2ARC)</option>
        <option value="log">Log (SLOG)</option>
        <option value="spare">Hot spare</option>
      </select></div>
    <div class="form-group"><label>Disks</label>
      ${checkboxList('zv-disks', diskItems, 'No free disks — wipe a disk on the Disks page first')}</div>
    <p class="help">Only free (unused) disks are shown. Cache / log / spare devices can be removed later (Remove button); a top-level data vdev generally cannot.</p>
    <button class="btn" onclick="zfsDoAddVdev('${jsArg(pool)}')">Add</button>
  `);
}

async function zfsDoAddVdev(pool) {
  const role = $('zv-role').value;
  const disks = checkedValues('zv-disks');
  if (!disks.length) { alert('Select at least one disk'); return; }
  try {
    const r = await API.post(`/api/zfs/pools/${encodeURIComponent(pool)}/vdev`, { role, disks });
    if (!r.success) alert(r.stderr || 'Command failed');
    closeModal();
    zfsPoolDetail(pool);
  } catch(e) { alert(e.message); }
}

// ─── iSCSI ──────────────────────────────────────────────
let iscsiBackstores = [];

async function page_schedules() {
  const data = await API.get('/api/snapshots/schedules');
  snapSchedules = data.schedules || [];
  const statusBanner = data.timer_active
    ? '<div class="health-ok">✓ Automatic snapshots are ON (timer active)</div>'
    : '<div class="alert alert-info">Automatic snapshots are OFF — they run only when at least one schedule below is enabled.</div>';
  const rows = snapSchedules.map(s => {
    const policy = SNAP_FREQS.filter(f => (s.keep || {})[f] > 0).map(f => `${f[0]}:${s.keep[f]}`).join(' ') || '—';
    const d = jsArg(s.dataset);
    return `<tr>
      <td><code>${escapeHtml(s.dataset)}</code></td>
      <td>${escapeHtml(policy)}</td>
      <td>${s.recursive ? 'yes' : 'no'}</td>
      <td><span class="status-badge ${s.enabled ? 'green' : 'gray'}">${s.enabled ? 'enabled' : 'disabled'}</span></td>
      <td>
        <button class="btn btn-sm" onclick="snapRunNow('${d}')">Run now</button>
        <button class="btn btn-sm" onclick="snapToggle('${d}')">${s.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn btn-sm" onclick="snapSchedModal('${d}')">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="snapDeleteSchedule('${d}')">Delete</button>
      </td>
    </tr>`;
  }).join('');
  $('page-content').innerHTML = `
    <h2>Automatic Snapshots</h2>
    ${statusBanner}
    <div class="toolbar"><button class="btn" onclick="snapSchedModal()">+ New Schedule</button></div>
    <table class="table">
      <thead><tr><th>Dataset / Pool</th><th>Keep (h/d/w/m)</th><th>Recursive</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5">No schedules — automatic snapshots are off.</td></tr>'}</tbody>
    </table>
    <p class="help">Snapshots are named <code>autosnap_&lt;freq&gt;_&lt;timestamp&gt;</code>; pruning only ever
      removes those, never your manual snapshots. Deleting/disabling a schedule stops snapshots and pruning
      but leaves existing snapshots in place. Targeting a pool with "recursive" snapshots every dataset in it.</p>
  `;
}

async function snapSchedModal(dataset) {
  const existing = dataset ? snapSchedules.find(s => s.dataset === dataset) : null;
  let targets = [];
  try { targets = await API.get('/api/zfs/datasets/all'); } catch(e) {}
  window.__snapPools = targets.filter(t => t.is_pool).map(t => t.name);
  const opts = targets.map(t =>
    `<option value="${escapeHtml(t.name)}" ${existing && existing.dataset === t.name ? 'selected' : ''}>${escapeHtml(t.name)}${t.is_pool ? ' (pool)' : ''}</option>`
  ).join('');
  const keep = (existing && existing.keep) || { hourly: 0, daily: 14, weekly: 8, monthly: 6 };
  const keepInput = f => `<input id="sc-${f}" class="form-control" type="number" min="0" value="${keep[f] || 0}" style="width:90px">`;
  openModal(existing ? 'Edit Schedule' : 'New Snapshot Schedule', `
    <div class="form-group"><label>Dataset / Pool</label>
      <select id="sc-dataset" class="form-control" onchange="snapTargetChange()" ${existing ? 'disabled' : ''}>
        ${opts || '<option value="">No datasets</option>'}
      </select>
    </div>
    <div class="form-group"><label><input type="checkbox" id="sc-recursive" ${existing && existing.recursive ? 'checked' : ''}> Recursive (include child datasets — required for a whole pool)</label></div>
    <label>Keep how many of each:</label>
    <div style="display:flex;gap:12px;margin:8px 0;flex-wrap:wrap">
      <div>Hourly ${keepInput('hourly')}</div><div>Daily ${keepInput('daily')}</div>
      <div>Weekly ${keepInput('weekly')}</div><div>Monthly ${keepInput('monthly')}</div>
    </div>
    <p class="help">0 disables that frequency. The hourly timer fires on the hour; each due frequency snapshots then prunes to its keep count.</p>
    <div class="form-group"><label><input type="checkbox" id="sc-enabled" ${!existing || existing.enabled ? 'checked' : ''}> Enabled</label></div>
    <button class="btn" onclick="snapSaveSchedule()">${existing ? 'Save' : 'Create'}</button>
  `);
}

function snapTargetChange() {
  const v = $('sc-dataset').value;
  if ((window.__snapPools || []).includes(v)) $('sc-recursive').checked = true;
}

async function snapSaveSchedule() {
  const dataset = $('sc-dataset').value.trim();
  if (!dataset) { alert('Select a dataset or pool'); return; }
  const keep = {};
  SNAP_FREQS.forEach(f => { keep[f] = parseInt($('sc-' + f).value) || 0; });
  if (SNAP_FREQS.every(f => keep[f] === 0)) { alert('Set a keep count for at least one frequency'); return; }
  try {
    await API.post('/api/snapshots/schedules', {
      dataset, recursive: $('sc-recursive').checked, enabled: $('sc-enabled').checked, keep
    });
    closeModal();
    page_schedules();
  } catch(e) { alert(e.message); }
}

async function snapToggle(dataset) {
  const s = snapSchedules.find(x => x.dataset === dataset);
  if (!s) return;
  try {
    await API.post('/api/snapshots/schedules', {
      dataset: s.dataset, recursive: s.recursive, enabled: !s.enabled, keep: s.keep
    });
    page_schedules();
  } catch(e) { alert(e.message); }
}

async function snapDeleteSchedule(dataset) {
  if (!confirm(`Delete the schedule for "${dataset}"? Existing snapshots are kept.`)) return;
  try {
    await API.delete(`/api/snapshots/schedules/${encodeURIComponent(dataset)}`);
    page_schedules();
  } catch(e) { alert(e.message); }
}

async function snapRunNow(dataset) {
  if (!confirm(`Take scheduled snapshots for "${dataset}" now?`)) return;
  try {
    const r = await API.post(`/api/snapshots/schedules/${encodeURIComponent(dataset)}/run`, {});
    const made = (r.results || []).map(x => `${x.freq}: ${x.ok ? 'ok' : (x.error || 'failed')}${x.pruned ? ` (pruned ${x.pruned})` : ''}`).join('\n');
    alert('Run complete:\n' + (made || 'nothing to do'));
    page_schedules();
  } catch(e) { alert(e.message); }
}

// ─── LVM ────────────────────────────────────────────────
let lvmState = { pvs: [], vgs: [] };

async function page_lvm() {
  const [lvm, disks] = await Promise.all([API.get('/api/lvm'), API.get('/api/disks')]);
  lvmState = { pvs: lvm.pvs, vgs: lvm.vgs };
  lvmState.freeDisks = (disks.devices || []).filter(d => d.type === 'disk' && d.usage === 'Free');

  const pvRows = lvm.pvs.map(p => {
    const d = jsArg(p.name);
    const acts = p.protected ? '<span class="help">system</span>' : `
      <button class="btn btn-sm" onclick="lvmPVResize('${d}')">Resize</button>
      ${p.vg ? `<button class="btn btn-sm" onclick="lvmPVMove('${d}')">Move</button>
                <button class="btn btn-sm" onclick="lvmVGReduce('${jsArg(p.vg)}','${d}')">Remove from VG</button>`
             : `<button class="btn btn-sm btn-danger" onclick="lvmPVRemove('${d}')">Delete</button>`}`;
    return `<tr><td><code>${escapeHtml(p.name)}</code></td><td>${escapeHtml(p.vg || '—')}</td><td>${escapeHtml(p.size)}</td><td>${escapeHtml(p.free)}</td><td>${acts}</td></tr>`;
  }).join('');

  const vgRows = lvm.vgs.map(g => {
    const n = jsArg(g.name);
    return `<tr><td><strong>${escapeHtml(g.name)}</strong>${g.protected ? ' <span class="status-badge gray">system</span>' : ''}</td>
      <td>${g.pv_count} PV / ${g.lv_count} LV</td><td>${escapeHtml(g.size)}</td><td>${escapeHtml(g.free)}</td>
      <td>
        <button class="btn btn-sm" onclick="lvmCreateLV('${n}')">+ LV</button>
        <button class="btn btn-sm" onclick="lvmVGExtend('${n}')">Add PV</button>
        ${g.protected ? '' : `<button class="btn btn-sm btn-danger" onclick="lvmVGRemove('${n}')">Delete</button>`}
      </td></tr>`;
  }).join('');

  const lvRows = lvm.lvs.map(l => {
    const acts = `<button class="btn btn-sm" onclick="lvmLVExtend('${jsArg(l.vg)}','${jsArg(l.name)}')">Extend</button>
      ${l.protected ? '' : `<button class="btn btn-sm btn-danger" onclick="lvmLVRemove('${jsArg(l.vg)}','${jsArg(l.name)}')">Delete</button>`}`;
    return `<tr><td>${escapeHtml(l.name)}</td><td>${escapeHtml(l.vg)}</td><td>${escapeHtml(l.size)}</td>
      <td>${escapeHtml(l.mountpoint || '—')}${l.protected ? ' <span class="status-badge gray">system</span>' : ''}</td><td>${acts}</td></tr>`;
  }).join('');

  $('page-content').innerHTML = `
    <h2>LVM</h2>
    <div class="toolbar">
      <button class="btn" onclick="lvmCreatePV()">+ Physical Volume</button>
      <button class="btn btn-outline" onclick="lvmCreateVG()">+ Volume Group</button>
    </div>
    <h3>Volume Groups</h3>
    <table class="table"><thead><tr><th>VG</th><th>Contents</th><th>Size</th><th>Free</th><th>Actions</th></tr></thead>
      <tbody>${vgRows || '<tr><td colspan="5">No volume groups</td></tr>'}</tbody></table>
    <h3>Logical Volumes</h3>
    <table class="table"><thead><tr><th>LV</th><th>VG</th><th>Size</th><th>Mounted</th><th>Actions</th></tr></thead>
      <tbody>${lvRows || '<tr><td colspan="5">No logical volumes</td></tr>'}</tbody></table>
    <h3>Physical Volumes</h3>
    <table class="table"><thead><tr><th>PV</th><th>VG</th><th>Size</th><th>Free</th><th>Actions</th></tr></thead>
      <tbody>${pvRows || '<tr><td colspan="5">No physical volumes</td></tr>'}</tbody></table>
    <p class="help">System volumes (backing a mounted filesystem, e.g. the OS disk) are protected from destructive actions.</p>
  `;
}

function lvmCreatePV() {
  const opts = (lvmState.freeDisks || []).map(d => `<option value="/dev/${d.name}">/dev/${d.name} (${escapeHtml(d.size)})</option>`).join('');
  openModal('Create Physical Volume', `
    <div class="form-group"><label>Free disk</label><select id="lv-pvdev" class="form-control">${opts || '<option value="">No free disks</option>'}</select></div>
    <p class="help">Only disks marked Free on the Disks page are listed.</p>
    <button class="btn" onclick="lvmDoCreatePV()">Create PV</button>`);
}
async function lvmDoCreatePV() {
  const device = $('lv-pvdev').value;
  if (!device) { alert('Select a disk'); return; }
  try { const r = await API.post('/api/lvm/pv', { device }); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } closeModal(); page_lvm(); }
  catch(e) { alert(e.message); }
}
async function lvmPVResize(device) {
  try { const r = await API.post('/api/lvm/pv/resize', { device }); if (!r.success) alert(r.error || r.stderr || 'Failed'); page_lvm(); } catch(e) { alert(e.message); }
}
async function lvmPVRemove(device) {
  if (!confirm(`Remove PV label from ${device}?`)) return;
  try { const r = await API.post('/api/lvm/pv/remove', { device }); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } page_lvm(); } catch(e) { alert(e.message); }
}
function lvmPVMove(source) {
  const opts = lvmState.pvs.filter(p => p.name !== source && p.vg).map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join('');
  openModal('Move data off ' + source, `
    <p>Move all extents off <code>${escapeHtml(source)}</code> onto another PV in the group, so it can be removed.</p>
    <div class="form-group"><label>Destination (optional — auto if blank)</label><select id="lv-mvdest" class="form-control"><option value="">Auto</option>${opts}</select></div>
    <button class="btn" onclick="lvmDoPVMove('${jsArg(source)}')">Move</button>`);
}
async function lvmDoPVMove(source) {
  try { const r = await API.post('/api/lvm/pv/move', { source, dest: $('lv-mvdest').value }); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } closeModal(); page_lvm(); } catch(e) { alert(e.message); }
}

function lvmCreateVG() {
  const free = lvmState.pvs.filter(p => !p.vg);
  const pvItems = free.map(p => ({ value: p.name, label: `${p.name} (${p.size})` }));
  openModal('Create Volume Group', `
    <div class="form-group"><label>Name</label><input id="lv-vgname" class="form-control" placeholder="data"></div>
    <div class="form-group"><label>Physical Volumes (check one or more)</label>${checkboxList('lv-vgpvs', pvItems, 'No unused PVs — create one first')}</div>
    <button class="btn" onclick="lvmDoCreateVG()">Create VG</button>`);
}
async function lvmDoCreateVG() {
  const name = $('lv-vgname').value.trim();
  const devices = checkedValues('lv-vgpvs').filter(Boolean);
  if (!name || !devices.length) { alert('Name and at least one PV required'); return; }
  try { const r = await API.post('/api/lvm/vg', { name, devices }); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } closeModal(); page_lvm(); } catch(e) { alert(e.message); }
}
function lvmVGExtend(vg) {
  const free = lvmState.pvs.filter(p => !p.vg);
  const opts = free.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)} (${escapeHtml(p.size)})</option>`).join('');
  openModal('Add PV to ' + vg, `
    <div class="form-group"><label>Unused Physical Volume</label><select id="lv-extpv" class="form-control">${opts || '<option value="">No unused PVs</option>'}</select></div>
    <button class="btn" onclick="lvmDoVGExtend('${jsArg(vg)}')">Add</button>`);
}
async function lvmDoVGExtend(vg) {
  const device = $('lv-extpv').value;
  if (!device) { alert('Select a PV'); return; }
  try { const r = await API.post(`/api/lvm/vg/${encodeURIComponent(vg)}/extend`, { device }); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } closeModal(); page_lvm(); } catch(e) { alert(e.message); }
}
async function lvmVGReduce(vg, device) {
  if (!confirm(`Remove ${device} from VG ${vg}? (must be empty — use Move first if not)`)) return;
  try { const r = await API.post(`/api/lvm/vg/${encodeURIComponent(vg)}/reduce`, { device }); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } page_lvm(); } catch(e) { alert(e.message); }
}
async function lvmVGRemove(vg) {
  if (!confirm(`Delete volume group ${vg}? (remove its LVs first)`)) return;
  try { const r = await API.delete(`/api/lvm/vg/${encodeURIComponent(vg)}`); page_lvm(); } catch(e) { alert(e.message); }
}

function lvmCreateLV(vg) {
  const vgOpts = lvmState.vgs.map(g => `<option value="${escapeHtml(g.name)}" ${g.name === vg ? 'selected' : ''}>${escapeHtml(g.name)} (${escapeHtml(g.free)} free)</option>`).join('');
  openModal('Create Logical Volume', `
    <div class="form-group"><label>Volume Group</label><select id="lv-lvvg" class="form-control">${vgOpts}</select></div>
    <div class="form-group"><label>Name</label><input id="lv-lvname" class="form-control" placeholder="vol1"></div>
    <div class="form-group"><label>Size</label><input id="lv-lvsize" class="form-control" placeholder="10G or 100%FREE"></div>
    <div class="form-group"><label>Filesystem</label><select id="lv-lvfs" class="form-control"><option value="">None</option><option value="ext4">ext4</option><option value="xfs">xfs</option></select></div>
    <button class="btn" onclick="lvmDoCreateLV()">Create LV</button>`);
}
async function lvmDoCreateLV() {
  const vg = $('lv-lvvg').value, name = $('lv-lvname').value.trim(), size = $('lv-lvsize').value.trim(), fstype = $('lv-lvfs').value;
  if (!vg || !name || !size) { alert('VG, name and size required'); return; }
  try { const r = await API.post('/api/lvm/lv', { vg, name, size, fstype }); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } closeModal(); page_lvm(); } catch(e) { alert(e.message); }
}
function lvmLVExtend(vg, name) {
  openModal(`Extend ${vg}/${name}`, `
    <div class="form-group"><label>Add / grow to size</label><input id="lv-extsize" class="form-control" placeholder="+10G, 50G, or 100%FREE"></div>
    <div class="form-group"><label><input type="checkbox" id="lv-extfs" checked> Grow the filesystem too</label></div>
    <p class="help">Extend only — this never shrinks a volume.</p>
    <button class="btn" onclick="lvmDoLVExtend('${jsArg(vg)}','${jsArg(name)}')">Extend</button>`);
}
async function lvmDoLVExtend(vg, name) {
  const size = $('lv-extsize').value.trim();
  if (!size) { alert('Size required'); return; }
  try { const r = await API.post(`/api/lvm/lv/${encodeURIComponent(vg)}/${encodeURIComponent(name)}/extend`, { size, resize_fs: $('lv-extfs').checked }); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } closeModal(); page_lvm(); } catch(e) { alert(e.message); }
}
async function lvmLVRemove(vg, name) {
  if (!confirm(`Delete logical volume ${vg}/${name}? This destroys its data.`)) return;
  try { const r = await API.delete(`/api/lvm/lv/${encodeURIComponent(vg)}/${encodeURIComponent(name)}`); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } page_lvm(); } catch(e) { alert(e.message); }
}

// ─── MD RAID ────────────────────────────────────────────
let mdState = { freeDisks: [], arrays: [] };

async function page_mdraid() {
  const [md, disks] = await Promise.all([API.get('/api/mdadm/arrays'), API.get('/api/disks')]);
  mdState.freeDisks = (disks.devices || []).filter(d => d.type === 'disk' && d.usage === 'Free');
  mdState.arrays = md.arrays;

  const rows = md.arrays.map(a => {
    const dv = jsArg(a.device);
    const members = (a.devices || []).map(d => `${d.device.replace('/dev/', '')} (${escapeHtml(d.state)})`).join(', ');
    const stateCls = /degraded|fail/i.test(a.state) ? 'red' : /clean|active/i.test(a.state) ? 'green' : 'yellow';
    return `<tr>
      <td><code>${escapeHtml(a.device)}</code></td>
      <td>${escapeHtml(a.level)}</td>
      <td><span class="status-badge ${stateCls}">${escapeHtml(a.state)}</span>${a.sync ? `<div class="help">${escapeHtml(a.sync)}</div>` : ''}</td>
      <td>${escapeHtml(a.size)}</td>
      <td style="font-size:12px">${escapeHtml(members)}</td>
      <td>
        <button class="btn btn-sm" onclick="mdManage('${dv}')">Manage</button>
        ${a.protected ? '<span class="help">in use</span>'
          : `<button class="btn btn-sm" onclick="mdStop('${dv}')">Stop</button>
             <button class="btn btn-sm btn-danger" onclick="mdDelete('${dv}')">Delete</button>`}
      </td>
    </tr>`;
  }).join('');

  $('page-content').innerHTML = `
    <h2>MD RAID (Software RAID)</h2>
    <div class="toolbar">
      <button class="btn" onclick="mdCreate()">+ New Array</button>
      <button class="btn btn-outline" onclick="mdAssemble()">Assemble (scan)</button>
    </div>
    <table class="table">
      <thead><tr><th>Array</th><th>Level</th><th>State</th><th>Size</th><th>Members</th><th>Actions</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6">No arrays</td></tr>'}</tbody>
    </table>
    <p class="help">Members can only be disks marked Free on the Disks page. Arrays backing a mounted
      filesystem, ZFS pool, or LVM are protected from stop/delete. New arrays are saved to mdadm.conf.</p>
  `;
}

function mdCreate() {
  const mdItems = mdState.freeDisks.map(d => ({ value: `/dev/${d.name}`, label: `/dev/${d.name} (${d.size})` }));
  openModal('Create RAID Array', `
    <div class="form-group"><label>Name</label><input id="md-name" class="form-control" placeholder="data"></div>
    <div class="form-group"><label>RAID level</label>
      <select id="md-level" class="form-control">
        <option value="1">RAID1 (mirror, 2+)</option><option value="0">RAID0 (stripe, 2+)</option>
        <option value="5">RAID5 (3+)</option><option value="6">RAID6 (4+)</option><option value="10">RAID10 (4+)</option>
      </select></div>
    <div class="form-group"><label>Member disks (check one or more)</label>${checkboxList('md-devs', mdItems, 'No free disks')}</div>
    <div class="form-group"><label>Spare disks (optional)</label>${checkboxList('md-spares', mdItems, 'No free disks')}</div>
    <label style="display:block"><input type="checkbox" id="md-persist" checked> Persist for boot (update initramfs)</label>
    <p class="help">A disk used as a member cannot also be a spare. Updating initramfs takes a few seconds.</p>
    <button class="btn" onclick="mdDoCreate()">Create Array</button>`);
}

async function mdDoCreate() {
  const name = $('md-name').value.trim(), level = $('md-level').value;
  const devices = checkedValues('md-devs').filter(Boolean);
  const spares = checkedValues('md-spares').filter(Boolean).filter(s => !devices.includes(s));
  if (!name || !devices.length) { alert('Name and member disks required'); return; }
  try {
    const r = await API.post('/api/mdadm/arrays', { name, level, devices, spares, persist: $('md-persist').checked });
    if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; }
    closeModal(); page_mdraid();
  } catch(e) { alert(e.message); }
}

function mdManage(dev) {
  const a = mdState.arrays.find(x => x.device === dev) || { devices: [] };
  const memberRows = (a.devices || []).map(d => `<tr><td><code>${escapeHtml(d.device)}</code></td><td>${escapeHtml(d.state)}</td>
    <td>${a.protected ? '' : `<button class="btn btn-sm" onclick="mdDeviceAction('${jsArg(dev)}','fail','${jsArg(d.device)}')">Fail</button>
      <button class="btn btn-sm btn-danger" onclick="mdDeviceAction('${jsArg(dev)}','remove','${jsArg(d.device)}')">Remove</button>`}</td></tr>`).join('');
  const opts = mdState.freeDisks.map(d => `<option value="/dev/${d.name}">/dev/${d.name} (${escapeHtml(d.size)})</option>`).join('');
  openModal('Manage ' + dev, `
    <p>${escapeHtml(a.level)} — ${escapeHtml(a.state)}${a.sync ? ' — ' + escapeHtml(a.sync) : ''}</p>
    <table class="table"><thead><tr><th>Device</th><th>State</th><th></th></tr></thead><tbody>${memberRows}</tbody></table>
    <h4>Add disk (spare / replacement)</h4>
    <div class="form-group" style="display:flex;gap:8px"><select id="md-adddev" class="form-control">${opts || '<option value="">No free disks</option>'}</select>
      <button class="btn btn-sm" onclick="mdDeviceAction('${jsArg(dev)}','add',$('md-adddev').value)">Add</button></div>
    <p class="help">To replace a disk: Fail it, Remove it, then Add a free replacement (which rebuilds).</p>`, {wide:true});
}

async function mdDeviceAction(dev, action, device) {
  if (!device) { alert('Select a device'); return; }
  if ((action === 'fail' || action === 'remove') && !confirm(`${action} ${device} on ${dev}?`)) return;
  try {
    const r = await API.post(`/api/mdadm/arrays/${encodeURIComponent(dev)}/device`, { action, device });
    if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; }
    await page_mdraid(); mdManage(dev);
  } catch(e) { alert(e.message); }
}

async function mdStop(dev) {
  if (!confirm(`Stop array ${dev}?`)) return;
  try { const r = await API.post(`/api/mdadm/arrays/${encodeURIComponent(dev)}/stop`, {}); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } page_mdraid(); } catch(e) { alert(e.message); }
}

async function mdAssemble() {
  try { await API.post('/api/mdadm/assemble', {}); page_mdraid(); } catch(e) { alert(e.message); }
}

async function mdDelete(dev) {
  if (!confirm(`Delete array ${dev}? This stops it and wipes RAID superblocks from its disks.`)) return;
  try { const r = await API.delete(`/api/mdadm/arrays/${encodeURIComponent(dev)}`); if (!r.success) { alert(r.error || r.stderr || 'Failed'); return; } page_mdraid(); } catch(e) { alert(e.message); }
}

// ─── Dashboard users (admin) ────────────────────────────
