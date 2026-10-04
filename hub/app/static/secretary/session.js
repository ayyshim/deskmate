// One session (#s-<id>, #s-<id>-t<turn>): the conversation (prompts, answers, and the steps in between folded
// into one line each), find in the session, and a side column with the digest, files, commands and what the
// session published. Items come in pages; a live session gets new items as the hub ingests them.
import {
  api, esc, chip, pill, srcHtml, outcomePill, head, emptyState, emptyNote, highlight, debounce, store, copyText,
  bullets, canWriteDigest, writeDigest, sid, snippetHtml, noDigest,
} from './common.js';

const PAGE = 500;
const CLIP = 520;        // characters before a message is clipped behind "Show all"
let S = null;            // the session being shown: {id, el, ctx, meta, digest, side, sources, items[], next, mode, q}

document.addEventListener('sec_session', (e) => {
  if (!S || S.el.hidden || !S.ctx.alive() || e.detail?.id !== S.id) return;
  live().catch(() => { /* the next event retries */ });
});

const MODES = [['msgs', 'Messages'], ['steps', 'With steps'], ['all', 'Everything']];

// ---------------------------------------------------------------- items

async function loadPage() {
  const r = await api(`/api/sec/sessions/${sid(S.id)}/items?cursor=${S.next}&limit=${PAGE}`);
  merge(r.items);
  S.next = r.next_cursor;
  S.total = r.total;
  S.isLive = r.live;
}

function merge(items) {
  for (const it of items) {
    const at = S.items.findIndex((x) => x.i === it.i);
    if (at >= 0) S.items[at] = it; else S.items.push(it);
  }
  S.items.sort((a, b) => a.i - b.i);
}

// A live session: re-fetch from the last item held (its step group may have grown) and append the rest.
async function live() {
  const from = S.items.length ? S.items[S.items.length - 1].i : 0;
  if (S.next !== null && S.next !== undefined) return;   // still paging through older items: the next page has them
  const r = await api(`/api/sec/sessions/${sid(S.id)}/items?cursor=${from}&limit=${PAGE}`);
  if (!S.ctx.alive()) return;
  merge(r.items);
  S.next = r.next_cursor;
  S.isLive = r.live;
  const d = await api(`/api/sec/sessions/${sid(S.id)}`).catch(() => null);
  if (d && S.ctx.alive()) Object.assign(S, { meta: d.session, digest: d.digest, side: d.side, sources: d.sources });
  render(false);
}

const has = (q) => (text) => text && text.toLowerCase().includes(q.toLowerCase());

function itemHtml(it, q) {
  const attrs = `data-i="${it.i}" data-turn="${it.turn}"`;
  const match = q ? has(q) : () => false;
  if (it.kind === 'steps') {
    const hit = q && it.steps.some((st) => match(`${st.label} ${st.arg}`));
    const open = S.mode === 'all' || hit || S.openSteps.has(it.i);
    return `<details class="steps-row" ${attrs}${open ? ' open' : ''}><summary><span class="car" aria-hidden="true">▸</span>${esc(it.summary)}${it.live ? ' <span class="live" title="still running"></span>' : ''}</summary>`
      + `<ol>${it.steps.map((st) => `<li data-n="${st.n}"><span class="tn">${esc(st.label)}</span><span class="ta">${highlight(st.arg, q)}</span>`
        + `<span class="ts${['error', 'denied'].includes(st.status) ? ' bad' : ''}" title="${esc(st.tool)}${st.ms != null ? ` · ${(st.ms / 1000).toFixed(1)} s` : ''}">${esc(st.status_label)}</span></li>`).join('')}</ol></details>`;
  }
  if (it.kind === 'compact') return `<p class="note compact" ${attrs}>${esc(it.text)}</p>`;
  if (it.kind === 'interrupt') return `<p class="note interrupt" ${attrs}>${esc(it.text)}</p>`;
  const you = it.kind === 'prompt';
  const text = it.text || '';
  const long = text.length > CLIP && !q && !S.expanded.has(it.i);
  const marks = [
    you && it.mid_turn ? pill('p-info', 'sent mid-turn') : '',
    you && it.slash ? chip(it.slash) : '',
    it.in_digest ? pill('p-ok', 'in digest') : '',
    !you && it.error ? pill('p-bad', 'API error') : '',
    !you && it.live ? '<span class="live" title="still writing"></span>' : '',
  ].join('');
  const images = you && it.images ? `<span class="imgs">${it.images} image${it.images === 1 ? '' : 's'}</span>` : '';
  const body = text ? `<div class="body${long ? ' clip' : ''}">${highlight(text, q)}</div>` : (images ? '' : '<div class="body dim">(empty)</div>');
  const more = long || (it.truncated && !S.expanded.has(it.i))
    ? `<button class="more" type="button" data-more="${it.i}">Show all${it.truncated ? ` (${Number(it.chars).toLocaleString('en')} characters)` : ''}</button>`
    : '';
  return `<div class="msg${you ? ' you' : ''}${!you && it.error ? ' error' : ''}" ${attrs}><div class="who"><b>${you ? 'You' : 'Claude'}</b>`
    + `<span class="mono">${esc(it.time || '')}</span>${marks}</div>${body}${images}${more}</div>`;
}

function renderConv() {
  const conv = S.el.querySelector('#conv');
  if (!conv) return;
  const q = S.q;
  const match = q ? has(q) : null;
  let n = 0;
  const shown = S.items.filter((it) => S.mode !== 'msgs' || it.kind !== 'steps');
  if (q) {
    for (const it of S.items) {
      if (it.kind === 'steps') { if (S.mode !== 'msgs') n += it.steps.filter((st) => match(`${st.label} ${st.arg}`)).length; }
      else if (match(it.text)) n += 1;
    }
  }
  conv.innerHTML = shown.length ? shown.map((it) => itemHtml(it, q)).join('') : emptyNote(S.items.length ? 'Only steps so far. Choose With steps to see them.' : 'This session has no messages yet.');
  const more = S.el.querySelector('#convMore');
  more.hidden = S.next === null || S.next === undefined;
  more.textContent = `Load more (${S.items.length} of ${S.total} loaded)`;
  const note = S.el.querySelector('#convFindNote');
  note.textContent = q ? (n ? `${n} match${n === 1 ? '' : 'es'}${more.hidden ? '' : ' so far'}` : 'No match') : '';
  if (q && !more.hidden) serverFind(q);
}

// Matches in items not loaded yet come from the server.
const serverFind = debounce(async (q) => {
  if (!S || q !== S.q) return;
  try {
    const r = await api(`/api/sec/sessions/${sid(S.id)}/find?q=${encodeURIComponent(q)}`);
    if (!S || q !== S.q || !S.ctx.alive()) return;
    const loaded = S.items.length ? S.items[S.items.length - 1].i : -1;
    const later = r.hits.filter((h) => h.item > loaded);
    const note = S.el.querySelector('#convFindNote');
    if (later.length) {
      note.innerHTML = `${esc(note.textContent)} · <button class="more find-more" type="button" data-findgo="${later[0].item}" data-step="${later[0].step ?? ''}">${later.length}${later.length < r.total ? '+' : ''} more further on: ${snippetHtml(later[0].snippet).slice(0, 400)}</button>`;
    }
  } catch { /* find is a nicety */ }
}, 300);

// ---------------------------------------------------------------- side column

function side() {
  const m = S.meta;
  const st = S.ctx.state;
  const dg = S.digest;
  const writeBtn = canWriteDigest(m, st)
    ? `<button class="btn sm" type="button" id="writeDigest" style="margin-top:10px">${m.digest_state === 'ready' ? 'Update the digest' : 'Write a digest'}</button>` : '';
  const digest = dg
    ? `<section class="block"><h2 style="font-size:16px">Digest</h2><p style="font-size:14px">${esc(dg.summary)}</p>${dg.stale || m.digest_stale ? '<p class="note" style="margin-top:6px">The session went on after this digest.</p>' : ''}${writeBtn}</section>
       <section class="block"><h3 class="label">Shipped</h3>${bullets(dg.shipped)}<h3 class="label" style="margin-top:10px">Decided</h3>${bullets(dg.decisions)}<h3 class="label" style="margin-top:10px">Left open</h3>${bullets(dg.open_loops)}${dg.blockers.length ? `<h3 class="label" style="margin-top:10px">Blockers</h3>${bullets(dg.blockers)}` : ''}</section>`
    : `<section class="block"><h2 style="font-size:16px">Digest</h2><p class="dim" style="font-size:14px">${esc(noDigest(m))}</p>${writeBtn}</section>`;
  const sd = S.side;
  const files = sd.files.length
    ? `<ul>${sd.files.map((f) => `<li class="mono" title="${esc(f.path)}">${esc(f.label)} <span class="dim">${esc(f.action)}${f.count > 1 ? ` ×${f.count}` : ''}</span></li>`).join('')}</ul>${sd.files_total > sd.files.length ? `<p class="more-n">+ ${sd.files_total - sd.files.length} more</p>` : ''}`
    : '<p class="dim" style="font-size:13.5px">None</p>';
  const cmds = sd.commands.length
    ? `<ul>${sd.commands.map((c) => `<li class="row-cmd"><span class="mono" title="${esc(c.description || c.command)}">${esc(c.command)}</span><span class="${c.status === 'ok' ? 'okc' : ['error', 'denied'].includes(c.status) ? 'badc' : 'dim'} mono">${esc(c.status_label)}</span></li>`).join('')}</ul>${sd.commands_total > sd.commands.length ? `<p class="more-n">+ ${sd.commands_total - sd.commands.length} more</p>` : ''}`
    : '<p class="dim" style="font-size:13.5px">None</p>';
  const pub = sd.published.length
    ? `<ul>${sd.published.map((p) => {
      const title = p.url && /^https?:\/\//i.test(p.url) ? `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.title)}</a>` : esc(p.title);
      return `<li>${chip(p.kind)} ${title}${p.detail ? ` <span class="dim mono">${esc(p.detail)}</span>` : ''}</li>`;
    }).join('')}</ul>`
    : '<p class="dim" style="font-size:13.5px">Nothing published.</p>';
  const srcs = S.sources.length ? `<section class="block"><h3 class="label">Notes it wrote</h3><ul>${S.sources.map((x) => `<li>${srcHtml(x)}</li>`).join('')}</ul></section>` : '';
  return `<aside class="sside">${digest}
    <section class="block"><h3 class="label">Files touched${sd.files_total ? ` · ${sd.files_total}` : ''}</h3>${files}
      <h3 class="label" style="margin-top:10px">Commands${sd.commands_total ? ` · ${sd.commands_total} run${sd.commands_failed ? `, ${sd.commands_failed} failed` : ''}` : ''}</h3>${cmds}
      <h3 class="label" style="margin-top:10px">Published</h3>${pub}</section>${srcs}</aside>`;
}

// ---------------------------------------------------------------- page

function headHtml(m) {
  const bits = [m.day_label, m.time_label, m.model_label, m.cwd_label].filter(Boolean).map(esc).join(' · ');
  const running = m.state === 'running'
    ? `<span class="note"><span class="live" aria-hidden="true"></span> still running · ${m.prompts} prompt${m.prompts === 1 ? '' : 's'} so far</span>`
    : `<span class="note">${m.prompts} prompt${m.prompts === 1 ? '' : 's'} · ${m.steps} steps${m.errors ? ` · ${m.errors} failed` : ''}${m.compactions ? ` · compacted ${m.compactions}×` : ''}</span>`;
  return head(bits, m.title, `<div class="cr" style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center">${outcomePill(m.outcome)}${m.repos.map(chip).join('')}${m.branch ? chip(m.branch) : ''}${running}</div>`);
}

function render(full = true) {
  const m = S.meta;
  const el = S.el;
  if (full) {
    el.innerHTML = `${headHtml(m)}
      <div class="sactions"><a class="btn sm" href="#sessions" style="text-decoration:none">All sessions</a><a class="btn sm" href="#timeline" style="text-decoration:none">Timeline</a><button class="btn sm" id="copyResume" type="button" title="${esc(m.resume_command)}">Copy resume command</button></div>
      ${m.transcript_exists ? '' : '<p class="note warnline">The transcript file is gone; only what the secretary stored is left.</p>'}
      <div class="sview"><div style="min-width:0">
        <div class="convbar"><div class="seg" id="convMode" role="group" aria-label="Show">${MODES.map(([k, l]) => `<button type="button" data-m="${k}" aria-pressed="${S.mode === k}">${l}</button>`).join('')}</div>
          <input id="convFind" type="search" placeholder="Find in this session" aria-label="Find in this session" autocomplete="off"><span class="note" id="convFindNote" role="status"></span></div>
        <div class="conv" id="conv"></div>
        <button class="btn sm loadmore" type="button" id="convMore" hidden>Load more</button>
      </div><div id="sideCol">${side()}</div></div>`;
    el.querySelector('#convFind').value = S.q;
  } else {
    // A live update: the header's counts and the side column change; the find box and modes stay as they are.
    const h = el.querySelector('.sec-head');
    if (h) h.outerHTML = headHtml(m);
    el.querySelector('#sideCol').innerHTML = side();
  }
  renderConv();
}

async function loadUntil(test) {
  while (!test() && S.next !== null && S.next !== undefined) {
    await loadPage();
    if (!S.ctx.alive()) return false;
  }
  return test();
}

async function jump(j) {
  if (j.item != null) await loadUntil(() => S.items.some((x) => x.i === j.item));
  else if (j.turn) await loadUntil(() => S.items.some((x) => x.turn >= j.turn));
  if (!S.ctx.alive()) return;
  const target = j.item != null ? S.items.find((x) => x.i === j.item) : null;
  if (target?.kind === 'steps') { S.openSteps.add(target.i); if (S.mode === 'msgs') S.mode = 'steps'; }
  if (j.q) S.q = j.q;
  render();
  const el = (j.item != null && S.el.querySelector(`#conv [data-i="${j.item}"]`)) || S.el.querySelector(`#conv [data-turn="${j.turn}"]`);
  if (!el) { if (j.turn || j.item != null) S.ctx.toast('That part of the session is not there any more.'); return; }
  el.scrollIntoView({ block: 'center' });
  el.classList.add('flash');
  if (j.step != null && el.tagName === 'DETAILS') {
    el.open = true;
    const li = el.querySelector(`li[data-n="${j.step}"]`);
    if (li) { li.classList.add('flash'); li.scrollIntoView({ block: 'center' }); }
  }
}

export async function show(el, route, ctx) {
  const same = S && S.id === route.id && S.el === el && el.childElementCount;
  if (!same) {
    S = { id: route.id, el, ctx, items: [], next: 0, total: 0, mode: store.local('deskmate.convMode', 'steps'), q: '', expanded: new Set(), openSteps: new Set() };
    if (!MODES.some(([k]) => k === S.mode)) S.mode = 'steps';
  }
  S.ctx = ctx;
  let d;
  try {
    d = await api(`/api/sec/sessions/${sid(route.id)}`);
  } catch (err) {
    if (err.status !== 404) throw err;
    if (!ctx.alive()) return;
    el.innerHTML = head('Session', 'No such session') + emptyState({
      title: 'This session is not here',
      text: 'It may have had no prompts, been outside the folders the secretary reads, or its transcript was removed. <a href="#sessions">Back to sessions</a>',
      glyph: '?',
    });
    return;
  }
  if (!ctx.alive()) return;
  Object.assign(S, { meta: d.session, digest: d.digest, side: d.side, sources: d.sources });
  if (!same) await loadPage();
  if (!ctx.alive()) return;
  document.title = `Deskmate · ${d.session.title.slice(0, 60)}`;

  let j = null;
  try { j = JSON.parse(store.get('deskmate.secJump', 'null')); } catch { /* ignore */ }
  if (j && j.id === route.id) store.del('deskmate.secJump'); else j = null;
  if (!j && route.turn) j = { turn: route.turn, item: null, step: null, q: '' };
  if (j) await jump(j); else render();
  if (!ctx.alive()) return;
  bind();
}

function bind() {
  const el = S.el;
  if (el.dataset.bound) return;
  el.dataset.bound = '1';  // the article outlives each show(): bind its handlers once, read S at event time
  el.addEventListener('click', async (e) => {
    if (!S || S.el !== el) return;
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.m) {
      S.mode = b.dataset.m;
      store.setLocal('deskmate.convMode', S.mode);
      el.querySelectorAll('#convMode button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      renderConv();
    } else if (b.id === 'convMore') {
      b.disabled = true;
      try { await loadPage(); renderConv(); } catch (err) { S.ctx.toast(err.message); }
      b.disabled = false;
    } else if (b.dataset.more) {
      const i = Number(b.dataset.more);
      const it = S.items.find((x) => x.i === i);
      if (it?.truncated) {
        try { merge([await api(`/api/sec/sessions/${sid(S.id)}/items/${i}`)]); } catch (err) { S.ctx.toast(err.message); return; }
      }
      S.expanded.add(i);
      const node = el.querySelector(`#conv [data-i="${i}"]`);
      if (node) node.outerHTML = itemHtml(S.items.find((x) => x.i === i), S.q);
    } else if (b.dataset.findgo) {
      await jump({ item: Number(b.dataset.findgo), step: b.dataset.step === '' ? null : Number(b.dataset.step), q: S.q, turn: null });
    } else if (b.id === 'copyResume') {
      await copyText(S.meta.resume_command, S.ctx.toast, `Copied: ${S.meta.resume_command}`);
    } else if (b.id === 'writeDigest') {
      b.disabled = true;
      if (await writeDigest(S.id, S.ctx)) b.textContent = 'Queued'; else b.disabled = false;
    }
  });
  el.addEventListener('toggle', (e) => {
    const d = e.target;
    if (!S || !d.matches?.('details.steps-row')) return;
    const i = Number(d.dataset.i);
    if (d.open) S.openSteps.add(i); else S.openSteps.delete(i);
  }, true);
  const find = debounce(() => { S.q = el.querySelector('#convFind')?.value.trim() || ''; renderConv(); }, 150);
  el.addEventListener('input', (e) => { if (e.target.id === 'convFind') find(); });
  el.addEventListener('keydown', (e) => {
    if (e.target.id !== 'convFind' || e.key !== 'Enter') return;
    e.preventDefault();
    const first = el.querySelector('#conv mark');
    first?.closest('[data-i]')?.scrollIntoView({ block: 'center' });
  });
}
