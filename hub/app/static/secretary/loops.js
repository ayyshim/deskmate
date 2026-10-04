// Open loops (#loops): what waits on the user and the follow-ups the changelogs left. Done and Snooze live in
// the secretary only; nothing is written back to the docs folder.
import { api, post, esc, chip, srcHtml, head, emptyNote, qs, weekday, store, docs } from './common.js';

let last = null;
let state = store.get('deskmate.loopState', 'open');
// Follow-ups can run to hundreds (every changelog follow-up of two weeks): show the newest first and the rest
// on request, so the list stays readable and the note form below it stays in reach.
const FOLLOW_FIRST = 30;
let allFollow = false;

document.addEventListener('sec_loops', () => { if (last && !last.el.hidden && last.ctx.alive() && !last.busy) show(last.el, last.route, last.ctx); });

const TICK = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6.5l2.5 2.5L10 3.5" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function loopLi(n) {
  const done = n.state === 'done';
  const action = n.state === 'open'
    ? '<button class="src" type="button" data-snooze>snooze</button>'
    : `<button class="src" type="button" data-reopen>${done ? 'reopen' : 'wake now'}</button>`;
  const until = n.state === 'snoozed' && n.until ? `<span>until ${esc(weekday(n.until))}</span>` : '';
  return `<li data-id="${esc(n.id)}"${done ? ' class="done"' : ''}><button class="tick" type="button" aria-label="${done ? 'Mark not done' : 'Mark done'}" aria-pressed="${done}">${TICK}</button>`
    + `<div class="tx"><span>${esc(n.text)}</span><div class="meta">${n.where ? chip(n.where) : ''}${srcHtml(n.src)}${until}${n.gone ? '<span>its source no longer says this</span>' : ''}</div></div>`
    + `<div class="age">${esc(n.age_label)}<br>${action}</div></li>`;
}

const EMPTY = {
  open: ['Nothing is waiting on you.', 'No follow-ups from the last 14 days.'],
  snoozed: ['Nothing snoozed.', 'Nothing snoozed.'],
  done: ['Nothing marked done.', 'Nothing marked done.'],
};

function render(el, d, ctx) {
  const c = d.counts;
  const filters = [['open', `Open · ${c.open}`], ['snoozed', `Snoozed · ${c.snoozed}`], ['done', `Done · ${c.done}`]];
  const dc = docs(ctx.state);
  const followNote = dc.changelog ? '' : `<p class="note">${dc.set ? 'The docs folder has no changelog folder' : 'No docs folder is set'}, so follow-ups come only from session digests and your notes.</p>`;
  const [e1, e2] = EMPTY[state] || EMPTY.open;
  const follow = allFollow ? d.followup : d.followup.slice(0, FOLLOW_FIRST);
  el.innerHTML = `${head('From changelog follow-ups, design status lines, memory notes, unpushed branches and session digests. Done and Snooze live in the secretary only.', 'Open loops')}
    <div class="filterbar" id="loopFilter" role="group" aria-label="Show">${filters.map(([k, l]) => `<button type="button" data-state="${k}" aria-pressed="${k === state}">${esc(l)}</button>`).join('')}</div>
    <section class="block"><h2>Waiting on you <span class="count">${d.you.length || ''}</span></h2>${d.you.length ? `<ul class="loops" id="loopsYou">${d.you.map(loopLi).join('')}</ul>` : emptyNote(e1)}</section>
    <section class="block"><h2>Follow-ups <span class="count">${d.followup.length || ''}</span></h2>${d.followup.length ? `<ul class="loops" id="loopsFollow">${follow.map(loopLi).join('')}</ul>` : emptyNote(e2)}${follow.length < d.followup.length ? `<button class="btn sm loadmore" type="button" id="followAll">Show all ${d.followup.length} follow-ups</button>` : ''}${followNote}</section>
    <section class="block" aria-labelledby="h-note"><h2 id="h-note">Add a note</h2>
      <p class="note" style="margin-bottom:10px">A follow-up for you shows up above; a decision goes to Decisions; a note is kept for the brief.</p>
      <form class="askform" id="noteForm">
        <select id="noteKind" aria-label="Kind of note"><option value="followup">Follow-up for me</option><option value="decision">Decision</option><option value="note">Note</option></select>
        <input id="noteText" type="text" maxlength="1000" placeholder="For example: rotate the staging SMTP password" aria-label="Note">
        <button class="btn" type="submit">Add</button>
      </form>
    </section>`;
}

export async function show(el, route, ctx) {
  last = { el, route, ctx, busy: false };
  const d = await api(`/api/sec/loops?${qs({ state })}`);
  if (!ctx.alive()) return;
  last.data = d;
  render(el, d, ctx);
  if (el.dataset.bound) return;
  el.dataset.bound = '1';  // the article outlives each show(): bind once, read `last` at event time
  el.addEventListener('click', onClick);
  el.addEventListener('submit', onSubmit);
}

async function act(li, path, body) {
  last.busy = true;
  try { return await post(`/api/sec/loops/${encodeURIComponent(li.dataset.id)}/${path}`, body); }
  finally { last.busy = false; }
}

async function onClick(e) {
  const { el, ctx } = last;
  if (e.target.closest('#followAll')) { allFollow = true; render(el, last.data, ctx); return; }
  const f = e.target.closest('#loopFilter button[data-state]');
  if (f) {
    state = f.dataset.state;
    allFollow = false;
    store.set('deskmate.loopState', state);
    show(el, last.route, ctx).catch((err) => ctx.toast(err.message));
    return;
  }
  const li = e.target.closest('li[data-id]');
  if (!li) return;
  const tick = e.target.closest('.tick');
  if (tick) {
    // Optimistic: strike it through now, put it back if the hub says no. It stays in the list until the next refresh.
    const wasDone = li.classList.contains('done');
    const setDone = (on) => { li.classList.toggle('done', on); tick.setAttribute('aria-label', on ? 'Mark not done' : 'Mark done'); tick.setAttribute('aria-pressed', String(on)); };
    setDone(!wasDone);
    try {
      await act(li, wasDone ? 'reopen' : 'done', {});
      ctx.refreshState();
    } catch (err) { setDone(wasDone); ctx.toast(err.message); }
    return;
  }
  if (e.target.closest('[data-snooze]')) {
    try {
      const r = await act(li, 'snooze', { days: 3 });
      ctx.toast(`Snoozed for 3 days. It comes back on ${r.loop.until ? weekday(r.loop.until) : 'its day'}.`);
      li.remove();
      ctx.refreshState();
    } catch (err) { ctx.toast(err.message); }
    return;
  }
  if (e.target.closest('[data-reopen]')) {
    try {
      await act(li, 'reopen', {});
      ctx.toast('It is open again.');
      li.remove();
      ctx.refreshState();
    } catch (err) { ctx.toast(err.message); }
  }
}

async function onSubmit(e) {
  if (e.target.id !== 'noteForm') return;
  e.preventDefault();
  const { el, ctx } = last;
  const text = el.querySelector('#noteText').value.trim();
  const kind = el.querySelector('#noteKind').value;
  if (!text) { ctx.toast('Write the note first.'); return; }
  try {
    await post('/api/sec/notes', { kind, text });
    ctx.toast(kind === 'followup' ? 'Added to Waiting on you.' : kind === 'decision' ? 'Added to Decisions.' : 'Noted. The secretary keeps it for the brief.');
    el.querySelector('#noteText').value = '';
    ctx.refreshState();
    if (kind === 'followup') show(el, last.route, ctx).catch((err) => ctx.toast(err.message));
  } catch (err) { ctx.toast(err.message); }
}
