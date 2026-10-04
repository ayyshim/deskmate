// Board (#board): one card per feature folder in the docs folder's design/, in three lanes by status.
import { api, esc, chip, pill, srcHtml, head, emptyNote, emptyState, docs, noDocsState } from './common.js';

function card(c) {
  const proto = c.prototype_url && /^https?:\/\//i.test(c.prototype_url)
    ? `<a class="src" href="${esc(c.prototype_url)}" target="_blank" rel="noopener noreferrer">prototype</a>` : '';
  return `<div class="fcard"><h4>${esc(c.title)}</h4><div class="fs" title="${esc(c.status)}">${esc(c.status_short)}</div>`
    + `${c.projects.length ? `<div class="ps">${c.projects.map(chip).join('')}</div>` : ''}`
    + `<div class="ff"><span>Last activity ${esc(c.last_activity_label)}</span>${c.open_loops ? `<a href="#loops" style="text-decoration:none">${pill('p-warn', `${c.open_loops} open`)}</a>` : ''}${proto}${srcHtml(c.doc)}</div></div>`;
}

export async function show(el, route, ctx) {
  const d = docs(ctx.state);
  const label = d.label ? `${esc(d.label)}/design` : 'design/';
  const eyebrow = `One card per folder in ${label}, status read from the design index and the latest changelog`;
  if (ctx.state && !d.design) { el.innerHTML = head(eyebrow, 'Board') + noDocsState(ctx.state, 'design'); return; }
  const b = await api('/api/sec/board');
  if (!ctx.alive()) return;
  const cards = b.lanes.reduce((n, l) => n + l.cards.length, 0);
  const body = cards
    ? `<div class="board" id="board">${b.lanes.map((l) => `<section class="lane"><header><h3>${esc(l.title)}</h3><span class="count">${l.cards.length}</span><p>${esc(l.hint)}</p></header>`
      + `${l.cards.length ? l.cards.map(card).join('') : emptyNote('Nothing here.')}</section>`).join('')}</div>`
    : emptyState({ title: 'No features yet', text: `Each folder in <code>${label}</code> becomes a card here, in a lane by the status line of its doc.`, glyph: '0' });
  el.innerHTML = `${head(eyebrow, 'Board')}${body}
    ${b.unindexed.length ? `<p class="note">Not in the design index: ${b.unindexed.map(esc).join(', ')}</p>` : ''}`;
}
