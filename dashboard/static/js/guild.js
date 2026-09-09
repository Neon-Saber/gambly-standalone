const GID = document.body.dataset.gid;
let STATE = null;

const TABS = ['overview', 'players', 'settings', 'moderation', 'games', 'leveling', 'bounties', 'activity', 'danger'];

function showTab(name) {
  TABS.forEach((t) => {
    document.getElementById('tab-' + t).style.display = t === name ? '' : 'none';
  });
  document.querySelectorAll('.nav-item[data-tab]').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  if (name === 'activity') loadActivity();
}

document.querySelectorAll('.nav-item[data-tab]').forEach((b) => {
  b.addEventListener('click', () => showTab(b.dataset.tab));
});

function iconHtml(name, url, size) {
  size = size || 40;
  if (url) return `<img src="${url}" alt="">`;
  return escapeHtml((name || '?').slice(0, 1).toUpperCase());
}

// Build <option> HTML for a channel/role picker, given a list from
// bot_fetch_channels()/bot_fetch_roles() (or null if that call failed -
// usually a missing/bad DISCORD_TOKEN on whichever machine runs the
// dashboard). This used to just render a single disabled "couldn't load"
// option with value="" whenever the fetch failed - which meant hitting
// Save while the list was down silently WIPED whatever channel/role was
// already configured back to nothing (exactly what broke welcome/leveling/
// member-count saves). Now: an already-saved id is always preserved as a
// locked-in option (with a visible warning) instead of getting lost, and
// the same warning shows if a saved id no longer matches anything in a
// list that DID load (e.g. the channel got deleted).
function pickerOptions(list, selectedId, opts) {
  opts = opts || {};
  const emptyLabel = opts.emptyLabel || 'None';
  const prefix = opts.prefix || '';
  selectedId = selectedId ? String(selectedId) : '';
  if (!list) {
    if (selectedId) {
      return `<option value="${selectedId}" selected>⚠ couldn't load the list - keeping current (${selectedId})</option>`;
    }
    return `<option value="">⚠ Couldn't load — check the bot token</option>`;
  }
  const known = list.some((c) => c.id === selectedId);
  const missing = (selectedId && !known)
    ? `<option value="${selectedId}" selected>⚠ unknown (${selectedId}) - deleted?</option>`
    : '';
  const rendered = list.map((c) =>
    `<option value="${c.id}" ${selectedId === c.id ? 'selected' : ''}>${prefix}${escapeHtml(c.name)}</option>`).join('');
  return `<option value="">${emptyLabel}</option>${missing}${rendered}`;
}

async function init() {
  STATE = await apiGet(`/api/guild/${GID}`);
  if (!STATE) return;
  document.getElementById('crumb-name').textContent = STATE.name;
  document.getElementById('head-name').textContent = STATE.name;
  document.title = STATE.name + ' — Dashboard';
  document.getElementById('head-icon').innerHTML = iconHtml(STATE.name, STATE.icon);
  document.getElementById('side-icon').innerHTML = iconHtml(STATE.name, STATE.icon);
  renderOverview();
  renderPlayers();
  renderSettings();
  renderModeration();
  renderGameChannels();
  renderLeveling();
  renderBounties();
  renderDanger();
}

// ---------------------------------------------------------------- overview
function renderOverview() {
  const el = document.getElementById('tab-overview');
  const users = STATE.users;
  const totalWallet = users.reduce((s, u) => s + u.bal, 0);
  const totalBank = users.reduce((s, u) => s + u.bank, 0);
  const totalDebt = users.reduce((s, u) => s + u.loan_owed, 0);
  const defaulted = users.filter((u) => u.loan_defaulted).length;
  const banned = users.filter((u) => u.is_banned).length;
  const top = users.slice(0, 8);

  el.innerHTML = `
    <div class="stat-row">
      <div class="stat"><div class="num">${users.length}</div><div class="label">Players</div></div>
      <div class="stat good"><div class="num chip">${fmtChips(totalWallet + totalBank)}</div><div class="label">Chips in circulation</div></div>
      <div class="stat warn"><div class="num chip">${fmtChips(totalDebt)}</div><div class="label">Total debt owed</div></div>
      <div class="stat ${defaulted ? 'danger' : ''}"><div class="num">${defaulted}</div><div class="label">Defaulted loans</div></div>
      <div class="stat ${banned ? 'danger' : ''}"><div class="num">${banned}</div><div class="label">Banned players</div></div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Richest players</h2><span class="hint">Wallet + bank</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Player</th><th class="num">Wallet</th><th class="num">Bank</th><th class="num">Total</th></tr></thead>
        <tbody>${top.map((u) => `
          <tr>
            <td class="player-name">${escapeHtml(u.name)} <span class="pid">${u.id}</span></td>
            <td class="num chip">${fmtChips(u.bal)}</td>
            <td class="num chip">${fmtChips(u.bank)}</td>
            <td class="num chip">${fmtChips(u.bal + u.bank)}</td>
          </tr>`).join('') || `<tr><td colspan="4" class="empty">No players yet.</td></tr>`}
        </tbody>
      </table></div>
    </div>`;
}

// ----------------------------------------------------------------- players
function renderPlayers() {
  const el = document.getElementById('tab-players');
  el.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h2>Bulk grant</h2>
        <span class="hint">Applies to every player in this server</span>
      </div>
      <div class="input-row" style="max-width:340px;">
        <input type="number" id="bulk-amount" placeholder="Amount (negative to deduct)">
        <button class="btn btn-gold" id="bulk-grant-btn">Apply</button>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <h2>Players</h2>
        <a class="btn btn-sm" href="/api/guild/${GID}/export.csv">Export CSV</a>
      </div>
      <div class="table-wrap"><table>
        <thead><tr>
          <th>Player</th><th class="num">Wallet</th><th class="num">Bank</th><th class="num">Loan</th><th>Status</th><th></th>
        </tr></thead>
        <tbody id="players-tbody"></tbody>
      </table></div>
    </div>`;
  document.getElementById('bulk-grant-btn').addEventListener('click', async () => {
    const amount = Number(document.getElementById('bulk-amount').value);
    if (!amount) { toast('Enter a nonzero amount.', 'error'); return; }
    if (!confirm(`Apply ${amount} chips to all ${STATE.users.length} players?`)) return;
    const r = await apiPost(`/api/guild/${GID}/bulk_grant`, { amount });
    if (r) { toast(`Applied to ${r.affected} players.`, 'ok'); refresh(); }
  });
  renderPlayerRows();
}

function renderPlayerRows() {
  const tbody = document.getElementById('players-tbody');
  if (!STATE.users.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">No players have used the bot here yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = STATE.users.map((u) => `
    <tr data-uid="${u.id}">
      <td class="player-name">${escapeHtml(u.name)}<br><span class="pid">${u.id}</span></td>
      <td class="num"><input type="number" class="mono" style="width:100px" value="${u.bal}" data-field="bal"></td>
      <td class="num"><input type="number" class="mono" style="width:100px" value="${u.bank}" data-field="bank"></td>
      <td class="num chip">${u.loan_owed ? fmtChips(u.loan_owed) + (u.loan_defaulted ? ' (defaulted)' : '') : '—'}</td>
      <td>
        ${u.is_manager ? '<span class="badge badge-gold">Manager</span>' : ''}
        ${u.is_banned ? '<span class="badge badge-red">Banned</span>' : ''}
        ${!STATE.is_personal && !u.is_manager && !u.is_banned ? '<span class="badge badge-muted">Player</span>' : ''}
      </td>
      <td>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <button class="btn btn-sm" data-act="save">Save</button>
          ${!STATE.is_personal ? `<button class="btn btn-sm" data-act="ban">${u.is_banned ? 'Unban' : 'Ban'}</button>` : ''}
          ${!STATE.is_personal ? `<button class="btn btn-sm" data-act="manager">${u.is_manager ? 'Remove mgr' : 'Make mgr'}</button>` : ''}
          ${u.loan_owed ? '<button class="btn btn-sm" data-act="forgive">Forgive loan</button>' : ''}
          <button class="btn btn-sm btn-danger" data-act="reset">Reset</button>
        </div>
      </td>
    </tr>`).join('');

  tbody.querySelectorAll('button[data-act]').forEach((btn) => {
    btn.addEventListener('click', () => handlePlayerAction(btn));
  });
}

async function handlePlayerAction(btn) {
  const row = btn.closest('tr');
  const uid = row.dataset.uid;
  const act = btn.dataset.act;
  const u = STATE.users.find((x) => x.id === uid);

  if (act === 'save') {
    const bal = Number(row.querySelector('[data-field=bal]').value);
    const bank = Number(row.querySelector('[data-field=bank]').value);
    let ok = true;
    if (bal !== u.bal) ok = ok && !!(await apiPost(`/api/guild/${GID}/balance`, { user_id: uid, balance: bal }));
    if (bank !== u.bank) ok = ok && !!(await apiPost(`/api/guild/${GID}/bank`, { user_id: uid, bank: bank }));
    if (ok) { toast(`Updated ${u.name}.`, 'ok'); refresh(); }
  } else if (act === 'ban') {
    const r = await apiPost(`/api/guild/${GID}/ban/${uid}`, { banned: !u.is_banned });
    if (r) { toast(`${u.name} ${u.is_banned ? 'unbanned' : 'banned'}.`, 'ok'); refresh(); }
  } else if (act === 'manager') {
    const r = await apiPost(`/api/guild/${GID}/manager/${uid}`, { manager: !u.is_manager });
    if (r) { toast(`${u.name} manager status updated.`, 'ok'); refresh(); }
  } else if (act === 'forgive') {
    if (!confirm(`Forgive ${u.name}'s ${fmtChips(u.loan_owed)}-chip debt?`)) return;
    const r = await apiPost(`/api/guild/${GID}/forgive/${uid}`, {});
    if (r) { toast('Loan forgiven.', 'ok'); refresh(); }
  } else if (act === 'reset') {
    if (!confirm(`Fully reset ${u.name}'s account? This cannot be undone.`)) return;
    const r = await apiPost(`/api/guild/${GID}/reset/${uid}`, {});
    if (r) { toast(`${u.name} reset.`, 'ok'); refresh(); }
  }
}

// ----------------------------------------------------------------- settings
async function renderSettings() {
  const el = document.getElementById('tab-settings');
  if (STATE.is_personal) {
    el.innerHTML = `<div class="empty">The personal/DM ledger doesn't have server settings.</div>`;
    return;
  }
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>General</h2></div>
      <div class="field-grid">
        <div class="field">
          <label>Command prefix</label>
          <input type="text" id="s-prefix" maxlength="3" value="${escapeHtml(STATE.prefix)}">
        </div>
        <div class="field">
          <label>Manager role</label>
          <select id="s-manager-role"><option value="">Loading roles…</option></select>
          <span class="desc">Anyone with this role can manage the economy here, in addition to server admins.</span>
        </div>
      </div>
      <button class="btn btn-gold" id="s-general-save">Save</button>
    </div>

    <div class="card">
      <div class="card-head"><h2>Economy</h2></div>
      <div class="field-grid">
        <div class="field"><label>Jackpot</label><input type="number" id="s-jackpot" value="${STATE.jackpot}"></div>
        <div class="field"><label>Tax %</label><input type="number" id="s-tax" min="0" max="100" value="${STATE.tax_pct}"></div>
        <div class="field"><label>Server pot</label><input type="number" id="s-pot" value="${STATE.server_pot}"></div>
        <div class="field"><label>Lottery pot</label><input type="number" id="s-lottery" value="${STATE.lottery_pot}"></div>
      </div>
      <div class="switch-row" style="margin-bottom:16px;">
        <label class="switch"><input type="checkbox" id="s-testmode" ${STATE.testmode ? 'checked' : ''}><span class="track"></span></label>
        <span class="lbl">Test mode — the owner's own bets always win, everyone else stays normal</span>
      </div>
      <button class="btn btn-gold" id="s-econ-save">Save</button>
    </div>`;

  document.getElementById('s-general-save').addEventListener('click', async () => {
    const prefix = document.getElementById('s-prefix').value || '!';
    const roleId = document.getElementById('s-manager-role').value;
    const ok1 = await apiPost(`/api/guild/${GID}/prefix`, { prefix });
    const ok2 = await apiPost(`/api/guild/${GID}/manager_role`, { role_id: roleId || null });
    if (ok1 && ok2) toast('Saved.', 'ok');
  });

  document.getElementById('s-econ-save').addEventListener('click', async () => {
    const body = {
      jackpot: Number(document.getElementById('s-jackpot').value),
      tax_pct: Number(document.getElementById('s-tax').value),
      server_pot: Number(document.getElementById('s-pot').value),
      lottery_pot: Number(document.getElementById('s-lottery').value),
      testmode: document.getElementById('s-testmode').checked,
    };
    const r = await apiPost(`/api/guild/${GID}/config`, body);
    if (r) toast('Saved.', 'ok');
  });

  const roles = await apiGet(`/api/guild/${GID}/roles`);
  document.getElementById('s-manager-role').innerHTML = pickerOptions(roles, STATE.manager_role);
}

// -------------------------------------------------------- moderation & logging
const LOG_TYPES = [
  { key: 'mod', label: 'Moderation actions', desc: 'Kicks, bans, mutes, warns, purges, lock/unlock' },
  { key: 'ticket', label: 'Ticket transcripts', desc: 'Posted when a ticket is closed' },
  { key: 'report', label: 'User reports', desc: '/report and the "Report Message" action' },
  { key: 'message', label: 'Message edits & deletes', desc: 'Can get noisy in an active server' },
  { key: 'withdraw', label: 'Withdrawals', desc: 'Bank → wallet transfers' },
  { key: 'deposit', label: 'Deposits', desc: 'Wallet → bank transfers' },
];

async function renderModeration() {
  const el = document.getElementById('tab-moderation');
  if (STATE.is_personal) {
    el.innerHTML = `<div class="empty">Not applicable to the personal/DM ledger.</div>`;
    return;
  }
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Staff & tickets</h2></div>
      <div class="field-grid">
        <div class="field">
          <label>Staff role</label>
          <select id="m-staff-role"><option value="">Loading roles…</option></select>
          <span class="desc">Can use moderation & ticket-staff commands, in addition to server admins.</span>
        </div>
        <div class="field">
          <label>Ticket category</label>
          <select id="m-ticket-category"><option value="">Loading channels…</option></select>
          <span class="desc">New ticket channels are created under this category.</span>
        </div>
        <div class="field">
          <label>Ticket ping role (optional)</label>
          <select id="m-ticket-ping"><option value="">Loading roles…</option></select>
          <span class="desc">Pinged whenever a new ticket opens.</span>
        </div>
      </div>
      <button class="btn btn-gold" id="m-staff-save">Save</button>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>Logging</h2>
        <span class="hint">Each log type sends professional embeds to the channel you pick</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Log type</th><th>Enabled</th><th>Channel</th></tr></thead>
        <tbody>${LOG_TYPES.map((lt) => `
          <tr data-log="${lt.key}">
            <td>${escapeHtml(lt.label)}<br><span class="pid">${escapeHtml(lt.desc)}</span></td>
            <td><label class="switch"><input type="checkbox" data-log-enabled ${STATE.logging[lt.key].enabled ? 'checked' : ''}><span class="track"></span></label></td>
            <td><select data-log-channel style="min-width:180px;"><option value="">Loading channels…</option></select></td>
          </tr>`).join('')}
        </tbody>
      </table></div>
      <button class="btn btn-gold" id="m-logging-save" style="margin-top:12px;">Save</button>
    </div>`;

  document.getElementById('m-staff-save').addEventListener('click', async () => {
    const r = await apiPost(`/api/guild/${GID}/moderation_config`, {
      staff_role_id: document.getElementById('m-staff-role').value || null,
      ticket_category_id: document.getElementById('m-ticket-category').value || null,
      ticket_ping_role_id: document.getElementById('m-ticket-ping').value || null,
    });
    if (r) { toast('Saved.', 'ok'); refresh(); }
  });

  document.getElementById('m-logging-save').addEventListener('click', async () => {
    const body = {};
    document.querySelectorAll('#tab-moderation tr[data-log]').forEach((row) => {
      const key = row.dataset.log;
      body[`log_${key}_enabled`] = row.querySelector('[data-log-enabled]').checked;
      body[`log_${key}_channel`] = row.querySelector('[data-log-channel]').value || null;
    });
    const r = await apiPost(`/api/guild/${GID}/moderation_config`, body);
    if (r) { toast('Saved.', 'ok'); refresh(); }
  });

  const [roles, channels] = await Promise.all([
    apiGet(`/api/guild/${GID}/roles`),
    apiGet(`/api/guild/${GID}/channels`),
  ]);

  const roleOptions = (selectedId) => pickerOptions(roles, selectedId);
  const channelOptions = (selectedId) => pickerOptions(channels ? channels.text : null, selectedId, { prefix: '#' });
  const categoryOptions = (selectedId) => pickerOptions(channels ? channels.categories : null, selectedId);

  document.getElementById('m-staff-role').innerHTML = roleOptions(STATE.staff_role_id);
  document.getElementById('m-ticket-category').innerHTML = categoryOptions(STATE.ticket_category_id);
  document.getElementById('m-ticket-ping').innerHTML = roleOptions(STATE.ticket_ping_role_id);

  document.querySelectorAll('#tab-moderation tr[data-log]').forEach((row) => {
    const key = row.dataset.log;
    row.querySelector('[data-log-channel]').innerHTML = channelOptions(STATE.logging[key].channel);
  });
}

// -------------------------------------------------------------- game channels
async function renderGameChannels() {
  const el = document.getElementById('tab-games');
  if (STATE.is_personal) {
    el.innerHTML = `<div class="empty">Not applicable to the personal/DM ledger.</div>`;
    return;
  }
  const games = STATE.game_list || [];
  el.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h2>Per-game channels</h2>
        <span class="hint">Leave "Auto-detect" to match a channel by name (e.g. #slots) - pick a channel here to lock it manually instead</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Game</th><th>Channel</th></tr></thead>
        <tbody>${games.map((g) => `
          <tr data-game="${g}">
            <td>/${escapeHtml(g)}</td>
            <td><select data-game-channel style="min-width:200px;"><option value="">Loading channels…</option></select></td>
          </tr>`).join('')}
        </tbody>
      </table></div>
      <button class="btn btn-gold" id="games-save" style="margin-top:12px;">Save</button>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>Custom server env</h2>
        <span class="hint">Per-server overrides, one KEY=VALUE per line - checked before this server's real .env value for the same key (cog_utils.get_custom_setting)</span>
      </div>
      <textarea id="custom-env-box" rows="8" style="width:100%;font-family:monospace;" placeholder="SOME_KEY=some value"></textarea>
      <button class="btn btn-gold" id="env-save" style="margin-top:12px;">Save</button>
    </div>`;

  document.getElementById('custom-env-box').value =
    Object.entries(STATE.custom_env || {}).map(([k, v]) => `${k}=${v}`).join('\n');

  document.getElementById('games-save').addEventListener('click', async () => {
    const overrides = {};
    document.querySelectorAll('#tab-games tr[data-game]').forEach((row) => {
      overrides[row.dataset.game] = row.querySelector('[data-game-channel]').value || null;
    });
    const r = await apiPost(`/api/guild/${GID}/game_channels`, { game_channels: overrides });
    if (r) { toast('Saved.', 'ok'); refresh(); }
  });

  document.getElementById('env-save').addEventListener('click', async () => {
    const raw = document.getElementById('custom-env-box').value;
    const r = await apiPost(`/api/guild/${GID}/custom_env`, { raw });
    if (r) { toast('Saved.', 'ok'); refresh(); }
  });

  const channels = await apiGet(`/api/guild/${GID}/channels`);
  const channelOptions = (selectedId) => pickerOptions(channels ? channels.text : null, selectedId, { emptyLabel: 'Auto-detect', prefix: '#' });

  document.querySelectorAll('#tab-games tr[data-game]').forEach((row) => {
    const g = row.dataset.game;
    row.querySelector('[data-game-channel]').innerHTML = channelOptions((STATE.game_channels || {})[g]);
  });
}

// -------------------------------------------------------- leveling & welcome
async function renderLeveling() {
  const el = document.getElementById('tab-leveling');
  if (STATE.is_personal) {
    el.innerHTML = `<div class="empty">Not applicable to the personal/DM ledger.</div>`;
    return;
  }
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Welcome messages</h2></div>
      <div class="switch-row" style="margin-bottom:16px;">
        <label class="switch"><input type="checkbox" id="w-enabled" ${STATE.welcome_enabled ? 'checked' : ''}><span class="track"></span></label>
        <span class="lbl">Post a welcome embed when someone joins</span>
      </div>
      <div class="field-grid">
        <div class="field">
          <label>Welcome channel</label>
          <select id="w-channel"><option value="">Loading channels…</option></select>
        </div>
      </div>
      <div class="field" style="margin-top:12px;">
        <label>Message template</label>
        <textarea id="w-message" rows="3" style="width:100%;" placeholder="${escapeHtml('welcome {user} to **{server}**! you\'re member #{membercount} 🎉')}">${escapeHtml(STATE.welcome_message || '')}</textarea>
        <span class="desc">Placeholders: {user} (mention), {username}, {server}, {membercount}. Leave blank to use the built-in default. Setting WELCOME_MESSAGE in this server's .env always overrides whatever's saved here.</span>
      </div>
      <button class="btn btn-gold" id="w-save" style="margin-top:12px;">Save</button>
    </div>

    <div class="card">
      <div class="card-head"><h2>Leveling</h2></div>
      <div class="switch-row" style="margin-bottom:16px;">
        <label class="switch"><input type="checkbox" id="l-enabled" ${STATE.leveling_enabled ? 'checked' : ''}><span class="track"></span></label>
        <span class="lbl">Award XP for chatting and announce level-ups</span>
      </div>
      <div class="field-grid">
        <div class="field">
          <label>Level-up announce channel</label>
          <select id="l-channel"><option value="">Loading channels…</option></select>
        </div>
      </div>
      <div class="field" style="margin-top:12px;">
        <label>Level-up message template</label>
        <textarea id="l-message" rows="2" style="width:100%;" placeholder="${escapeHtml('🎉 {user} just reached **level {level}**!')}">${escapeHtml(STATE.level_message || '')}</textarea>
        <span class="desc">Placeholders: {user}, {username}, {server}, {level}. Leave blank for the built-in default. Setting LEVEL_UP_MESSAGE in this server's .env always overrides whatever's saved here.</span>
      </div>
      <button class="btn btn-gold" id="l-save" style="margin-top:12px;">Save</button>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>Level roles</h2>
        <span class="hint">Roles stack - hitting a level grants every role at or below it that the member doesn't already have</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Level</th><th>Role</th><th></th></tr></thead>
        <tbody id="lr-rows"></tbody>
      </table></div>
      <button class="btn btn-sm btn-ghost" id="lr-add" style="margin-top:8px;">+ Add level</button>
      <button class="btn btn-gold" id="lr-save" style="margin-top:12px;">Save</button>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>Live member count</h2>
        <span class="hint">Renames a voice channel to show the live non-bot member count. Updates on join/leave and every 10 minutes (Discord rate-limits channel renames, so it can lag slightly on very active servers)</span>
      </div>
      <div class="switch-row" style="margin-bottom:16px;">
        <label class="switch"><input type="checkbox" id="mc-enabled" ${STATE.member_count_enabled ? 'checked' : ''}><span class="track"></span></label>
        <span class="lbl">Enable the counter</span>
      </div>
      <div class="field-grid">
        <div class="field">
          <label>Voice channel</label>
          <select id="mc-channel"><option value="">Loading channels…</option></select>
        </div>
        <div class="field">
          <label>Name template</label>
          <input type="text" id="mc-template" value="${escapeHtml(STATE.member_count_template || '')}" placeholder="Members: {count}">
        </div>
      </div>
      <button class="btn btn-gold" id="mc-save" style="margin-top:12px;">Save</button>
    </div>

    <div class="card">
      <div class="card-head"><h2>Leaderboard preview</h2></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Player</th><th>Level</th><th>Total XP</th></tr></thead>
        <tbody id="lvl-board"><tr><td colspan="3" class="empty">Loading…</td></tr></tbody>
      </table></div>
    </div>`;

  const [channels, roles, board] = await Promise.all([
    apiGet(`/api/guild/${GID}/channels`),
    apiGet(`/api/guild/${GID}/roles`),
    apiGet(`/api/guild/${GID}/level_leaderboard`),
  ]);

  const channelOptions = (selectedId) => pickerOptions(channels ? channels.text : null, selectedId, { prefix: '#' });
  const roleOptions = (selectedId) => pickerOptions(roles, selectedId, { emptyLabel: 'Pick a role…' });

  document.getElementById('w-channel').innerHTML = channelOptions(STATE.welcome_channel_id);
  document.getElementById('l-channel').innerHTML = channelOptions(STATE.level_channel_id);
  document.getElementById('mc-channel').innerHTML = pickerOptions(
    channels ? channels.voice : null, STATE.member_count_channel_id, { prefix: '🔊 ' }
  );

  document.getElementById('w-save').addEventListener('click', async () => {
    const r = await apiPost(`/api/guild/${GID}/welcome_config`, {
      welcome_enabled: document.getElementById('w-enabled').checked,
      welcome_channel_id: document.getElementById('w-channel').value || null,
      welcome_message: document.getElementById('w-message').value,
    });
    if (r) { toast('Saved.', 'ok'); refresh(); }
  });

  document.getElementById('l-save').addEventListener('click', async () => {
    const r = await apiPost(`/api/guild/${GID}/leveling_config`, {
      leveling_enabled: document.getElementById('l-enabled').checked,
      level_channel_id: document.getElementById('l-channel').value || null,
      level_message: document.getElementById('l-message').value,
    });
    if (r) { toast('Saved.', 'ok'); refresh(); }
  });

  document.getElementById('mc-save').addEventListener('click', async () => {
    const r = await apiPost(`/api/guild/${GID}/member_count_config`, {
      member_count_enabled: document.getElementById('mc-enabled').checked,
      member_count_channel_id: document.getElementById('mc-channel').value || null,
      member_count_template: document.getElementById('mc-template').value,
    });
    if (r) { toast('Saved.', 'ok'); refresh(); }
  });

  // ---- level roles table ----
  const rowsEl = document.getElementById('lr-rows');
  const addRow = (level, roleId) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="number" min="0" class="lr-level" value="${level ?? ''}" style="width:80px;"></td>
      <td><select class="lr-role">${roleOptions(roleId)}</select></td>
      <td><button class="btn btn-sm btn-danger lr-remove">Remove</button></td>`;
    tr.querySelector('.lr-remove').addEventListener('click', () => tr.remove());
    rowsEl.appendChild(tr);
  };
  const existing = Object.entries(STATE.level_roles || {}).sort((a, b) => Number(a[0]) - Number(b[0]));
  if (existing.length) {
    existing.forEach(([lvl, roleId]) => addRow(lvl, roleId));
  } else {
    rowsEl.innerHTML = `<tr><td colspan="3" class="empty">No level roles set yet.</td></tr>`;
  }
  document.getElementById('lr-add').addEventListener('click', () => {
    if (rowsEl.querySelector('td.empty')) rowsEl.innerHTML = '';
    addRow('', '');
  });
  document.getElementById('lr-save').addEventListener('click', async () => {
    const level_roles = {};
    rowsEl.querySelectorAll('tr').forEach((tr) => {
      const lvlInput = tr.querySelector('.lr-level');
      const roleSelect = tr.querySelector('.lr-role');
      if (!lvlInput || !roleSelect) return;
      const lvl = lvlInput.value.trim();
      const roleId = roleSelect.value;
      if (lvl !== '' && roleId) level_roles[lvl] = roleId;
    });
    const r = await apiPost(`/api/guild/${GID}/level_roles`, { level_roles });
    if (r) { toast('Saved.', 'ok'); refresh(); }
  });

  // ---- leaderboard preview ----
  const boardEl = document.getElementById('lvl-board');
  if (board && board.length) {
    boardEl.innerHTML = board.map((e) =>
      `<tr><td>${escapeHtml(e.name)}</td><td>${e.level}</td><td>${e.total_xp.toLocaleString()}</td></tr>`
    ).join('');
  } else {
    boardEl.innerHTML = `<tr><td colspan="3" class="empty">Nobody's earned any xp here yet.</td></tr>`;
  }
}

// ----------------------------------------------------------------- bounties
function renderBounties() {
  const el = document.getElementById('tab-bounties');
  if (STATE.is_personal) { el.innerHTML = `<div class="empty">Not applicable here.</div>`; return; }
  const entries = Object.entries(STATE.bounties || {});
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Set a bounty</h2></div>
      <div class="input-row" style="max-width:420px;">
        <input type="text" id="b-uid" placeholder="Player user ID">
        <input type="number" id="b-amount" placeholder="Amount (0 clears)" style="max-width:150px;">
        <button class="btn btn-gold" id="b-set">Set</button>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Active bounties</h2></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Player</th><th class="num">Amount</th><th></th></tr></thead>
        <tbody>${entries.map(([uid, amt]) => {
          const u = STATE.users.find((x) => x.id === uid);
          return `<tr><td>${escapeHtml(u ? u.name : uid)} <span class="pid">${uid}</span></td>
            <td class="num chip">${fmtChips(amt)}</td>
            <td><button class="btn btn-sm btn-danger" data-clear="${uid}">Clear</button></td></tr>`;
        }).join('') || `<tr><td colspan="3" class="empty">No bounties set.</td></tr>`}</tbody>
      </table></div>
    </div>`;

  document.getElementById('b-set').addEventListener('click', async () => {
    const uid = document.getElementById('b-uid').value.trim();
    const amount = Number(document.getElementById('b-amount').value || 0);
    if (!uid) { toast('Enter a user ID.', 'error'); return; }
    const r = await apiPost(`/api/guild/${GID}/bounty`, { user_id: uid, amount });
    if (r) { toast('Bounty updated.', 'ok'); refresh(); }
  });
  el.querySelectorAll('[data-clear]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const r = await apiPost(`/api/guild/${GID}/bounty`, { user_id: btn.dataset.clear, amount: 0 });
      if (r) { toast('Bounty cleared.', 'ok'); refresh(); }
    });
  });
}

// ----------------------------------------------------------------- activity
async function loadActivity(q) {
  const el = document.getElementById('tab-activity');
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Activity log</h2></div>
      <div class="input-row" style="max-width:340px;margin-bottom:14px;">
        <input type="search" id="a-search" placeholder="Search log text…" value="${q ? escapeHtml(q) : ''}">
      </div>
      <div id="a-list"></div>
    </div>`;
  const list = await apiGet(`/api/guild/${GID}/activity` + (q ? `?q=${encodeURIComponent(q)}` : ''));
  const box = document.getElementById('a-list');
  box.innerHTML = (list && list.length)
    ? list.map((e) => `<div class="log-item"><span class="t">${new Date(e.ts * 1000).toLocaleString()}</span><span>${escapeHtml(e.text)}</span></div>`).join('')
    : `<div class="empty">Nothing logged yet.</div>`;
  document.getElementById('a-search').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') loadActivity(ev.target.value.trim());
  });
}

// ------------------------------------------------------------------- danger
function renderDanger() {
  const el = document.getElementById('tab-danger');
  el.innerHTML = `
    <div class="card danger">
      <div class="card-head"><h2>Remove ${STATE.is_personal ? 'nothing' : 'bot from this server'}</h2></div>
      ${STATE.is_personal ? `<p class="hint">Not applicable to the personal/DM ledger.</p>` : `
      <p style="color:var(--ink-dim);font-size:13px;margin:0 0 14px;">This removes ${document.title.split(' — ')[0] || 'the bot'} from the server. Player balances stay on disk in case you reinvite it later, but nobody can play until you do.</p>
      <button class="btn btn-danger" id="leave-btn">Remove bot from this server</button>`}
    </div>`;
  const btn = document.getElementById('leave-btn');
  if (btn) btn.addEventListener('click', async () => {
    if (!confirm(`Really remove the bot from ${STATE.name}? You'll need to re-invite it to manage this server again.`)) return;
    const r = await apiPost(`/api/guild/${GID}/leave`, {});
    if (r) { toast('Left the server.', 'ok'); window.location.href = '/dashboard'; }
  });
}

async function refresh() {
  const prev = STATE.users;
  STATE = await apiGet(`/api/guild/${GID}`);
  if (!STATE) return;
  renderOverview();
  renderPlayerRows();
  renderModeration();
  renderBounties();
}

init();
