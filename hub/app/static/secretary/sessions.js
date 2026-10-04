// Sessions (#sessions): every session, newest first. Typing filters titles; Enter searches inside all of them
// (prompts, answers, commands, file paths). A hit opens the session at that spot.
import {
  api, esc, chip, outcomePill, qs, head, emptyState, emptyNote, snippetHtml, debounce, store,
} from './common.js';

let last = null;
let repo = store.get('deskmate.sessRepo', 'all');

document.addEventListener('sec_session', () => {
  // Refresh the list only while it is showing (not search hits, which the user is reading).
  if (last && !last.el.hidden && last.ctx.alive() && last.el.querySelector('#sessHits')?.hidden) loadList(last.el, last.ctx, false);
});

function srow(s) {
  const when = `${esc(s.day_label.replace(/^\w+ /, ''))}<br>${esc(s.time_label)}`;
  const running = s.state === 'running' ? '<span><span class="live" aria-hidden="true"></span> running</span>' : '';
  const dl = `<span${['failed', 'capped', 'no_token', 'paused'].includes(s.digest_state) ? ' class="warnc"' : ''}>${esc(s.digest_label)}${s.digest_stale && s.digest_state === 'ready' ? ' · older than the session' : ''}</span>`;
  const sm = s.summary ? `<span class="sm${s.summary_source === 'prompt' ? ' dim' : ''}">${esc(s.summary)}</span>` : '';
  return `<a class="srow" href="#s-${esc(s.id)}"><span class="when">${when}</span><span class="ttl">${esc(s.title)}</span>`
    + `<span class="rt">${outcomePill(s.outcome)}${running}${dl}</span>${sm}<span class="cr">${s.repos.map(chip).join('')}</span></a>`;
}

async function loadList(el, ctx, append, cursor = null) {
  const q = el.querySelector('#sessSearch').value.trim();
  const r = await api(`/api/sec/sessions?${qs({ q, repo, cursor, limit: 50 })}`);
  if (!ctx.alive()) return;
  const list = el.querySelector('#sessList');
  const roots = ctx.state?.sources?.roots || [];
  let html;
  if (!r.total) {
    html = emptyState({
      title: 'No sessions yet',
      text: `A session appears here after its first turn ends. The secretary reads Claude Code sessions started in ${roots.length ? roots.map((x) => `<code>${esc(x)}</code>`).join(', ') : 'the folders you chose in setup'}.`,
      glyph: '0',
    });
  } else if (!r.sessions.length && !append) {
    html = emptyNote(q ? 'No session title matches. Press Enter to search inside them.' : 'No session touched this repo.');
  } else {
    html = r.sessions.map(srow).join('');
  }
  el.querySelector('#sessMore')?.remove();
  if (append) list.insertAdjacentHTML('beforeend', html); else list.innerHTML = html;
  if (r.next_cursor) list.insertAdjacentHTML('afterend', `<button class="btn sm loadmore" type="button" id="sessMore" data-cursor="${esc(r.next_cursor)}">Show more sessions</button>`);
  if (el.querySelector('#sessHits').hidden) {
    el.querySelector('#sessSearchNote').textContent = q && r.total ? `${r.matched} of ${r.total} titles match · Enter searches inside` : '';
  }
}

async function search(el, ctx, q) {
  const note = el.querySelector('#sessSearchNote');
  note.textContent = 'Searching…';
  const r = await api(`/api/sec/search?${qs({ q, repo, limit: 20 })}`);
  if (!ctx.alive()) return;
  store.set('deskmate.secSearch', JSON.stringify({ q, hits: true }));
  const hits = el.querySelector('#sessHits');
  note.textContent = r.total_hits ? `${r.total_hits} hit${r.total_hits === 1 ? '' : 's'} in ${r.total_sessions} session${r.total_sessions === 1 ? '' : 's'}` : '';
  hits.innerHTML = (r.results.length
    ? r.results.map((x) => {
      const s = x.session;
      return `<div class="hit"><h4><a href="#s-${esc(s.id)}">${esc(s.title)}</a></h4><span class="note">${esc(s.day_label)} · ${esc(s.time_label)}${s.repos.length ? ` · ${s.repos.map(esc).join(', ')}` : ''}</span>`
        + x.hits.map((h) => `<button type="button" class="sn" data-go="${esc(s.id)}" data-item="${h.item}" data-turn="${h.turn}" data-step="${h.step ?? ''}">${snippetHtml(h.snippet)}</button>`).join('')
        + (x.more > 0 ? `<span class="note">and ${x.more} more in this session</span>` : '')
        + '</div>';
    }).join('')
    : emptyNote(`Nothing found for “${q}”. Every word must appear; the last one may be the start of a word.`))
    + '<button class="btn sm" id="clearHits" type="button">Back to the list</button>';
  hits.hidden = false;
  el.querySelector('#sessList').hidden = true;
  el.querySelector('#sessMore')?.setAttribute('hidden', '');
}

function backToList(el) {
  el.querySelector('#sessHits').hidden = true;
  el.querySelector('#sessList').hidden = false;
  el.querySelector('#sessMore')?.removeAttribute('hidden');
  el.querySelector('#sessSearchNote').textContent = '';
  const saved = JSON.parse(store.get('deskmate.secSearch', '{}') || '{}');
  store.set('deskmate.secSearch', JSON.stringify({ q: saved.q || '', hits: false }));
}

export async function show(el, route, ctx) {
  last = { el, route, ctx };
  const roots = ctx.state?.sources?.roots || [];
  const where = roots.length ? roots.join(', ') : 'the folders you chose';
  const repos = ctx.state?.repos || [];
  if (repo !== 'all' && !repos.some((r) => r.key === repo)) repo = 'all';
  el.innerHTML = `${head(`Every Claude Code session started in ${esc(where)}, newest first. Enter searches inside all of them: prompts, answers, commands, file paths.`, 'Sessions')}
    <form class="searchrow" id="sessSearchForm" role="search">
      <input class="search" id="sessSearch" type="search" placeholder="Filter by title, or Enter to search inside" aria-label="Search sessions" autocomplete="off">
      <button class="btn" type="submit">Search inside</button>
      <span class="note" id="sessSearchNote" role="status"></span>
    </form>
    ${repos.length ? `<div class="filterbar" id="sessFilter">${[{ key: 'all', label: 'All repos' }, ...repos].map((r) => `<button type="button" aria-pressed="${r.key === repo}" data-r="${esc(r.key)}">${esc(r.label)}</button>`).join('')}</div>` : ''}
    <div class="hits" id="sessHits" hidden></div>
    <div class="slist" id="sessList"></div>`;
  const input = el.querySelector('#sessSearch');
  const saved = JSON.parse(store.get('deskmate.secSearch', '{}') || '{}');
  input.value = saved.q || '';
  await loadList(el, ctx, false);
  if (!ctx.alive()) return;
  if (saved.hits && saved.q && saved.q.trim().length >= 2) await search(el, ctx, saved.q.trim());

  const refilter = debounce(() => {
    store.set('deskmate.secSearch', JSON.stringify({ q: input.value, hits: false }));
    loadList(el, ctx, false).catch((err) => ctx.toast(err.message));
  }, 200);
  input.addEventListener('input', () => { backToList(el); refilter(); });
  el.querySelector('#sessSearchForm').addEventListener('submit', (e) => {
    e.preventDefault();
    refilter.cancel();
    const q = input.value.trim();
    if (q.replace(/[^\p{L}\p{N}]/gu, '').length < 2) { ctx.toast('Type at least two characters.'); return; }
    search(el, ctx, q).catch((err) => { el.querySelector('#sessSearchNote').textContent = ''; ctx.toast(err.message); });
  });
  el.querySelector('#sessFilter')?.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-r]');
    if (!b) return;
    repo = b.dataset.r;
    store.set('deskmate.sessRepo', repo);
    el.querySelectorAll('#sessFilter button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    const q = input.value.trim();
    const run = el.querySelector('#sessHits').hidden ? loadList(el, ctx, false) : search(el, ctx, q);
    run.catch((err) => ctx.toast(err.message));
  });
  if (el.dataset.bound) return;
  el.dataset.bound = '1';  // the article outlives each show(): bind its click handler once
  el.addEventListener('click', (e) => {
    const { ctx: c } = last;
    const q = el.querySelector('#sessSearch')?.value.trim() || '';
    if (e.target.closest('#clearHits')) { backToList(el); return; }
    const more = e.target.closest('#sessMore');
    if (more) { more.disabled = true; loadList(el, c, true, more.dataset.cursor).catch((err) => { c.toast(err.message); more.disabled = false; }); return; }
    const sn = e.target.closest('.sn[data-go]');
    if (!sn) return;
    // Hand the spot to the session page (it loads pages until the item is there, then flashes it).
    store.set('deskmate.secJump', JSON.stringify({
      id: sn.dataset.go, turn: Number(sn.dataset.turn), item: Number(sn.dataset.item),
      step: sn.dataset.step === '' ? null : Number(sn.dataset.step), q,
    }));
    location.hash = `#s-${sn.dataset.go}`;
  });
}
