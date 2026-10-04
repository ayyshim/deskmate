// Hygiene (#hygiene): checks against the working rules (changelog entries, design status lines, memory notes).
// Report only: each warning carries a sentence to paste into a Claude session, which makes the fix.
import { api, post, esc, chip, srcHtml, levelPill, head, emptyState, fmtTime, copyText, docs } from './common.js';

let last = null;
document.addEventListener('sec_today', () => { if (last && !last.el.hidden && last.ctx.alive()) show(last.el, last.route, last.ctx); });

export async function show(el, route, ctx) {
  last = { el, route, ctx };
  const h = await api('/api/sec/hygiene');
  if (!ctx.alive()) return;
  const dc = docs(ctx.state);
  const eyebrow = 'Checks against the working rules: a changelog entry for each day with commits, design status lines that keep up, memory notes that match git. Report only: a Claude session makes the fix.';
  let body;
  if (!h.checks.length) {
    body = h.ts == null
      ? emptyState({ title: 'No checks have run yet', text: 'They run with each sweep of your docs folder and git, a few minutes apart. Run one now:', html: '<button class="btn sm" type="button" id="hySweep">Check now</button>', glyph: '…' })
      : emptyState({
        title: 'Nothing to check',
        text: dc.set ? 'The checks found no changelog, design or memory rules to hold your repos to.' : 'Most checks compare your repos with a docs folder (changelog and design), and none is set. Pick one in the setup wizard, or: <code>./deskmate config set DOCS_DIR ~/path/to/docs</code>.',
        glyph: '0',
      });
  } else {
    body = `<section class="block"><ul class="checks" id="checks">${h.checks.map((c) => `<li><span>${levelPill(c.level)}</span><span>${esc(c.text)}</span>`
      + `<span class="meta">${c.where ? `${chip(c.where)} ` : ''}${srcHtml(c.src)}${c.hand_off ? ` <button class="btn sm" type="button" data-copy="${esc(c.id)}">Copy for a session</button>` : ''}</span></li>`).join('')}</ul></section>`;
  }
  const when = h.ts ? `Last checked ${fmtTime(h.ts, ctx.state?.tz)} · ${h.counts.warn} to check, ${h.counts.ok} fine${h.counts.info ? `, ${h.counts.info} for information` : ''}` : '';
  el.innerHTML = `${head(esc(eyebrow), 'Hygiene')}${body}
    ${h.ts ? `<p class="note">${esc(when)} · <button class="src" type="button" id="hySweep">check again</button></p>` : ''}`;
  last.checks = h.checks;
  if (el.dataset.bound) return;
  el.dataset.bound = '1';
  el.addEventListener('click', async (e) => {
    const { ctx: c } = last;
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.copy) {
      const check = last.checks.find((x) => x.id === b.dataset.copy);
      if (check?.hand_off) await copyText(check.hand_off, c.toast, 'Copied. Paste it into a Claude session in that repo.');
    } else if (b.id === 'hySweep') {
      b.disabled = true;
      try {
        const r = await post('/api/sec/sweep', {});
        c.toast(r.started ? 'Checking. The results show here in a minute.' : 'A check is already running.');
      } catch (err) { c.toast(err.message); b.disabled = false; }
    }
  });
}
