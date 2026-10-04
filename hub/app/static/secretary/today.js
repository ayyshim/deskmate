// Today (#today, #today-YYYY-MM-DD): the brief so far, what needs the user, what shipped, what is in
// progress, what was decided, and changelog entries by repo. Markup and classes are the prototype's.
import { api, post, esc, chip, pill, srcHtml, shipPill, qs, emptyNote, fmtTime, weekday, head, docs } from './common.js';

let last = null;  // { el, route, ctx } of the latest show(), for live refreshes

for (const kind of ['sec_today', 'sec_brief', 'sec_loops']) {
  document.addEventListener(kind, () => { if (last && !last.el.hidden && last.ctx.alive()) show(last.el, last.route, last.ctx); });
}

const list = (id, rows, empty, row) => (rows.length ? `<ul class="items" id="${id}">${rows.map(row).join('')}</ul>` : emptyNote(empty));

function eyebrow(d, s) {
  const tz = s?.tz;
  const at = s?.schedule || { brief_at: '18:30', discord_at: '09:00' };
  const todayHash = (day) => (day === s?.today ? '#today' : `#today-${day}`);
  const nav = [
    d.prev_day ? `<a href="#today-${esc(d.prev_day)}">‹ ${esc(weekday(d.prev_day))}</a>` : '',
    d.next_day ? `<a href="${esc(todayHash(d.next_day))}">${esc(weekday(d.next_day))} ›</a>` : '',
  ].filter(Boolean).join(' · ');
  let text;
  if (d.brief?.status === 'running') text = 'Writing the brief…';
  else if (d.brief?.status === 'failed') text = d.brief.error || 'The brief failed.';   // the hub's own sentence
  else if (d.brief?.status === 'ready') text = `Brief · written ${d.brief.ts ? fmtTime(d.brief.ts, tz) : ''}${d.is_today ? ` · morning post at ${at.discord_at}` : ''}`;
  else if (d.is_today) text = `Brief so far${d.updated_ts ? ` · updated ${fmtTime(d.updated_ts, tz)}` : ''} · full brief at ${at.brief_at}, morning post at ${at.discord_at}`;
  else text = 'No brief was written for this day';
  return `${esc(text)}${nav ? ` · ${nav}` : ''}`;
}

function lede(d, s) {
  if (d.lede) return d.lede;
  const at = s?.schedule?.brief_at || '18:30';
  if (d.is_today && !d.stats.sessions) return `No sessions yet today. The brief is written at ${at}.`;
  if (!d.stats.sessions) return 'No sessions on this day.';
  return 'Nothing written about this day yet.';
}

export async function show(el, route, ctx) {
  last = { el, route, ctx };
  const d = await api(`/api/sec/today?${qs({ day: route.day })}`);
  if (!ctx.alive()) return;
  const s = ctx.state;
  const dc = docs(s);
  const canBrief = s?.secretary.token && s.secretary.status !== 'off' && d.brief?.status !== 'running' && d.stats.sessions > 0;
  const word = d.is_today ? ' today' : '';
  const briefBtn = canBrief
    ? `<button class="btn sm" type="button" id="briefRun">${d.brief?.status === 'ready' ? 'Write the brief again' : 'Write the brief now'}</button>`
    : '';
  const reposNote = !dc.changelog
    ? `<p class="note">${dc.set ? 'The docs folder has no changelog folder' : 'No docs folder is set'}, so Shipped comes from session digests only.</p>`
    : '';
  el.innerHTML = `
    ${head(eyebrow(d, s), d.day_label)}
    <p class="lede${d.lede_source === 'empty' ? ' dim' : ''}">${esc(lede(d, s))}</p>
    <div class="statline">
      <span><b>${d.stats.sessions}</b>sessions</span><span><b>${d.stats.shipped}</b>shipped</span><span><b id="openStat">${d.stats.open_loops}</b>open loops</span><span><b>${d.stats.decisions}</b>decisions</span>
      ${briefBtn}
    </div>
    <div class="two">
      <section class="block" aria-labelledby="h-needs">
        <h2 id="h-needs">Needs you <span class="count warn" id="needsCount">${d.needs_you_total || ''}</span></h2>
        ${list('needs', d.needs_you, 'Nothing is waiting on you.', (n) => `<li><span class="lead">${pill('p-warn', n.age_label)}</span><span class="tx">${esc(n.text)}</span><span class="meta">${n.where ? chip(n.where) : ''}${srcHtml(n.src)}</span></li>`)}
        ${d.needs_you_total > d.needs_you.length ? `<p class="note" style="margin-top:8px"><a href="#loops">All ${d.needs_you_total} waiting on you</a></p>` : ''}
      </section>
      <div style="display:grid;gap:18px">
        <section class="block" aria-labelledby="h-ship">
          <h2 id="h-ship">Shipped${word} <span class="count">${d.shipped.length || ''}</span></h2>
          ${list('shipped', d.shipped, `Nothing shipped${word}${d.is_today ? ' yet' : ''}.`, (x) => `<li><span class="lead time">${esc(x.time || '')}</span><span class="tx">${esc(x.text)}</span><span class="meta">${shipPill(x.state)}${x.repo ? chip(x.repo) : ''}${srcHtml(x.src)}</span></li>`)}
        </section>
        <section class="block" aria-labelledby="h-prog">
          <h2 id="h-prog">In progress <span class="count">${d.in_progress.length || ''}</span></h2>
          ${list('inprog', d.in_progress, 'Nothing in progress.', (x) => `<li><span class="lead">${pill('p-info', 'open')}</span><span class="tx">${esc(x.text)}</span><span class="meta">${x.where ? chip(x.where) : ''}${srcHtml(x.src)}</span></li>`)}
        </section>
      </div>
    </div>
    <section class="block" aria-labelledby="h-dec">
      <h2 id="h-dec">Decided${word} <span class="count">${d.decided.length || ''}</span></h2>
      ${list('decToday', d.decided, `No decisions recorded${word}.`, (x) => `<li><span class="lead">${x.where ? chip(x.where) : ''}</span><span class="tx">${x.is_question ? `${pill('p-warn', 'open question')} ` : ''}${esc(x.text)}</span><span class="meta">${srcHtml(x.src)}</span></li>`)}
    </section>
    <section aria-labelledby="h-repo">
      <h2 id="h-repo" class="label" style="margin-bottom:8px">Changelog entries${word}, by repo</h2>
      ${d.by_repo.length ? `<div class="repos" id="repos">${d.by_repo.map((r) => `<span class="repo${r.count ? '' : ' zero'}"><b>${r.count}</b>${esc(r.label)}</span>`).join('')}</div>` : ''}
      ${reposNote}
    </section>`;
  el.querySelector('#briefRun')?.addEventListener('click', async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    try {
      await post('/api/sec/brief/run', { day: d.day });
      ctx.toast('Writing the brief. It takes about a minute.');
      if (ctx.alive()) show(el, route, ctx);
    } catch (err) { ctx.toast(err.message); b.disabled = false; }
  });
}
