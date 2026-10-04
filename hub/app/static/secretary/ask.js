// Ask (#ask): questions to the secretary about the files in the session folders, the docs folder and memory notes. The answer is
// written by a model with read-only tools; it is rendered through md(), which escapes everything first.
import { api, post, esc, srcHtml, head, md, docs, fmtTime } from './common.js';

let last = null;
const polls = new Map();   // ask id -> timer

document.addEventListener('sec_ask', (e) => {
  const id = e.detail?.id;
  if (id != null && last && !last.el.hidden && last.ctx.alive()) refreshOne(id);
});

// A citation names its line when it has one, so the same file cited at three lines does not read as a duplicate.
const citeHtml = (c) => srcHtml(c.line && !/:\d+$/.test(c.label || '') ? { ...c, label: `${c.label}:${c.line}` } : c);

function bubble(a, tz) {
  const q = `<div class="q" data-q="${a.id}">${esc(a.question)}</div>${a.origin !== 'ui' ? `<div class="q-origin">asked by ${esc(a.origin_label)}${a.ts ? ` at ${fmtTime(a.ts, tz)}` : ''}</div>` : ''}`;
  let ans;
  if (a.status === 'running') {
    ans = `<div class="a running" data-a="${a.id}" aria-busy="true"><p><span class="live" aria-hidden="true"></span> Reading the files…</p></div>`;
  } else if (a.status === 'failed') {
    // What the model wrote before it failed (rare), then the hub's one sentence on why, then a retry.
    const partial = a.answer_md ? md(a.answer_md) : '';
    const cites = partial && a.citations.length ? `<div class="cites">${a.citations.map(citeHtml).join('')}</div>` : '';
    ans = `<div class="a failed" data-a="${a.id}">${partial}${cites}<p class="err">${esc(a.error || 'The answer failed.')}</p><button class="btn sm" type="button" data-again="${a.id}" style="justify-self:start">Ask again</button></div>`;
  } else {
    const cites = a.citations.length ? `<div class="cites">${a.citations.map(citeHtml).join('')}</div>` : '';
    const meta = [a.ms ? `${(a.ms / 1000).toFixed(a.ms < 10000 ? 1 : 0)} s` : '', a.warning || ''].filter(Boolean).map(esc).join(' · ');
    ans = `<div class="a" data-a="${a.id}">${md(a.answer_md || '') || '<p class="dim">No answer text.</p>'}${cites}${meta ? `<p class="note">${meta}</p>` : ''}</div>`;
  }
  return q + ans;
}

function poll(id, started = Date.now()) {
  clearTimeout(polls.get(id));
  if (Date.now() - started > 180000) { polls.delete(id); return; }
  polls.set(id, setTimeout(async () => {
    const done = await refreshOne(id);
    if (!done) poll(id, started); else polls.delete(id);
  }, 3000));
}

async function refreshOne(id) {
  try {
    const a = await api(`/api/sec/asks/${encodeURIComponent(id)}`);
    if (!last) return true;
    const i = last.asks.findIndex((x) => x.id === a.id);
    if (i >= 0) last.asks[i] = a;
    const qn = last.el.querySelector(`[data-q="${a.id}"]`);
    const an = last.el.querySelector(`[data-a="${a.id}"]`);
    if (qn && an) {
      qn.nextElementSibling?.classList.contains('q-origin') && qn.nextElementSibling.remove();
      qn.remove();
      an.outerHTML = bubble(a, last.ctx.state?.tz);
    }
    return a.status !== 'running';
  } catch { return false; }
}

function formNote(r, state) {
  if (r.enabled) return r.warning ? `<p class="note warnline">${esc(r.warning)}</p>` : '';
  if (r.disabled_reason === 'off' || state?.secretary.status === 'off') return '<p class="note warnline">The secretary is off, so it cannot answer. Turn it on with <code>./deskmate config set SECRETARY on</code> and <code>./deskmate up</code>.</p>';
  return '<p class="note warnline">Ask needs the secretary\'s Claude login. Run <code>claude setup-token</code>, then <code>./deskmate config set-secret claude-token</code> and <code>./deskmate restart hub</code>.</p>';
}

function render(el, r, ctx) {
  const s = ctx.state;
  const dc = docs(s);
  // What Ask may read (scheduler.ask_roots): the session folders, the docs folder when it is outside them, and
  // memory notes. Its tools are Read, Grep and Glob, so git history is not among them.
  const roots = s?.sources?.roots || [];
  const docsOutside = dc.found && dc.label && !roots.some((r) => dc.label === r || dc.label.startsWith(`${r}/`));
  const reads = [...roots.map(esc), docsOutside ? esc(dc.label) : '', s?.sources?.memory ? 'memory notes' : ''].filter(Boolean);
  const eyebrow = `${esc(s?.models?.ask_label || 'Claude')} · read-only access to ${reads.length ? reads.join(', ') : 'your sessions'} · every answer cites its files`;
  const chat = [...r.asks].reverse();
  el.innerHTML = `${head(eyebrow, 'Ask the secretary')}
    ${r.next_cursor ? `<button class="btn sm loadmore" type="button" id="askOlder" data-cursor="${esc(r.next_cursor)}">Show older questions</button>` : ''}
    <div class="chat" id="chat">${chat.map((a) => bubble(a, s?.tz)).join('')}</div>
    ${chat.length ? '' : '<p class="note">Ask about any repo, feature or decision. The secretary reads, it never changes anything.</p>'}
    ${r.suggestions.length ? `<div class="sugs" id="sugs">${r.suggestions.map((q) => `<button type="button"${r.enabled ? '' : ' disabled'}>${esc(q)}</button>`).join('')}</div>` : ''}
    ${formNote(r, s)}
    <form class="askform" id="askForm"><input id="askInput" type="text" maxlength="2000" placeholder="Ask about any repo, feature or decision" aria-label="Your question"${r.enabled ? '' : ' disabled'}><button class="btn primary" type="submit"${r.enabled ? '' : ' disabled'}>Ask</button></form>`;
  for (const a of r.asks) if (a.status === 'running' && !polls.has(a.id)) poll(a.id);
}

async function ask(question) {
  const { el, ctx } = last;
  const q = question.trim();
  if (!q) return;
  if (q.length > 2000) { ctx.toast('That question is too long: 2,000 characters at most.'); return; }
  const input = el.querySelector('#askInput');
  try {
    const { ask: a } = await post('/api/sec/ask', { question: q });
    if (!last || last.el !== el) return;
    last.asks.unshift(a);
    el.querySelector('#chat').insertAdjacentHTML('beforeend', bubble(a, ctx.state?.tz));
    el.querySelector('#chat').nextElementSibling?.matches('p.note') && el.querySelector('#chat').nextElementSibling.remove();
    if (input) input.value = '';
    el.querySelector(`[data-a="${a.id}"]`)?.scrollIntoView({ block: 'nearest' });
    if (a.status === 'running') poll(a.id);
  } catch (err) {
    ctx.toast(err.status === 409 ? 'Two questions are already being answered. Ask again when one is done.' : err.message);
  }
}

export async function show(el, route, ctx) {
  const r = await api('/api/sec/asks');
  if (!ctx.alive()) return;
  last = { el, route, ctx, asks: r.asks };
  render(el, r, ctx);
  if (el.dataset.bound) return;
  el.dataset.bound = '1';
  el.addEventListener('submit', (e) => {
    if (e.target.id !== 'askForm') return;
    e.preventDefault();
    ask(el.querySelector('#askInput').value);
  });
  el.addEventListener('click', async (e) => {
    const b = e.target.closest('button');
    if (!b || !last) return;
    if (b.closest('#sugs')) { ask(b.textContent); return; }
    if (b.dataset.again) {
      const a = last.asks.find((x) => String(x.id) === b.dataset.again);
      if (a) ask(a.question);
      return;
    }
    if (b.id === 'askOlder') {
      b.disabled = true;
      try {
        const more = await api(`/api/sec/asks?cursor=${encodeURIComponent(b.dataset.cursor)}`);
        last.asks.push(...more.asks);
        el.querySelector('#chat').insertAdjacentHTML('afterbegin', [...more.asks].reverse().map((a) => bubble(a, last.ctx.state?.tz)).join(''));
        if (more.next_cursor) { b.dataset.cursor = more.next_cursor; b.disabled = false; } else b.remove();
      } catch (err) { last.ctx.toast(err.message); b.disabled = false; }
    }
  });
}
