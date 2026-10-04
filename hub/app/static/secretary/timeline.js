// Timeline (#timeline): one line per session, grouped by day, newest first. A row opens to its digest.
import {
  api, esc, chip, srcHtml, outcomePill, qs, head, emptyState, emptyNote, bullets, canWriteDigest, writeDigest, store, noDigest,
} from './common.js';

let last = null;
let repo = store.get('deskmate.tlRepo', 'all');

document.addEventListener('sec_session', () => { if (last && !last.el.hidden && last.ctx.alive()) show(last.el, last.route, last.ctx); });

function row(s, ctx) {
  const dg = s.digest;
  const summary = s.summary
    ? `<span class="sm${s.summary_source === 'prompt' ? ' dim' : ''}">${esc(s.summary)}</span>`
    : `<span class="sm dim">${s.digest_state === 'skipped' ? 'Too short for a digest.' : 'No summary yet.'}</span>`;
  const pills = `${outcomePill(s.outcome)}${s.state === 'running' ? ' <span class="note"><span class="live" aria-hidden="true"></span> running</span>' : ''}`;
  const sources = s.sources.length ? s.sources.map(srcHtml).join('<br>') : '<span class="dim">None</span>';
  const writeBtn = canWriteDigest(s, ctx.state)
    ? `<button class="btn sm" type="button" data-digest="${esc(s.id)}">${s.digest_state === 'ready' ? 'Update the digest' : 'Write a digest'}</button>`
    : '';
  const body = dg
    ? `<div><h5 class="label">Shipped</h5>${bullets(dg.shipped)}</div>
       <div><h5 class="label">Decided</h5>${bullets(dg.decisions)}</div>
       <div><h5 class="label">Left open</h5>${bullets(dg.open_loops)}</div>
       ${dg.blockers.length ? `<div><h5 class="label">Blockers</h5>${bullets(dg.blockers)}</div>` : ''}`
    : `<div class="nod"><h5 class="label">Digest</h5><p class="dim">${esc(noDigest(s))}</p></div>`;
  return `<details class="sessrow" data-id="${esc(s.id)}"><summary>
      <span class="time">${esc(s.time_label)}</span><span class="ttl">${esc(s.title)}</span><span class="stt">${pills}</span>
      ${summary}
      <span class="cr">${s.repos.map(chip).join('')}${dg?.stale || s.digest_stale ? '<span class="note">the session went on after this digest</span>' : ''}</span>
    </summary>
    <div class="digest">${body}
      <div><h5 class="label">Source</h5>${sources}<br>
        <a class="btn sm" style="margin-top:8px;display:inline-block;text-decoration:none" href="#s-${esc(s.id)}">Open the session</a>
        ${writeBtn ? `<br><span style="display:inline-block;margin-top:6px">${writeBtn}</span>` : ''}</div>
    </div></details>`;
}

function dayHtml(d, ctx) {
  return `<section class="daygroup"><div class="dayhead"><h2>${esc(d.day_label)}</h2><span class="dim">${esc(d.headline)}</span>`
    + `<a class="note" href="#today-${esc(d.day)}">Day page</a></div>${d.sessions.map((s) => row(s, ctx)).join('')}</section>`;
}

export async function show(el, route, ctx) {
  last = { el, route, ctx };
  const open = new Set([...el.querySelectorAll('details.sessrow[open]')].map((x) => x.dataset.id));
  const t = await api(`/api/sec/timeline?${qs({ repo })}`);
  if (!ctx.alive()) return;
  if (repo !== 'all' && !t.repos.some((r) => r.key === repo)) { repo = 'all'; store.set('deskmate.tlRepo', 'all'); }
  const roots = ctx.state?.sources?.roots || [];
  const filter = t.repos.length
    ? `<div class="filterbar" id="tlFilter">${[{ key: 'all', label: 'All repos' }, ...t.repos].map((r) => `<button type="button" aria-pressed="${r.key === repo}" data-r="${esc(r.key)}">${esc(r.label)}</button>`).join('')}</div>`
    : '';
  let body;
  if (!t.days.length && repo === 'all') {
    body = emptyState({
      title: 'No sessions yet',
      text: `A session appears here after its first turn ends. The secretary reads Claude Code sessions started in ${roots.length ? roots.map((r) => `<code>${esc(r)}</code>`).join(', ') : 'the folders you chose in setup'}.`,
      glyph: '0',
    });
  } else if (!t.days.length) {
    body = emptyNote(`No sessions touched ${t.repos.find((r) => r.key === repo)?.label || repo} in these days.`);
  } else {
    body = t.days.map((d) => dayHtml(d, ctx)).join('');
  }
  el.innerHTML = `${head('One line per session, written from its transcript. Open one for the digest.', 'Timeline')}
    ${filter}
    <div id="timeline" style="display:grid;gap:22px">${body}</div>
    ${t.next_before ? `<button class="btn sm loadmore" type="button" id="tlMore" data-before="${esc(t.next_before)}">Load earlier days</button>` : ''}`;
  for (const id of open) el.querySelector(`details.sessrow[data-id="${CSS.escape(id)}"]`)?.setAttribute('open', '');

  el.querySelector('#tlFilter')?.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-r]');
    if (!b) return;
    repo = b.dataset.r;
    store.set('deskmate.tlRepo', repo);
    show(el, route, ctx).catch((err) => ctx.toast(err.message));
  });
  el.querySelector('#tlMore')?.addEventListener('click', async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    try {
      const more = await api(`/api/sec/timeline?${qs({ repo, before: b.dataset.before })}`);
      if (!ctx.alive()) return;
      el.querySelector('#timeline').insertAdjacentHTML('beforeend', more.days.map((d) => dayHtml(d, ctx)).join('') || emptyNote('No earlier sessions.'));
      if (more.next_before) { b.dataset.before = more.next_before; b.disabled = false; } else b.remove();
    } catch (err) { ctx.toast(err.message); b.disabled = false; }
  });
  el.querySelector('#timeline').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-digest]');
    if (!b) return;
    b.disabled = true;
    if (await writeDigest(b.dataset.digest, ctx)) b.textContent = 'Queued'; else b.disabled = false;
  });
}
