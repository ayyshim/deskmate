// Decisions (#decisions): the decision tables in the design docs, decisions from session digests and the
// user's own decision notes, newest first, with a filter.
import { api, esc, chip, pill, srcHtml, head, emptyState, qs, debounce, store, docs } from './common.js';

let last = null;

function rowHtml(r) {
  const where = r.where ? chip(r.where) : (r.repos.length ? r.repos.map(chip).join(' ') : '');
  return `<tr><td>${esc(r.day || '—')}</td><td>${r.ref ? `<span class="ref">${esc(r.ref)}</span>` : ''}${esc(r.text)}${r.is_question ? `<span class="qpill">${pill('p-warn', 'open question')}</span>` : ''}`
    + `${r.outcome ? `<div class="note">${esc(r.outcome)}</div>` : ''}</td><td>${where}</td><td>${srcHtml(r.src)}</td></tr>`;
}

async function load(el, ctx, append, cursor = null) {
  const q = el.querySelector('#decSearch').value.trim();
  const d = await api(`/api/sec/decisions?${qs({ q, cursor, limit: 200 })}`);
  if (!ctx.alive() || q !== el.querySelector('#decSearch').value.trim()) return;
  const body = el.querySelector('#decRows');
  const rows = d.rows.map(rowHtml).join('');
  if (append) body.insertAdjacentHTML('beforeend', rows);
  else body.innerHTML = rows || `<tr><td colspan="4" class="dim">${q ? `No decision matches “${esc(q)}”.` : 'No decisions recorded yet.'}</td></tr>`;
  const more = el.querySelector('#decMore');
  more.hidden = !d.next_cursor;
  more.dataset.cursor = d.next_cursor || '';
  el.querySelector('#decNote').textContent = d.total ? `${d.total} decision${d.total === 1 ? '' : 's'}${q ? ' match' : ''}` : '';
}

export async function show(el, route, ctx) {
  last = { el, ctx };
  const dc = docs(ctx.state);
  const from = dc.design ? `the decision tables in ${esc(dc.label || 'the docs folder')}/design, ` : '';
  el.innerHTML = `${head(`From ${from}the session digests and your decision notes`, 'Decisions')}
    <div class="searchrow"><input class="search" id="decSearch" type="search" placeholder="Filter decisions by any word" aria-label="Filter decisions" autocomplete="off"><span class="note" id="decNote" role="status"></span></div>
    <div id="decWrap"><div class="tablewrap"><table class="dt"><thead><tr><th>Date</th><th>Decision</th><th>Where</th><th>Source</th></tr></thead><tbody id="decRows"></tbody></table></div></div>
    <button class="btn sm loadmore" type="button" id="decMore" hidden>Show older decisions</button>`;
  const input = el.querySelector('#decSearch');
  input.value = store.get('deskmate.decQ', '');
  await load(el, ctx, false);
  if (!ctx.alive()) return;
  // A brand-new install: say where decisions come from instead of an empty table.
  if (!input.value && !el.querySelector('#decRows tr td:not([colspan])')) {
    el.querySelector('#decWrap').innerHTML = emptyState({
      title: 'No decisions yet',
      text: `Decisions arrive from session digests${dc.design ? ', from the decision tables in your design docs' : ''} and from notes you add on the Open loops page${dc.design ? '' : '. With a docs folder that has a <code>design/</code> folder, its decision tables show here too'}.`,
      glyph: '0',
    });
    el.querySelector('#decSearch').hidden = true;
    return;
  }
  const refilter = debounce(() => { store.set('deskmate.decQ', input.value); load(el, ctx, false).catch((err) => ctx.toast(err.message)); }, 200);
  input.addEventListener('input', refilter);
  el.querySelector('#decMore').addEventListener('click', (e) => {
    const b = e.currentTarget;
    load(el, ctx, true, b.dataset.cursor).catch((err) => ctx.toast(err.message));
  });
}
