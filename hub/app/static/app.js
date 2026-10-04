// Deskmate hub UI: the live desk, who is using it, and what they did.
import RFB from '/novnc/core/rfb.js';

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const COLORS = ['#2D58D2', '#8B3CC4', '#0D817E', '#B8336A', '#B26B00', '#3A7D2C', '#5A55D6', '#B23824'];
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
};

let state = null;
let rfb = null;
let monitor = store.get('deskmate.monitor', '1');
let feedFilter = 'all';

// ---------------------------------------------------------------- helpers

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { t.hidden = true; }, 3200);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts });
  if (r.status === 401) { location.reload(); throw new Error('signed out'); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
}
const post = (path, body) => api(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body ?? {}) });

function session(sid) { return state?.sessions.find((s) => s.id === sid); }
function colorOf(sid) {
  if (sid === 'human') return 'var(--you)';
  const s = session(sid);
  return s ? COLORS[(s.number - 1) % COLORS.length] : 'var(--faint)';
}
function letterOf(sid) { return sid === 'human' ? 'you' : session(sid)?.letter ?? '?'; }
function nameOf(sid) { return sid === 'human' ? 'you' : session(sid)?.display ?? 'a session'; }
const hhmmss = (ts) => new Date(ts * 1000).toTimeString().slice(0, 8);
function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

// ---------------------------------------------------------------- state

async function refresh() {
  try {
    state = await api('/api/state');
    render();
  } catch (e) { /* the event stream will retry */ }
}
let refreshTimer;
function soon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 250); }

function render() {
  renderHolder();
  renderSessions();
  renderFilters();
  renderFeed();
  renderKnocks();
  renderMonitorBar();
  layoutScreen();
  const human = state.lease.human;
  $('#btnTake').textContent = human ? 'Hand back' : 'Take over';
  $('#btnPause').textContent = state.lease.paused ? 'Resume agents' : 'Pause agents';
  if (rfb) rfb.viewOnly = !human;
  $('#viewNote').textContent = human ? 'You have the mouse and keyboard. Agents wait.' : 'View only. Press Take over to use the mouse and keyboard.';
  const m = state.monitors;
  $('#deskInfo').textContent = `${m.count} monitor${m.count > 1 ? 's' : ''} · ${m.width}×${m.height} each`;
}

function renderHolder() {
  const h = $('#holder'), t = $('#holderText'), i = $('#holder i');
  const l = state.lease;
  h.className = 'holder';
  i.style.background = '';
  if (l.human) { h.classList.add('you'); t.textContent = 'You have the desk'; i.style.background = 'var(--you)'; }
  else if (l.paused) { h.classList.add('paused'); t.textContent = 'Agents paused'; i.style.background = 'var(--bad)'; }
  else if (l.holder) { t.textContent = `Input: ${l.label} holds it`; i.style.background = colorOf(l.holder); }
  else t.textContent = 'Input: free';
  const last = state.activity[0];
  $('#nowText').innerHTML = last
    ? `<i class="sd" style="background:${colorOf(last.session)}"></i>${esc(letterOf(last.session))} · <b>${esc(last.tool)}</b> ${esc(last.why || last.arg || '')}`
    : 'Waiting for a session';
}

function renderSessions() {
  const ul = $('#sessions');
  if (!state.sessions.length) { ul.innerHTML = '<li class="dim">No session has used the desk yet.</li>'; return; }
  const knocking = new Set(state.knocks.map((k) => k.session));
  ul.innerHTML = state.sessions.slice().reverse().map((s) => {
    const tabs = state.tabs.filter((t) => t.owner === s.id).length;
    let st = ago(s.last_seen);
    if (knocking.has(s.id)) st = '<span class="pill p-knock">waiting on you</span>';
    else if (state.lease.holder === s.id) st = '<span class="pill p-info">holds input</span>';
    const where = s.cwd ? s.cwd.replace(/^\/(?:home|Users)\/[^/]+/, '~') : 'folder not known yet';
    return `<li><span class="dot" style="background:${colorOf(s.id)}"></span><span class="nm">${esc(s.letter)} · ${esc(s.display)}</span><span class="st">${st}<br>${tabs} tab${tabs === 1 ? '' : 's'}</span><span class="cw">${esc(where)}</span></li>`;
  }).join('');
}

function renderFilters() {
  const seen = [...new Set(state.activity.map((a) => a.session).filter(Boolean))];
  const btn = (f, label, color) => `<button aria-pressed="${feedFilter === f}" data-f="${esc(f)}">${color ? `<i style="background:${color}"></i>` : ''}${esc(label)}</button>`;
  $('#filters').innerHTML = btn('all', 'All') + seen.map((sid) => btn(sid, letterOf(sid), colorOf(sid))).join('');
}

function feedItem(a, fresh) {
  const pill = a.status && a.status !== 'ok' ? ` <span class="pill p-${esc(a.status)}">${esc(a.status === 'knock' ? 'knocking' : a.status)}</span>` : '';
  return `<li class="fi${fresh ? ' new' : ''}" data-s="${esc(a.session)}"><span class="ft mono">${hhmmss(a.ts)}</span><span class="fd" style="background:${colorOf(a.session)}"></span><div><div><b class="mono">${esc(a.tool)}</b> <span class="fa mono">${esc(a.arg)}</span>${pill}</div>${a.why ? `<div class="fw">${esc(a.why)}</div>` : ''}${a.note ? `<div class="fn">${esc(a.note)}</div>` : ''}</div></li>`;
}

function renderFeed() {
  const items = state.activity.filter((a) => feedFilter === 'all' || a.session === feedFilter);
  $('#feed').innerHTML = items.map((a) => feedItem(a, false)).join('') || '<li class="dim">Nothing yet.</li>';
}

function renderKnocks() {
  $('#knocks').innerHTML = state.knocks.map((k) => `
    <div class="knockbar" data-k="${k.id}">
      <span class="kicon" aria-hidden="true">!</span>
      <h3><span class="sd" style="background:${colorOf(k.session)}"></span>${esc(k.label)} needs you</h3>
      <p>${esc(k.question)}</p>
      <div><div class="krow">
        <input type="text" id="knock-${k.id}" placeholder="Reply to the session (optional)" aria-label="Reply">
        <button class="btn" data-act="take">Take over</button>
        <button class="btn knock" data-act="resume">Resume</button>
      </div><p class="note" style="margin-top:6px">Knocked at ${hhmmss(k.ts).slice(0, 5)}. If notifications are on, they went out then.</p></div>
    </div>`).join('');
  const ring = $('#ring');
  const knocking = state.knocks.length > 0;
  ring.hidden = !(state.lease.human || knocking);
  ring.classList.toggle('knocking', knocking && !state.lease.human);
  $('#ringText').textContent = state.lease.human ? 'You have the desk. Agents are waiting.' : knocking ? `${state.knocks[0].label} is knocking` : '';
}

// ---------------------------------------------------------------- live view

function renderMonitorBar() {
  const n = state.monitors.count;
  if (n < 2) { $('#monSeg').innerHTML = ''; monitor = '1'; return; }
  const opts = [...Array(n).keys()].map((i) => String(i + 1)).concat(['all']);
  if (!opts.includes(monitor)) monitor = '1';
  $('#monSeg').innerHTML = opts.map((o) => `<button data-mon="${o}" aria-pressed="${o === monitor}">${o === 'all' ? 'Both' : `Monitor ${o}`}</button>`).join('');
}

function layoutScreen() {
  const { count, width, height } = state.monitors;
  const vp = $('#viewport'), host = $('#rfbHost');
  if (monitor === 'all' || count < 2) {
    vp.style.aspectRatio = `${width * count} / ${height}`;
    host.style.width = '100%';
    host.style.left = '0';
  } else {
    // One noVNC canvas for the whole screen, drawn twice as wide and shifted: the viewport shows one
    // monitor, and noVNC still maps the pointer correctly because it reads the canvas's real position.
    vp.style.aspectRatio = `${width} / ${height}`;
    host.style.width = `${count * 100}%`;
    host.style.left = `${-(Number(monitor) - 1) * 100}%`;
  }
}

function connectVNC() {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/vnc`;
  rfb = new RFB($('#rfbHost'), url, { wsProtocols: ['binary'], shared: true });
  rfb.viewOnly = !(state?.lease.human);
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.background = '#0A0F15';
  rfb.qualityLevel = 7;
  rfb.compressionLevel = 2;
  rfb.addEventListener('connect', () => { $('#screenMsg').hidden = true; });
  rfb.addEventListener('disconnect', () => {
    $('#screenMsg').hidden = false;
    $('#screenMsg').textContent = 'The live view dropped. Reconnecting…';
    rfb = null;
    setTimeout(connectVNC, 2000);
  });
}

// ---------------------------------------------------------------- events

function listen() {
  const es = new EventSource('/api/events');
  // One event stream per tab: secretary.js listens on this one too (browsers allow only 6 connections per
  // host over HTTP/1.1, and every stream holds one open for good).
  window.deskmateEvents = es;
  window.dispatchEvent(new Event('deskmate:events'));
  const conn = $('#conn');
  es.onopen = () => { conn.className = 'conn ok'; conn.lastElementChild.textContent = 'live'; refresh(); };
  es.onerror = () => { conn.className = 'conn bad'; conn.lastElementChild.textContent = 'reconnecting'; };
  es.addEventListener('activity', (e) => {
    const a = JSON.parse(e.data);
    if (!state) return;
    state.activity.unshift(a);
    state.activity.length = Math.min(state.activity.length, 120);
    if (!session(a.session) && a.session !== 'human') soon();
    renderHolder();
    renderFilters();
    if (feedFilter === 'all' || a.session === feedFilter) {
      $('#feed .dim')?.remove();
      $('#feed').insertAdjacentHTML('afterbegin', feedItem(a, true));
    }
    if (['desk_files', 'browser_act', 'upload'].includes(a.tool)) loadDownloads();
  });
  for (const kind of ['lease', 'sessions', 'tabs', 'knocks']) es.addEventListener(kind, soon);
}

async function loadDownloads() {
  try {
    const files = await api('/api/downloads');
    $('#files').innerHTML = files.length
      ? files.slice(0, 12).map((f) => `<li><span class="fnm" title="${esc(f.name)}">${esc(f.name)}</span><span class="dim mono" style="font-size:12px">${(f.size / 1024).toFixed(f.size < 10240 ? 1 : 0)} KB</span><a class="btn sm" href="/api/downloads/${encodeURIComponent(f.name)}" download="${esc(f.name)}">Save</a></li>`).join('')
      : '<li class="dim">Nothing downloaded yet.</li>';
  } catch { /* desk not up yet */ }
}

// ---------------------------------------------------------------- controls

document.addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  try {
    if (b.id === 'btnTake') {
      await post(state.lease.human ? '/api/handback' : '/api/takeover');
      await refresh();
      if (state.lease.human) rfb?.focus();
    } else if (b.id === 'btnPause') {
      await post('/api/pause', { paused: !state.lease.paused });
      await refresh();
    } else if (b.id === 'btnClipTo') {
      let text;
      try { text = await navigator.clipboard.readText(); } catch { toast('This browser would not share its clipboard. Paste on the desk after Take over instead.'); return; }
      await post('/api/clipboard', { text });
      toast('Your clipboard is on the desk now.');
    } else if (b.id === 'btnClipFrom') {
      const { text } = await api('/api/clipboard');
      try { await navigator.clipboard.writeText(text); toast('Copied the desk’s clipboard.'); } catch { toast(text ? `Desk clipboard: ${text.slice(0, 120)}` : 'The desk’s clipboard is empty.'); }
    } else if (b.id === 'btnRestart') {
      await post('/api/restart-browser');
      toast('Chromium is restarting. Each session gets a new tab on its next call.');
    } else if (b.dataset.mon) {
      monitor = b.dataset.mon;
      store.set('deskmate.monitor', monitor);
      renderMonitorBar();
      layoutScreen();
    } else if (b.dataset.f) {
      feedFilter = b.dataset.f;
      renderFilters();
      renderFeed();
    } else if (b.dataset.act) {
      const id = b.closest('[data-k]').dataset.k;
      if (b.dataset.act === 'take') {
        await post('/api/takeover');
        await refresh();
        rfb?.focus();
      } else {
        await post(`/api/knock/${id}`, { text: $(`#knock-${id}`).value });
        if (state.lease.human) await post('/api/handback');
        await refresh();
      }
    }
    // The Desk / Secretary tabs are routed by secretary.js through the URL hash.
  } catch (err) {
    toast(err.message);
  }
});

$('#upload').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    await api(`/api/upload?name=${encodeURIComponent(f.name)}`, { method: 'POST', body: f });
    toast(`${f.name} is in ~/Uploads on the desk.`);
  } catch (err) { toast(err.message); }
  e.target.value = '';
});

// ---------------------------------------------------------------- boot

await refresh();
connectVNC();
listen();
loadDownloads();
setInterval(loadDownloads, 15000);
setInterval(() => state && renderSessions(), 10000);
