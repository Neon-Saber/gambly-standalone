async function loadOverview() {
  const o = await apiGet('/api/overview');
  if (!o) return;
  document.getElementById('admin-stats').innerHTML = `
    <div class="stat"><div class="num">${o.guild_count}</div><div class="label">Servers total</div></div>
    <div class="stat good"><div class="num">${o.installed_count}</div><div class="label">Actually installed</div></div>
    <div class="stat ${o.ghost_count ? 'danger' : ''}"><div class="num">${o.ghost_count}</div><div class="label">Has data, not installed</div></div>
    <div class="stat"><div class="num">${o.total_players}</div><div class="label">Players</div></div>
    <div class="stat good"><div class="num chip">${fmtChips(o.total_chips_in_circulation)}</div><div class="label">Chips in circulation</div></div>
    <div class="stat warn"><div class="num chip">${fmtChips(o.total_debt_owed)}</div><div class="label">Total debt</div></div>
    <div class="stat danger"><div class="num">${o.defaulted_count}</div><div class="label">Defaulted loans</div></div>
    <div class="stat danger"><div class="num">${o.banned_count}</div><div class="label">Banned</div></div>`;
}

function guildInitial(name) {
  return (name || '?').trim().slice(0, 1).toUpperCase();
}

async function loadGuilds() {
  const list = await apiGet('/api/all_guilds');
  const grid = document.getElementById('admin-guilds-grid');
  if (!list || !list.length) {
    grid.innerHTML = `<div class="empty">Nothing yet.</div>`;
    return;
  }
  grid.innerHTML = list.map((g) => {
    const isDm = g.id === 'dm';
    const iconHtml = g.icon
      ? `<img src="${g.icon}" alt="">`
      : guildInitial(g.name);
    let tag;
    if (isDm) {
      tag = `<div class="tag tag-active">● Personal / DM ledger</div>`;
    } else if (g.installed) {
      tag = g.user_count
        ? `<div class="tag tag-active">● Installed</div>`
        : `<div class="tag">Installed — no activity yet</div>`;
    } else {
      tag = `<div class="tag tag-warn">⚠ Not installed — used via personal app</div>`;
    }
    const substat = isDm ? '' : `<div class="substat">${g.user_count} player${g.user_count === 1 ? '' : 's'} · ${g.banned_count} banned</div>`;
    const gidLine = g.unnamed ? `<div class="gid">${g.id}</div>` : '';
    return `
      <a class="guild-card" href="/dashboard/${g.id}">
        <div class="guild-icon${g.installed || isDm ? '' : ' ghost'}">${iconHtml}</div>
        <div class="guild-meta">
          <div class="name">${escapeHtml(g.name)}</div>
          ${tag}
          ${substat}
          ${gidLine}
        </div>
      </a>`;
  }).join('');
}

async function loadSettings() {
  const s = await apiGet('/api/settings');
  if (!s) return;
  const labels = {
    starting_bal: 'Starting balance', daily_amt: 'Daily claim amount',
    bank_interest: 'Bank interest (0-1)', loan_max: 'Max loan', loan_interest: 'Loan interest (0-1)',
  };
  const el = document.getElementById('admin-settings-fields');
  el.innerHTML = Object.keys(labels).map((k) => `
    <div class="field"><label>${labels[k]}</label><input type="number" step="any" id="gs-${k}" value="${s[k]}"></div>`).join('');
  document.getElementById('admin-settings-save').onclick = async () => {
    const body = {};
    Object.keys(labels).forEach((k) => { body[k] = Number(document.getElementById(`gs-${k}`).value); });
    const r = await apiPost('/api/settings', body);
    if (r) toast('Saved.', 'ok');
  };
}

let searchTimer;
document.getElementById('admin-search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  searchTimer = setTimeout(async () => {
    const tbody = document.querySelector('#admin-search-table tbody');
    if (!q) { tbody.innerHTML = ''; return; }
    const results = await apiGet(`/api/search?q=${encodeURIComponent(q)}`);
    tbody.innerHTML = (results || []).map((r) => `
      <tr>
        <td>${escapeHtml(r.name)} <span class="pid">${r.user_id}</span></td>
        <td>${escapeHtml(r.guild_name)}</td>
        <td class="num chip">${fmtChips(r.bal)}</td>
        <td class="num chip">${fmtChips(r.bank)}</td>
      </tr>`).join('') || `<tr><td colspan="4" class="empty">No matches.</td></tr>`;
  }, 250);
});

loadOverview();
loadGuilds();
loadSettings();
