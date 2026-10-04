// Shared helpers for the Secretary pages (static/secretary/common.js). No DOM access at import time, so
// node can import it in tests.

export const SEC_PAGES = ['today', 'timeline', 'sessions', 'board', 'loops', 'decisions', 'ask', 'hygiene'];
export const TITLES = {
  today: 'Today', timeline: 'Timeline', sessions: 'Sessions', session: 'Session', board: 'Board',
  loops: 'Open loops', decisions: 'Decisions', ask: 'Ask', hygiene: 'Hygiene',
};

// Everything that comes from a transcript, the docs folder or a model goes through esc() before it
// becomes markup. Ask answers go through md(), which escapes first too.
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ---------------------------------------------------------------- routing (shared with the Desk view)

// '' | #desk | #desk-*        -> { view: 'desk' }
// #secretary                   -> today
// #today | #today-2026-10-03   -> { page: 'today', day }
// #s-<id> | #s-<id>-t<turn>    -> { page: 'session', id, turn }   (session ids never contain "-t<digits>" at the end)
// #timeline #sessions #board #loops #decisions #ask #hygiene
// anything else, or a broken % escape -> desk
export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  let h;
  try { h = decodeURIComponent(raw); } catch { return { view: 'desk' }; }
  if (h === '' || h === 'desk' || h.startsWith('desk-')) return { view: 'desk' };
  if (h === 'secretary') return { view: 'secretary', page: 'today', day: null };
  let m = h.match(/^today(?:-(\d{4}-\d{2}-\d{2}))?$/);
  if (m) return { view: 'secretary', page: 'today', day: m[1] || null };
  m = h.match(/^s-(.+?)(?:-t(\d+))?$/);
  if (m) return { view: 'secretary', page: 'session', id: m[1], turn: m[2] ? Number(m[2]) : null };
  if (SEC_PAGES.includes(h)) return { view: 'secretary', page: h };
  return { view: 'desk' };
}

export function hashFor(r) {
  if (r.view === 'desk') return '#desk';
  if (r.page === 'session') return `#s-${r.id}${r.turn ? `-t${r.turn}` : ''}`;
  if (r.page === 'today' && r.day) return `#today-${r.day}`;
  return `#${r.page}`;
}

// ---------------------------------------------------------------- fetch

export async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts });
  if (r.status === 401) { location.reload(); throw new Error('signed out'); }
  if (!r.ok) {
    let msg = r.statusText || `HTTP ${r.status}`;
    try {
      const d = (await r.json()).detail;
      if (typeof d === 'string') msg = d;
      else if (Array.isArray(d) && d[0]?.msg) msg = d[0].msg;   // FastAPI 422
    } catch { /* not json */ }
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return r.status === 204 ? null : r.json();
}
export const post = (path, body) => api(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body ?? {}) });
export const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== '' && v !== 'all')).toString();
export const sid = (id) => encodeURIComponent(id);

export function debounce(fn, ms) {
  let t;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
}

export const store = {
  get(k, d = null) { try { return sessionStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private window */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* private window */ } },
  local(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  setLocal(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
};

export async function copyText(text, toast, done) {
  try { await navigator.clipboard.writeText(text); toast(done || 'Copied.'); }
  catch { toast(`This browser would not copy. Here it is: ${text}`); }
}

// ---------------------------------------------------------------- markup pieces (prototype classes)

export const chip = (t) => `<span class="chip" title="${esc(t)}">${esc(t)}</span>`;
export const pill = (cls, t) => `<span class="pill ${cls}">${esc(t)}</span>`;

export function head(eyebrow, title, extra = '') {
  return `<div class="sec-head"><p class="eyebrow">${eyebrow}</p><h1 class="h1">${esc(title)}</h1>${extra}</div>`;
}

// A source link: the same shape everywhere ({kind, label, href, path, line}). Only three kinds of href
// become links: an in-page hash, http(s), and vscode://file. Anything else is plain text.
export function srcHtml(src) {
  if (!src || !src.label) return '';
  const label = esc(src.label);
  const h = String(src.href || '');
  if (/^#[\w-]+$/.test(h)) return `<a class="src" href="${esc(h)}">${label}</a>`;
  if (/^(https?:\/\/|vscode:\/\/file\/)/i.test(h)) {
    return `<a class="src" href="${esc(h)}" target="_blank" rel="noopener noreferrer" title="${esc(src.path || h)}">${label}</a>`;
  }
  return `<span class="src" aria-disabled="true"${src.path ? ` title="${esc(src.path)}"` : ''}>${label}</span>`;
}

export function outcomePill(o) {
  return {
    shipped: pill('p-ok', 'shipped'), pushed: pill('p-warn', 'pushed, not deployed'), in_progress: pill('p-info', 'in progress'),
    explored: pill('p-mute', 'explored'), blocked: pill('p-bad', 'blocked'),
  }[o] || '';
}
export function shipPill(s) {
  return {
    deployed: pill('p-ok', 'deployed'), pushed: pill('p-warn', 'pushed, not deployed'), merged: pill('p-info', 'merged'),
    committed: pill('p-info', 'committed'), built: pill('p-info', 'built'), shipped: pill('p-ok', 'shipped'),
  }[s] || '';
}
export const levelPill = (l) => ({ warn: pill('p-warn', 'Check'), ok: pill('p-ok', 'OK'), info: pill('p-info', 'Info') }[l] || '');

// Search snippets carry U+0002 / U+0003 around matches: escape first, then mark.
export const snippetHtml = (s) => esc(s).replace(/\u0002/g, '<mark>').replace(/\u0003/g, '</mark>').replace(/[\u0002\u0003]/g, '');

// Find in a session: case-insensitive substring over the escaped text, like the prototype.
export function highlight(text, q) {
  const e = esc(text);
  if (!q) return e;
  const re = new RegExp(`(${esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'ig');
  return e.replace(re, '<mark>$1</mark>');
}

// ---------------------------------------------------------------- markdown subset for Ask answers

// Escape first, then add a few tags. Supports paragraphs, line breaks, "-"/"*" and "1." lists, ### headings,
// ``` code blocks, **bold**, *italic*, `code` and [text](https://…) links. Nothing else becomes markup,
// so a model that writes <script> or an onerror attribute only ever shows that text.
export function md(src) {
  const inline = (s) => {
    const codes = [];
    let t = esc(String(s).replace(/\u0000/g, '')).replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`)
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/(^|[\s(])\*([^*\s][^*\n]*?)\*(?=[\s).,;:!?]|$)/g, '$1<i>$2</i>');
    return t.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
  };
  const out = [];
  const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n');
  let para = [];
  // Open lists, outermost first: {tag, indent, start, items: [{text, kids: [html]}]}. An indented item opens a
  // list inside the item above it; an ordered list that resumes after one keeps its number (start="3").
  let stack = [];
  const flushPara = () => { if (para.length) out.push(`<p>${para.map(inline).join('<br>')}</p>`); para = []; };
  const listHtml = (l) => `<${l.tag}${l.tag === 'ol' && l.start !== 1 ? ` start="${l.start}"` : ''}>${l.items.map((x) => `<li>${inline(x.text)}${x.kids.join('')}</li>`).join('')}</${l.tag}>`;
  const closeTop = () => {
    const l = stack.pop();
    const parent = stack[stack.length - 1];
    if (parent && parent.items.length) parent.items[parent.items.length - 1].kids.push(listHtml(l));
    else out.push(listHtml(l));
  };
  const flushList = () => { while (stack.length) closeTop(); };
  let blank = false;  // a blank line inside a list: the list goes on only if an item or an indented line follows
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flushPara(); flushList();
      const code = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
      out.push(`<pre class="code">${esc(code.join('\n'))}</pre>`);
      continue;
    }
    if (!line.trim()) { flushPara(); if (stack.length) blank = true; continue; }
    const h = line.match(/^\s{0,3}#{1,6}\s+(.*)$/);
    if (h) { flushPara(); flushList(); blank = false; out.push(`<h4>${inline(h[1])}</h4>`); continue; }
    const li = line.match(/^(\s*)(?:([-*•])|(\d+)[.)])\s+(.*)$/);
    if (li) {
      flushPara();
      const indent = li[1].replace(/\t/g, '    ').length;
      const tag = li[2] ? 'ul' : 'ol';
      // a blank line, then a new list of another kind at the outer level: a separate list, not a continuation
      if (blank && stack.length && indent <= stack[0].indent && stack[0].tag !== tag) flushList();
      blank = false;
      while (stack.length && indent < stack[stack.length - 1].indent) closeTop();
      let top = stack[stack.length - 1];
      if (top && indent > top.indent && top.items.length && stack.length < 4) {
        top = null;  // deeper: a list inside the item above
      } else if (top && top.tag !== tag) {
        closeTop();
        top = stack[stack.length - 1];
        if (top && (indent > top.indent || top.tag !== tag)) top = null;
      }
      if (!top) {
        top = { tag, indent, start: li[3] ? Math.min(Number(li[3]), 1e6) : 1, items: [] };
        stack.push(top);
      }
      top.items.push({ text: li[4], kids: [] });
      continue;
    }
    if (stack.length && /^\s{2,}\S/.test(line)) {  // an indented line carries on the item above
      const top = stack[stack.length - 1];
      top.items[top.items.length - 1].text += ` ${line.trim()}`;
      blank = false;
      continue;
    }
    flushList(); blank = false;
    para.push(line);
  }
  flushPara(); flushList();
  return out.join('');
}

// ---------------------------------------------------------------- time

export function fmtTime(ts, tz) {
  try {
    return new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: tz || undefined }).format(new Date(ts * 1000));
  } catch {
    return new Date(ts * 1000).toTimeString().slice(0, 5);
  }
}
export const weekday = (day) => new Date(`${day}T12:00:00Z`).toLocaleDateString('en-GB', { weekday: 'long', timeZone: 'UTC' });
export const shortDay = (day) => new Date(`${day}T12:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });

// ---------------------------------------------------------------- states

export function emptyNote(text) { return `<p class="empty-line">${esc(text)}</p>`; }

// A designed empty state: what is missing, why, and the one command that fixes it (optional).
export function emptyState({ title, text, code = '', glyph = '·', warn = false, html = '' }) {
  return `<div class="empty${warn ? ' warn' : ''}"><span class="glyph" aria-hidden="true">${esc(glyph)}</span><h2>${esc(title)}</h2>`
    + `<p>${text}</p>${code ? `<pre class="code">${esc(code)}</pre>` : ''}${html}</div>`;
}

export function skeleton() {
  return '<div class="sk" aria-busy="true"><span class="vh" role="status">Loading…</span><i class="e"></i><i class="h"></i><i></i><i></i><i></i><div class="skb"></div></div>';
}

export function errorNote(err) {
  return `<div class="empty warn"><span class="glyph" aria-hidden="true">!</span><h2>Could not load this page</h2><p>${esc(err?.message || err)}.</p>`
    + '<button class="btn sm" type="button" data-sec-retry>Try again</button></div>';
}

// What the docs folder gives. Pages that need part of it explain what is missing instead of looking broken.
export function docs(state) {
  return state?.sources?.docs || { set: false, found: false, label: null, kind: 'none', changelog: false, design: false };
}

export function noDocsState(state, part) {
  const d = docs(state);
  const what = part === 'design'
    ? 'a <code>design/</code> folder (one folder per feature, with a README index and decision tables)'
    : 'a <code>changelog/</code> folder (one file per repo per day)';
  if (!d.set) {
    return emptyState({
      title: 'No docs folder is set',
      text: `This page reads ${what} from a docs folder, and none is configured. Sessions, Timeline and Ask work without one. Pick a folder in the setup wizard's Secretary step, or set it directly:`,
      code: './deskmate config set DOCS_DIR ~/path/to/docs\n./deskmate up',
      glyph: '?',
    });
  }
  if (!d.found) {
    return emptyState({
      title: 'The docs folder cannot be read',
      text: `The docs folder ${esc(d.label || '')} is set but the hub cannot see it. Check that it exists, then restart:`,
      code: './deskmate doctor\n./deskmate up',
      glyph: '!', warn: true,
    });
  }
  return emptyState({
    title: part === 'design' ? 'No design folder yet' : 'No changelog folder yet',
    text: `${esc(d.label || 'The docs folder')} has no ${what}. The working-habits pack can create one: <code>./deskmate habits apply</code>.`,
  });
}

export const DIGEST_STATE_NOTE = {
  none: 'no digest yet', queued: 'digest queued', running: 'writing the digest…', ready: 'digest ready', skipped: 'skipped',
  paused: 'digest paused', capped: 'daily cap reached', no_token: 'no Claude login', failed: 'digest failed',
};

// Can the user ask for a digest of this session now? (no login or secretary off: no)
export function canWriteDigest(row, state) {
  const st = state?.secretary?.status;
  if (!state?.secretary?.token || st === 'off' || st === 'no_token') return false;
  return ['none', 'failed', 'skipped'].includes(row.digest_state) || (row.digest_state === 'ready' && row.digest_stale);
}

// Queue a digest for one session (older sessions are not digested on their own; §13 "first run").
export async function writeDigest(id, ctx) {
  try {
    const r = await post(`/api/sec/sessions/${sid(id)}/digest`, {});
    if (r && r.ok === false) { ctx.toast(`Nothing new to digest (${DIGEST_STATE_NOTE[r.digest_state] || r.digest_state}).`); return false; }
    ctx.toast({
      capped: 'The daily cap is reached. It waits for tomorrow.',
      paused: 'Queued. The secretary is paused, so it waits until you resume.',
    }[r?.digest_state] || 'Queued. The digest appears here in a minute or two.');
    return true;
  } catch (err) { ctx.toast(err.message); return false; }
}

// The digest's four lists, as the Timeline and the session side show them.
export function bullets(a, none = 'None') {
  return a?.length ? `<ul>${a.map((x) => `<li>${esc(typeof x === 'string' ? x : x.text)}</li>`).join('')}</ul>` : `<p class="dim">${esc(none)}</p>`;
}

// "No digest yet", plus why when there is a reason ("no Claude login", "daily cap reached", ...).
export const noDigest = (row) => (['none', 'ready'].includes(row.digest_state) ? 'No digest yet.' : `No digest yet · ${row.digest_label}`);
