function toast(msg, kind) {
  const root = document.getElementById('toast-root');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401 || res.status === 403) {
    if (res.status === 401) { window.location.href = '/login'; return null; }
    toast("You don't have access to do that.", 'error');
    return null;
  }
  let body = null;
  try { body = await res.json(); } catch (e) { /* no body, e.g. CSV download */ }
  if (!res.ok) {
    toast((body && body.error) || 'Something went wrong.', 'error');
    return null;
  }
  return body;
}

const apiGet = (path) => api(path);
const apiPost = (path, data) => api(path, { method: 'POST', body: JSON.stringify(data || {}) });

function fmtChips(n) {
  n = Number(n) || 0;
  return n.toLocaleString('en-US');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
