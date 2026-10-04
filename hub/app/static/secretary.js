// Secretary view shell (static/secretary.js). It owns the URL-hash routing for BOTH views (Desk and
// Secretary), the plan meter, the secretary state card, the sub-nav counts and the secretary's live events.
// Each page lives in static/secretary/<page>.js and exports show(el, route, ctx); it renders the whole
// <article id="sec-<page>">. app.js knows nothing about this file: if it fails to load, the Desk still works.
import {
  api, post, parseRoute, hashFor, SEC_PAGES, TITLES, errorNote, skeleton, emptyState, head, store,
} from './secretary/common.js';

const $ = (s, r = document) => r.querySelector(s);
const ctx = { state: null, missing: false, refreshState, toast };
let current = null;           // the route being shown
let pageSeq = 0;              // drops responses of pages the user already left
let lastMode = null;          // see refreshState(): re-render when the secretary's mode flips

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { t.hidden = true; }, 3600);
}

// ---------------------------------------------------------------- views and routing

function showView(view) {
  for (const v of ['desk', 'secretary']) {
    $(`#view-${v}`).hidden = v !== view;
    $(`#tab-${v}`)?.setAttribute('aria-selected', String(v === view));
  }
  // noVNC sizes its canvas from its container, and a hidden container has no size.
  if (view === 'desk') requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
}

function markNav(page) {
  const navPage = page === 'session' ? 'sessions' : page;
  const nav = $('.subnav');
  document.querySelectorAll('.subnav a').forEach((a) => {
    if (a.dataset.sec !== navPage) { a.removeAttribute('aria-current'); return; }
    a.setAttribute('aria-current', 'page');
    // On a phone the sub-nav scrolls sideways: keep the current page in view.
    if (nav.scrollWidth > nav.clientWidth) nav.scrollLeft = a.offsetLeft - nav.offsetLeft - (nav.clientWidth - a.offsetWidth) / 2;
  });
}

// States that replace every page: the hub has no secretary, or it is switched off.
function wholeViewState(page) {
  if (ctx.missing) {
    return head('Secretary', TITLES[page]) + emptyState({
      title: 'This hub has no secretary',
      text: 'The hub answered, but it has no Secretary pages. Update Deskmate and start it again:',
      code: './deskmate update',
      glyph: '?',
    });
  }
  if (ctx.state?.secretary.status === 'off') {
    return head('Secretary', TITLES[page]) + emptyState({
      title: 'The secretary is off',
      text: 'It reads your Claude Code sessions and your docs folder and writes a digest of each session, a daily brief and answers to your questions. While it is off it reads nothing and calls no model. To turn it on:',
      code: './deskmate config set SECRETARY on\n./deskmate up',
      glyph: '○',
    });
  }
  return '';
}

function renderBanner() {
  const b = $('#secBanner');
  const s = ctx.state;
  const on = s && !s.first_sweep_done && s.secretary.status !== 'off';
  b.hidden = !on;
  if (on) b.innerHTML = '<span class="live" aria-hidden="true"></span><span>The secretary is reading your sessions and docs for the first time. This page fills in within a minute.</span>';
}

async function route() {
  const r = parseRoute(location.hash);
  showView(r.view);
  if (r.view === 'desk') { document.title = 'Deskmate'; current = r; return; }
  store.set('deskmate.secHash', hashFor(r));
  const samePage = current && current.view === 'secretary' && current.page === r.page && current.id === r.id && current.day === r.day;
  current = r;
  document.title = `Deskmate · ${TITLES[r.page]}`;
  markNav(r.page);
  for (const p of [...SEC_PAGES, 'session']) $(`#sec-${p}`).hidden = p !== r.page;
  if (!samePage) window.scrollTo(0, 0);
  renderBanner();
  const el = $(`#sec-${r.page}`);
  const seq = ++pageSeq;
  if (!ctx.state && !ctx.missing) await refreshState();
  if (seq !== pageSeq) return;
  const whole = wholeViewState(r.page);
  if (whole) { el.innerHTML = whole; return; }
  if (!samePage) el.innerHTML = '';
  // A designed loading state, only when the page is slow (most answer within 150 ms).
  const slow = setTimeout(() => { if (seq === pageSeq && !el.childElementCount) el.innerHTML = skeleton(); }, 150);
  try {
    const mod = await import(`./secretary/${r.page}.js`);
    if (seq !== pageSeq) return;
    const pageCtx = {
      get state() { return ctx.state; },
      refreshState, toast, alive: () => seq === pageSeq,
    };
    await mod.show(el, r, pageCtx);
  } catch (err) {
    if (seq === pageSeq) el.innerHTML = head('Secretary', TITLES[r.page]) + errorNote(err);
  } finally {
    clearTimeout(slow);
  }
}

document.addEventListener('click', (e) => {
  const tab = e.target.closest('.views button[data-view]');
  if (tab) {
    const last = store.get('deskmate.secHash', '#today');
    const want = tab.dataset.view === 'secretary' ? last : '#desk';
    if (location.hash === want) route(); else location.hash = want;
    return;
  }
  if (e.target.closest('[data-sec-retry]')) { route(); return; }
  if (e.target.closest('#secPause')) togglePause();
});
// Arrow keys move between the two tabs, as a tablist should.
document.querySelector('.views')?.addEventListener('keydown', (e) => {
  if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
  const tabs = [...document.querySelectorAll('.views button[data-view]')];
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
  next.focus();
  next.click();
});
window.addEventListener('hashchange', route);

// ---------------------------------------------------------------- state card, counts, meter

async function refreshState() {
  try {
    ctx.state = await api('/api/sec/state');
    ctx.missing = false;
    renderState();
  } catch (err) {
    if (err.status === 404) { ctx.missing = true; ctx.state = null; renderMissing(); }
    /* otherwise the next event or tick retries */
  }
  // Re-render the page when something that changes every page flips: the first sweep finished, the
  // secretary was switched on or off, the Claude login was added or removed, or the hub gained its secretary.
  const mode = ctx.missing ? 'missing' : ctx.state ? `${ctx.state.first_sweep_done}|${ctx.state.secretary.status === 'off'}|${ctx.state.secretary.token}` : null;
  if (lastMode !== null && mode !== null && mode !== lastMode && current?.view === 'secretary') route();
  if (mode !== null) lastMode = mode;
  return ctx.state;
}

function renderMissing() {
  $('#secState').hidden = true;
  $('#planMeter').hidden = true;
  for (const id of ['#sessCount', '#loopCount', '#hygCount']) $(id).textContent = '';
}

function renderState() {
  const s = ctx.state;
  const sec = s.secretary;
  const off = sec.status === 'off';
  $('#secState').hidden = false;
  $('#secOn').textContent = sec.label;
  $('#secDigests').textContent = `${s.caps.digests_today} of ${s.caps.digests_max}`;
  $('#secPauseAt').textContent = `${Math.round(s.caps.pause_at * 100)}% of 5 h`;
  $('#secPauseAt').title = s.usage?.pause_at_week ? `or ${Math.round(s.usage.pause_at_week * 100)}% of the 7-day window` : '';
  $('#secModel').textContent = s.models.digest_label;
  $('#secQueued').textContent = String(sec.queued);
  $('#secQueuedRow').hidden = !sec.queued || off;
  $('#secNoToken').hidden = sec.token || off;
  $('#secOffNote').hidden = !off;
  for (const id of ['#secDigests', '#secPauseAt', '#secModel']) $(id).closest('.row').hidden = off;
  $('#secPause').hidden = off || !sec.token;
  $('#secPause').textContent = sec.paused_by_user ? 'Resume secretary' : 'Pause secretary';
  const count = (id, n, warn) => { const el = $(id); el.textContent = n ? String(n) : ''; el.classList.toggle('warn', Boolean(warn && n)); };
  count('#sessCount', s.counts.sessions);
  count('#loopCount', s.counts.loops_open);
  count('#hygCount', s.counts.hygiene_warn, true);
  renderMeter(s.usage, off);
  renderBanner();
}

function renderMeter(u, off) {
  const m = $('#planMeter');
  m.hidden = Boolean(off || !u || u.error === 'off');
  if (m.hidden) return;
  const five = u.five_hour, seven = u.seven_day;
  const bar = m.querySelector('.bar i');
  m.classList.remove('over', 'stale', 'off');
  if (!u.ok || !five) {
    bar.style.width = '0%';
    m.classList.add('off');
    $('#meter5').textContent = u.error === 'no_token' ? 'no Claude login' : u.error === 'not_yet' ? 'usage not read yet' : 'usage unknown';
    $('#meter7').textContent = '';
    m.title = u.error === 'no_token'
      ? 'The secretary has no Claude login, so it cannot read your plan usage.'
      : 'Plan usage. The secretary reads it at most once a minute, before it calls a model.';
    return;
  }
  const pct = (w) => `${Math.round(Math.max(0, w.utilization) * 100)}%`;
  // The bar shows the 5-hour window, or the 7-day one when that is the one holding the secretary back.
  const weekHolds = Boolean(u.over_pause && seven && seven.utilization >= u.pause_at_week && five.utilization < u.pause_at);
  const shown = weekHolds ? seven : five;
  bar.style.width = `${Math.min(100, Math.max(0, Math.round(shown.utilization * 100)))}%`;
  m.classList.toggle('over', Boolean(u.over_pause));
  m.classList.toggle('stale', Boolean(u.stale));
  $('#meter5').textContent = `5 h ${pct(five)}`;
  $('#meter7').textContent = seven ? `· 7 d ${pct(seven)}` : '';
  const tz = ctx.state?.tz;
  const at = (w) => {
    if (!w?.resets_at) return null;
    if (w.resets_at * 1000 < Date.now()) return 'already';
    try { return new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', weekday: w === seven ? 'short' : undefined, timeZone: tz || undefined }).format(new Date(w.resets_at * 1000)); } catch { return null; }
  };
  const r5 = at(five);
  m.title = 'Plan usage, read the same way Claude Code\'s /usage does.'
    + (r5 === 'already' ? ' The 5-hour window has reset since the last reading.' : r5 ? ` The 5-hour window resets at ${r5}.` : '')
    + (seven && at(seven) && at(seven) !== 'already' ? ` The 7-day window resets ${at(seven)}.` : '')
    + (u.over_pause ? ` Digests and the brief are paused. ${u.reason || ''} Ask still works.` : ` Digests and the brief pause at ${Math.round(u.pause_at * 100)}% of 5 h${u.pause_at_week ? ` or ${Math.round(u.pause_at_week * 100)}% of 7 d` : ''}.`)
    + (u.stale ? ' This reading is more than 5 minutes old.' : '');
}

async function togglePause() {
  const paused = !ctx.state?.secretary.paused_by_user;
  try {
    await post('/api/sec/pause', { paused });
    toast(paused ? 'Secretary paused. Finished sessions queue until you resume.' : 'Secretary resumed. Queued sessions are processed now.');
    await refreshState();
  } catch (err) { toast(err.message); }
}

async function refreshUsage() {
  if (document.visibilityState !== 'visible' || !ctx.state || ctx.state.secretary.status === 'off') return;
  try {
    const u = await api('/api/sec/usage?refresh=1');
    if (ctx.state) { ctx.state.usage = u; renderMeter(u, false); }
  } catch { /* keep the old meter */ }
}

// ---------------------------------------------------------------- live events

// The secretary's events ride the Desk's event stream (app.js puts it on window.deskmateEvents), so a tab
// holds one stream, not two. If app.js has not opened it within a few seconds, open our own.
function attach(es) {
  let t;
  const soonState = () => { clearTimeout(t); t = setTimeout(refreshState, 400); };
  const parse = (e) => { try { return JSON.parse(e.data); } catch { return null; } };
  es.addEventListener('sec_state', soonState);
  es.addEventListener('sec_usage', (e) => {
    const u = parse(e);
    if (!u || !ctx.state) return;
    const flipped = Boolean(u.over_pause) !== Boolean(ctx.state.usage?.over_pause);
    ctx.state.usage = u;
    renderMeter(u, false);
    if (flipped) soonState();   // the status card says "Paused at …" (or no longer does)
  });
  for (const kind of ['sec_session', 'sec_today', 'sec_ask', 'sec_brief', 'sec_loops']) {
    es.addEventListener(kind, (e) => document.dispatchEvent(new CustomEvent(kind, { detail: parse(e) })));
  }
  es.addEventListener('sec_loops', soonState);
  es.addEventListener('open', soonState);
  if (es.readyState === 1) soonState();
}

function listen() {
  if (window.deskmateEvents) { attach(window.deskmateEvents); return; }
  let done = false;
  const use = (es) => { if (!done) { done = true; attach(es); } };
  window.addEventListener('deskmate:events', () => use(window.deskmateEvents), { once: true });
  setTimeout(() => use(new EventSource('/api/events')), 4000);
}

// ---------------------------------------------------------------- boot

route();
listen();
setInterval(refreshState, 60000);
setInterval(refreshUsage, 60000);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshUsage(); });
