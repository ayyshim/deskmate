/* Deskmate setup: the page ./deskmate setup opens in a browser tab.
   It renders whatever steps the setup server sends (GET /api/model: the step model in the build
   contract §8) and calls back to validate answers, run checks and install. Plain JavaScript, no
   framework, and no host but the one that served it. Secrets never come back from the server: the
   page only ever holds {set, masked}, plus whatever the user is typing into a secret field. */
(function () {
  'use strict';

  var $ = function (sel, root) { return (root || document).querySelector(sel); };

  var ICON = {
    ok: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3 7.4l2.6 2.6L11 4.6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    bad: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M4 4l6 6M10 4l-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    warn: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M7 2.8v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="7" cy="10.9" r="1.25" fill="currentColor"/></svg>',
    info: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="3.2" r="1.25" fill="currentColor"/><path d="M7 6.2v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    wait: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M4.5 7h5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
  };
  var ICON_WORD = { ok: 'OK', bad: 'Problem', warn: 'Needs a look', info: 'Note', wait: 'Not checked yet', run: 'Running' };
  var SPIN = '<span class="spin" aria-hidden="true"></span>';
  // Headings when the model gives none (info.heading); the stepper uses each step's own title.
  var HEADINGS = {
    check: 'Check this computer', you: 'About you', desk: 'The desk', secretary: 'The secretary',
    sessions: 'Which sessions it reads', docs: 'Notes and docs', notes: 'Notes and docs', notify: 'Notifications',
    habits: 'Working habits', connect: 'Connect Claude Code', review: 'Review and install', done: 'Deskmate is running'
  };
  var ADV_TITLE = { desk: 'Ports and data folder', secretary: 'Models, limits and times', docs: 'Files it never reads' };
  var PREVIEW_TITLE = { connect: 'What changes in Claude Code', habits: 'What setup writes' };
  // Actions a step offers when the model doesn't list them in info.actions.
  var STEP_ACTIONS = {
    connect: [{ name: 'preview_connect', label: 'Show the changes', auto: true }],
    habits: [{ name: 'preview_habits', label: 'Show the changes', auto: true }]
  };
  var PHASES = ['Save settings', 'Prepare folders', 'Build images', 'Start Deskmate', 'Connect Claude Code', 'Working habits', 'Health check'];
  var PHASE_NOTE = {
    'Save settings': '.env, which only you can read, and compose.local.yaml',
    'Prepare folders': 'The data folder and the secret files',
    'Build images': 'The desk and the hub: about 2.6 GB the first time',
    'Start Deskmate': 'On 127.0.0.1, and again whenever Docker starts',
    'Connect Claude Code': 'The deskmate MCP server and plugin, for every session',
    'Working habits': 'Rules, skills and the end-of-turn check',
    'Health check': './deskmate doctor'
  };
  var DATA_LABEL = { changes: 'Changes', conflicts: 'Found', diffs: 'Files that change', files: 'Files it creates', settings: 'Claude Code settings', lint: 'Agent checks', notes: '', warnings: '' };
  var THEMES = ['auto', 'light', 'dark'];

  var S = {
    model: null,      // the last model from the server
    values: {},       // the answers; a secret is {set, masked}
    saved: null,      // the answers when the page loaded: edit mode lists changes against these
    step: null,
    seen: {},
    errors: {},       // KEY -> message from the last validation
    dirty: {},        // KEY -> true once the user changed it (hides a stale field.error)
    sec: {},          // secret KEY -> {edit, msg, state}
    typed: {},        // secret KEY -> what the user is typing; never part of S.values
    other: {},        // path KEY -> "Another folder" is chosen
    otherVal: {},
    open: {},         // step id -> its advanced section is open
    preview: {},      // step id -> the last preview action's result
    stepMsg: {},      // step id -> {level, text}
    run: null,        // the install run this page follows
    busy: '',
    queued: false,    // Continue was pressed while busy
    confirm: false,
    ended: null,
    checkedAt: null,
    lastPing: 0
  };

  // ------------------------------------------------------------ small helpers

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  // Texts from the model may carry `code` and **bold**; everything else is escaped.
  function fmt(s) {
    if (s == null || s === '') return '';
    return esc(s).replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  }
  function plain(s) { return String(s == null ? '' : s).replace(/`([^`\n]+)`/g, '$1').replace(/\*\*([^*\n]+)\*\*/g, '$1'); }
  function fid(key) { return 'f-' + String(key).replace(/[^A-Za-z0-9_-]/g, '_'); }
  function ids() { return Array.prototype.slice.call(arguments).filter(Boolean).join(' '); }
  function clone(o) { return JSON.parse(JSON.stringify(o == null ? null : o)); }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function norm(v) {
    if (v === true) return 'on';
    if (v === false) return 'off';
    var s = String(v == null ? '' : v).trim().toLowerCase();
    if (['on', 'true', '1', 'yes'].indexOf(s) >= 0) return 'on';
    if (['off', 'false', '0', 'no', ''].indexOf(s) >= 0 && typeof v !== 'number') return s === '' ? '' : 'off';
    return String(v);
  }
  function isOn(v) { return norm(v) === 'on'; }
  function asList(v, sep) {
    if (Array.isArray(v)) return v.map(String).filter(function (x) { return x.trim(); });
    return String(v == null ? '' : v).split(sep || ':').map(function (x) { return x.trim(); }).filter(Boolean);
  }
  function home() { return String(S.values.HOST_HOME || '').replace(/\/+$/, ''); }
  function tilde(p) {
    p = String(p == null ? '' : p);
    var h = home();
    return h && h !== '/' && (p === h || p.indexOf(h + '/') === 0) ? '~' + p.slice(h.length) : p;
  }
  function untilde(p) {
    p = String(p || '').trim();
    var h = home();
    return h && (p === '~' || p.indexOf('~/') === 0) ? h + p.slice(1) : p;
  }
  function below(child, parent) { return parent === '/' ? child !== '/' : child.indexOf(parent + '/') === 0; }
  function wmsg(level, html, id, extra) {
    var icon = level === 'busy' ? SPIN : ICON[level] || ICON.info;
    return '<div class="wz-msg ' + (level === 'busy' ? 'info busy' : level) + '"' + (id ? ' id="' + id + '"' : '') + (extra || '') + '>' + icon + '<div>' + html + '</div></div>';
  }
  function levelOf(status) { return status === 'fail' ? 'bad' : status === 'warn' ? 'warn' : status === 'ok' ? 'ok' : 'info'; }
  function clock(d) { return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2); }
  function seconds(ts) {
    if (typeof ts === 'number') return ts > 1e12 ? ts / 1000 : ts;
    var t = Date.parse(ts);
    return isNaN(t) ? null : t / 1000;
  }
  function took(s) {
    if (s == null || s < 0) return '';
    if (s < 10) return (Math.round(s * 10) / 10) + ' s';
    if (s < 60) return Math.round(s) + ' s';
    return Math.floor(s / 60) + ' min ' + Math.round(s % 60) + ' s';
  }
  function announce(text) {
    var el = $('#announce');
    el.textContent = '';
    setTimeout(function () { el.textContent = text; }, 30);
  }
  var toastTimer = null;
  function toast(text) {
    var el = $('#toast');
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 2600);
  }

  // ------------------------------------------------------------ the model

  function steps() { return (S.model && S.model.steps) || []; }
  function stepById(id) { var L = steps(); for (var i = 0; i < L.length; i++) if (L[i].id === id) return L[i]; return null; }
  function stepIndex(id) { var L = steps(); for (var i = 0; i < L.length; i++) if (L[i].id === id) return i; return -1; }
  function cur() { return stepById(S.step); }
  function edit() { return !!(S.model && S.model.mode === 'edit'); }
  function isSkipped(st) { return !!(st && (st.skip || st.skipped || st.status === 'skip')); }
  function nextId(id, dir) {
    var L = steps(), i = stepIndex(id);
    do { i += dir; } while (i >= 0 && i < L.length && isSkipped(L[i]));
    return i >= 0 && i < L.length ? L[i].id : null;
  }
  function allFields() {
    var out = [];
    steps().forEach(function (st) { (st.fields || []).forEach(function (f) { out.push({ f: f, st: st }); }); });
    return out;
  }
  function fieldOf(key) { var a = allFields(); for (var i = 0; i < a.length; i++) if (a[i].f.key === key) return a[i]; return null; }
  function platformWord() { var p = (S.model && S.model.platform) || {}; return p.os === 'macos' ? 'this Mac' : 'this computer'; }
  function repo() { var r = stepById('review'); return (S.model && S.model.repo) || (r && r.info && r.info.repo) || ''; }

  function applyModel(m) {
    S.model = m;
    var vals = {};
    Object.keys(m.values || {}).forEach(function (k) { vals[k] = m.values[k]; });
    allFields().forEach(function (x) { if (!(x.f.key in vals) && x.f.value !== undefined) vals[x.f.key] = x.f.value; });
    S.values = vals;
    applySecrets(m.secrets);
    if (S.saved === null) S.saved = clone(vals);
  }
  function applySecrets(secrets) {
    Object.keys(secrets || {}).forEach(function (k) { S.values[k] = secrets[k]; });
  }
  function replaceStep(st) {
    if (!st || !S.model) return;
    var L = steps();
    for (var i = 0; i < L.length; i++) if (L[i].id === st.id) L[i] = st;
  }
  // The step's on/off switch (SECRETARY on the secretary step, HABITS on habits) hides the rest while off.
  function mainSwitch(st) {
    var L = (st && st.fields) || [];
    for (var i = 0; i < L.length; i++) {
      var f = L[i];
      if (f.type === 'bool' && !f.advanced && (f.role === 'switch' || String(f.key).toLowerCase() === String(st.id).toLowerCase())) return f;
    }
    return null;
  }
  function visible(f, st) {
    if (f.hidden) return false;
    if (f.show_if && typeof f.show_if === 'object') {
      for (var k in f.show_if) {
        var want = [].concat(f.show_if[k]).map(norm);
        if (want.indexOf(norm(S.values[k])) < 0) return false;
      }
    }
    var sw = mainSwitch(st);
    return !(sw && f !== sw && !isOn(S.values[sw.key]));
  }
  function errFor(f) { return S.errors[f.key] || (!S.dirty[f.key] && f.error) || ''; }
  function secretAction(f, st) { return f.action || (st.id === 'secretary' ? 'check_token' : st.id === 'notify' ? 'test_notify' : ''); }
  function stepActions(st) { return (st.info && st.info.actions) || STEP_ACTIONS[st.id] || []; }
  function blockers() {
    var c = stepById('check');
    return c ? (c.checks || []).filter(function (x) { return x.status === 'fail'; }) : [];
  }

  // A value as the user typed it, kept in the type the model used (8080 stays a number, "on" stays "on").
  function coerce(f, raw) {
    var curv = S.values[f.key];
    if (f.type === 'bool') {
      var b = norm(raw) === 'on';
      if (typeof curv === 'boolean') return b;
      if (typeof curv === 'number') return b ? 1 : 0;
      var s = String(curv == null ? '' : curv).toLowerCase();
      if (s === 'true' || s === 'false') return b ? 'true' : 'false';
      if (s === 'yes' || s === 'no') return b ? 'yes' : 'no';
      if (s === '1' || s === '0') return b ? '1' : '0';
      return b ? 'on' : 'off';
    }
    if (f.type === 'int' || typeof curv === 'number') {
      var t = String(raw).trim();
      return t !== '' && /^-?\d+$/.test(t) ? parseInt(t, 10) : raw;
    }
    return raw;
  }
  function setList(f, list, sep) {
    S.values[f.key] = Array.isArray(S.values[f.key]) ? list : list.join(sep);
  }
  function setVal(key, raw) {
    var x = fieldOf(key);
    S.values[key] = x ? coerce(x.f, raw) : raw;
    S.dirty[key] = true;
    delete S.errors[key];
  }
  function display(f, v) {
    if (f.type === 'secret') return v && v.set ? (v.masked || 'set') : 'not set';
    if (f.type === 'bool') return isOn(v) ? 'On' : 'Off';
    if (f.type === 'multi') {
      var picked = asList(v, Array.isArray(v) ? null : ',');
      return picked.map(function (x) { var c = choice(f, x); return c ? plain(c.label) : x; }).join(', ') || 'none';
    }
    if (f.type === 'folders' || f.type === 'paths') return asList(v, ':').map(tilde).join(', ') || 'none';
    var c = choice(f, v);
    if (c) return plain(c.label) || (f.type === 'path' ? tilde(c.value) : String(c.value));
    if (f.type === 'path') return v ? tilde(v) : 'none';
    return v === '' || v == null ? '—' : String(v);
  }
  function choice(f, v) {
    var L = f.choices || [];
    for (var i = 0; i < L.length; i++) if (String(L[i].value) === String(v == null ? '' : v)) return L[i];
    return null;
  }
  function choiceKind(f) {
    var L = f.choices || [];
    if (f.display) return f.display;
    // A few one-word choices read best as a segmented control; the picked one's detail shows under it.
    if (L.length <= 4 && L.every(function (c) { return !c.disabled && plain(c.label == null ? c.value : c.label).length <= 8; })) return 'seg';
    if (L.some(function (c) { return c.detail; })) return 'cards';
    return 'select';
  }
  function isCards(f) {
    if (f.readonly) return false;
    if (f.type === 'choice' || (f.type === 'int' && (f.choices || []).length)) return choiceKind(f) === 'cards';
    return f.type === 'multi' || f.type === 'folders' || (f.type === 'path' && (f.choices || []).length > 0);
  }

  // ------------------------------------------------------------ the server

  var LOST = 'Setup has stopped, or this tab lost contact with it. If the terminal still shows setup running, reload this page. Otherwise run ./deskmate setup again.';
  function api(method, path, body) {
    var opt = { method: method, credentials: 'same-origin', cache: 'no-store', headers: {} };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return { ok: r.ok, status: r.status, data: data || {} }; });
    }, function () { return { ok: false, status: 0, data: { error: LOST } }; });
  }
  // Errors after which this page can't go on: no session, setup ended, or the server is gone.
  function fatal(res) {
    if (res.status === 401) return end('This tab isn’t part of setup', 'warn', res.data.error || LOST);
    if (res.status === 410 || res.status === 0) return end('Setup isn’t running', 'warn', res.data.error || LOST);
    return false;
  }
  function end(title, level, text) {
    S.ended = { title: title, level: level, text: text };
    render(null);
    return true;
  }
  function refreshModel() {
    var before = clone(S.values);
    return api('POST', '/api/model', { values: S.values }).then(function (res) {
      if (!res.ok) { fatal(res); return false; }
      var after = clone(S.values);
      applyModel(res.data);
      Object.keys(after).forEach(function (k) { if (!same(after[k], before[k])) S.values[k] = after[k]; });
      return true;
    });
  }
  function ping() {
    var now = Date.now();
    if (now - S.lastPing < 60000 || S.ended) return;
    S.lastPing = now;
    api('POST', '/api/ping', {});
  }

  // ------------------------------------------------------------ rendering

  function render(focusId) {
    var active = document.activeElement, keep = null;
    if (focusId === undefined && active && active.id) keep = { id: active.id, s: active.selectionStart, e: active.selectionEnd };
    if (S.ended) { renderEnded(); return; }
    if (!S.model || !S.step) return;
    $('#topMode').textContent = edit() ? 'Settings' : 'Setup';
    $('#topWhere').textContent = location.host;
    renderSide();
    renderMain();
    restoreTyped();
    paintRun();
    var target = focusId || (keep && keep.id);
    var el = target && document.getElementById(target);
    if (el) {
      el.focus({ preventScroll: true });
      if (keep && keep.id === target && typeof keep.s === 'number' && el.setSelectionRange && /^(text|password|search|url)$/.test(el.type || '')) {
        try { el.setSelectionRange(keep.s, keep.e); } catch (_) { /* some inputs refuse */ }
      }
    }
  }
  function restoreTyped() {
    Object.keys(S.typed).forEach(function (k) { var el = document.getElementById(fid(k)); if (el && el.type === 'password') el.value = S.typed[k]; });
    var adv = document.querySelectorAll('details.adv');
    Array.prototype.forEach.call(adv, function (d) { d.addEventListener('toggle', function () { S.open[d.getAttribute('data-step')] = d.open; }); });
  }
  function focusHeading() { var h = $('#wzH'); if (h) h.focus({ preventScroll: true }); }

  function stepClass(st) {
    if (isSkipped(st)) return 'skip';
    var r = S.run;
    if (st.id === 'review') return r && r.done ? (r.ok ? 'done' : 'bad') : '';
    if (st.id === 'done') return r && r.done && r.ok ? 'done' : '';
    if (st.status === 'fail') return 'bad';
    if (st.status === 'warn' && (edit() || S.seen[st.id] || st.id === 'check')) return 'warn';
    if (st.status === 'ok' && st.id !== S.step && (edit() || S.seen[st.id])) return 'done';
    return '';
  }
  function stepSub(st) {
    var r = S.run;
    if (st.id === 'review') {
      if (r && !r.done) return 'Installing…';
      if (r && r.done) return r.ok ? (edit() ? 'Applied' : 'Installed') : 'Stopped: retry';
      if (edit()) { var n = changes().length; return n ? n + (n > 1 ? ' changes' : ' change') : 'No changes'; }
    }
    return plain(st.summary || '');
  }
  function renderSide() {
    var words = { skip: 'skipped', warn: 'needs a look', bad: 'problem', done: 'done' };
    $('#wzSteps').innerHTML = steps().map(function (st, i) {
      var c = stepClass(st), here = st.id === S.step;
      var mark = !here && c === 'done' ? ICON.ok : !here && c === 'bad' ? ICON.warn : String(i + 1);
      return '<li><a href="#' + esc(st.id) + '" class="' + c + '"' + (here ? ' aria-current="step"' : '') + '><span class="wz-n" aria-hidden="true">' + mark +
        '</span><span class="wz-t">' + esc(st.title) + (c && !here ? '<span class="vh"> (' + words[c] + ')</span>' : '') +
        '</span><span class="wz-s">' + esc(stepSub(st)) + '</span></a></li>';
    }).join('');
    var ol = $('#wzSteps'), ca = ol.querySelector('[aria-current="step"]');
    if (ca && ol.scrollWidth > ol.clientWidth) ol.scrollLeft += ca.getBoundingClientRect().left - ol.getBoundingClientRect().left - (ol.clientWidth - ca.offsetWidth) / 2;
    $('#wzSideCard').innerHTML = sideCard();
  }
  function sideCard() {
    var r = S.run, rp = repo(), where = rp ? ' in <code>' + esc(tilde(rp)) + '</code>' : '';
    if (r && !r.done) return '<p><b>' + (edit() ? 'Applying.' : 'Installing.') + '</b> Keep the terminal open until it finishes. You can close this tab and open the link again: the log picks up where it is.</p>';
    if (r && r.done && r.ok) return '<p><b>' + (edit() ? 'Applied.' : 'Installed.') + '</b> Your answers are in <code>.env</code>' + where + '. Only you can read it.</p><p>Change them any time: run <code>./deskmate setup</code> again.</p>';
    if (edit()) return '<p>Your settings live in <code>.env</code>' + where + '. Only you can read it.</p><p>Nothing changes until you press <b>Apply changes</b>.</p><button type="button" class="btn sm" data-act="cancel">Close without changes</button>';
    var n = stepIndex('review') + 1;
    return '<p>Nothing on ' + platformWord() + ' changes until you press <b>Install</b>' + (n ? ' on step ' + n : '') + '.</p><p>Your answers stay with setup until then. Closing this tab keeps them while the terminal runs.</p><button type="button" class="btn sm" data-act="cancel">Cancel setup</button>';
  }

  function renderMain() {
    var st = cur(), i = stepIndex(st.id), ed = edit();
    var heading = (st.info && st.info.heading) || HEADINGS[st.id] || st.title;
    if (ed && st.id === 'review') heading = 'Settings';
    if (st.id === 'done' && !(S.run && S.run.done && S.run.ok)) heading = st.title || 'Done';
    else if (ed && st.id === 'done') heading = 'Applied';
    var lede = st.intro || '';
    if (st.id === 'done' && !(S.run && S.run.done && S.run.ok)) lede = '';
    var eyebrow = ed ? 'Settings · ./deskmate setup · ' + location.host : 'Step ' + (i + 1) + ' of ' + steps().length + ' · ./deskmate setup · ' + location.host;
    var head = '<div class="wz-head"><p class="eyebrow">' + esc(eyebrow) + '</p><h1 class="h1" id="wzH" tabindex="-1">' + esc(heading) + '</h1>' + (lede ? '<p class="lede">' + fmt(lede) + '</p>' : '') + '</div>';
    var banner = ed && (st.id === 'review' || st.id === 'check') ? '<div class="wz-banner" role="note"><p><b>Deskmate is already installed here.</b> Setup opened with your current settings. Change what you need, then apply on the last step. Only what changed is rebuilt or restarted.</p></div>' : '';
    var body;
    if (isSkipped(st)) body = skippedBody(st);
    else if (st.id === 'check') body = checkBody(st);
    else if (st.id === 'review') body = reviewBody(st);
    else if (st.id === 'done') body = doneBody(st);
    else body = genericBody(st);
    $('#wzMain').innerHTML = '<form class="wz-form" id="wzForm" novalidate>' + head + banner + body + footer(st) + '</form>';
  }

  function footer(st) {
    var r = S.run, ed = edit(), prev = nextId(st.id, -1), left = '', note = '', right = '';
    if (S.confirm) {
      var after = r && r.done && !r.ok;
      return '<div class="wz-foot"><div class="wz-confirm" role="group" aria-labelledby="cc-l"><p id="cc-l"><b>' + (ed ? 'Close setup?' : 'Stop setup?') + '</b> ' +
        (after ? 'Install didn’t finish. Run <code>./deskmate setup</code> again later to retry.' : 'Nothing on ' + platformWord() + ' has changed.') +
        '</p><div class="wz-row"><button type="button" class="btn" id="cc-yes" data-act="cancel-yes">' + (ed ? 'Close setup' : 'Stop setup') + '</button><button type="button" class="btn" id="cc-no" data-act="cancel-no">Keep going</button></div></div></div>';
    }
    if (ed && st.id !== 'review') left = '<a class="btn" href="#review">All settings</a>';
    else if (prev && st.id !== 'check') left = '<button type="button" class="btn" data-act="back">Back</button>';
    if (st.id === 'check' && blockers().length) note = 'You can carry on. Install waits until this is fixed.';
    if (st.id === 'done' && r && r.ok) note = 'You can close this tab. The terminal shows the same link.';
    if (ed && st.id !== 'review' && st.id !== 'done') note = 'Nothing is applied until you press Apply changes.';
    if (st.id === 'review') right = r && r.done && r.ok ? '<button type="submit" class="btn primary">Continue</button>' : '';
    else if (st.id === 'done') right = r && r.done && r.ok ? '<button type="button" class="btn primary" id="btn-finish" data-act="finish">Finish setup</button>' : '';
    else right = '<button type="submit" class="btn primary" id="btn-continue"' + (S.busy === 'continue' ? ' disabled' : '') + '>' + (S.busy === 'continue' ? SPIN : '') + 'Continue</button>';
    var phone = !(r && !r.done) && st.id !== 'done' ? '<button type="button" class="btn quiet phone" data-act="cancel">' + (ed ? 'Close' : 'Cancel setup') + '</button>' : '';
    return '<div class="wz-foot">' + left + '<span class="note">' + esc(note) + '</span>' + phone + right + '</div>';
  }

  // ------------------------------------------------------------ steps

  function skippedBody(st) {
    var btn = !isOn(S.values.SECRETARY) && 'SECRETARY' in S.values && fieldOf('SECRETARY') ? '<div class="wz-row"><button type="button" class="btn sm" data-act="secretary-on">Turn the secretary on</button></div>' : '';
    return wmsg('info', '<b>' + fmt(st.summary || 'Skipped.') + '</b> Nothing to answer here.' + btn);
  }

  function messages(st, withChecks) {
    var out = [];
    if (withChecks) (st.checks || []).forEach(function (c) {
      out.push(wmsg(c.status === 'ok' ? 'info' : levelOf(c.status), '<b>' + fmt(c.label) + '</b> ' + fmt(c.detail) + (c.fix ? ' ' + fixHtml(c.fix) : '')));
    });
    ((st.info && st.info.messages) || []).forEach(function (m) { out.push(wmsg(m.level || 'info', fmt(m.text))); });
    var keys = {};
    (st.fields || []).forEach(function (f) { if (visible(f, st)) keys[f.key] = 1; });
    Object.keys(S.errors).forEach(function (k) {
      var x = fieldOf(k);
      if (!keys[k] && (!x || x.st.id === st.id)) out.push(wmsg('bad', fmt(S.errors[k])));
    });
    var m = S.stepMsg[st.id];
    if (m) out.push(wmsg(m.level, fmt(m.text)));
    return out.length ? '<div class="msgs" role="status">' + out.join('') + '</div>' : '';
  }
  function fixHtml(text) {
    var m = /`([^`\n]+)`/.exec(text || '');
    var cmd = m && /\s/.test(m[1]) ? m[1] : '';
    return fmt(text) + (cmd ? '<div class="wz-row"><button type="button" class="btn sm" data-act="copy" data-copy="' + esc(cmd) + '">Copy the command</button></div>' : '');
  }

  function genericBody(st) {
    return messages(st, true) + fieldBlocks(st) + advanced(st) + previewBlock(st) + infoNotes(st);
  }
  function infoNotes(st) {
    var info = st.info || {}, out = '';
    if (info.line && ['check', 'sessions', 'review', 'done'].indexOf(st.id) < 0) out += '<p class="note">' + fmt(info.line) + '</p>';
    else if (info.mounts && info.mounts.length) out += '<p class="note">Deskmate sees only these, read-only: ' + info.mounts.map(function (p) { return '<code>' + esc(tilde(p)) + '</code>'; }).join(', ') + '.</p>';
    (info.notes || []).forEach(function (n) { out += '<p class="note">' + fmt(n) + '</p>'; });
    var sw = mainSwitch(st);
    if (info.after && !(sw && !isOn(S.values[sw.key]))) out += '<p class="note">' + fmt(info.after) + '</p>';
    if (info.footer) out += '<p class="note">' + fmt(info.footer) + '</p>';
    return out;
  }

  function fieldBlocks(st) {
    var fields = (st.fields || []).filter(function (f) { return !f.advanced && visible(f, st); });
    if (!fields.length) return '';
    var blocks = [], run = [];
    fields.forEach(function (f) {
      var big = isCards(f);
      if (big && run.length) { blocks.push(run); run = []; }
      run.push(f);
      if (big) { blocks.push(run); run = []; }
    });
    if (run.length) blocks.push(run);
    return blocks.map(function (b) { return '<section class="block stack">' + layoutFields(b, st) + '</section>'; }).join('');
  }
  function layoutKind(f, st) {
    if (f.readonly) return 'wide';
    if (f.type === 'bool' && f !== mainSwitch(st)) return 'bool';
    if (['text', 'int', 'time', 'model'].indexOf(f.type) >= 0 && !(f.choices && f.choices.length && choiceKind(f) !== 'select')) return 'short';
    if (f.type === 'choice' && choiceKind(f) === 'select') return 'short';
    return 'wide';
  }
  function layoutFields(fields, st) {
    var out = [], run = [], kind = '';
    function flush() {
      if (!run.length) return;
      if (kind === 'short' && run.length > 1) out.push('<div class="fgrid">' + run.join('') + '</div>');
      else if (kind === 'bool') out.push('<div class="cbxs">' + run.join('') + '</div>');
      else out.push(run.join(''));
      run = [];
    }
    fields.forEach(function (f) {
      var k = layoutKind(f, st);
      if (k !== kind || k === 'wide') flush();
      kind = k;
      run.push(fieldHtml(f, st) + (f.warning && !errFor(f) ? '<div class="msgs">' + wmsg('warn', fmt(f.warning)) + '</div>' : ''));
    });
    flush();
    return out.join('');
  }
  function advanced(st) {
    var fields = (st.fields || []).filter(function (f) { return f.advanced && visible(f, st); });
    if (!fields.length) return '';
    var open = S.open[st.id] || fields.some(function (f) { return errFor(f); });
    var title = (st.info && st.info.advanced_title) || ADV_TITLE[st.id] || 'More settings';
    var note = fields.filter(function (f) { return f.type !== 'secret' && f.type !== 'folders'; })
      .map(function (f) { var d = shortValue(f); return d.length > 28 ? '' : d; }).filter(Boolean).slice(0, 3).join(' · ');
    return '<details class="adv" id="adv-' + esc(st.id) + '" data-step="' + esc(st.id) + '"' + (open ? ' open' : '') + '><summary><span class="car" aria-hidden="true">▸</span>' +
      esc(title) + '<span class="note">' + esc(note) + '</span></summary><div>' + layoutFields(fields, st) + '</div></details>';
  }

  // ------------------------------------------------------------ fields

  function helpHtml(f, id) { return f.help ? '<p class="help" id="' + id + '">' + fmt(f.help) + '</p>' : ''; }
  function errHtml(err, id) { return err ? wmsg('bad', fmt(err), id) : ''; }
  function chipsHtml(f) { return f.chips && f.chips.length ? '<div class="chips mt-s">' + f.chips.map(function (c) { return '<span class="chip">' + esc(c) + '</span>'; }).join('') + '</div>' : ''; }

  function fieldHtml(f, st) {
    if (f.readonly) return readonlyField(f);
    var hasChoices = f.choices && f.choices.length;
    switch (f.type) {
      case 'bool': return f === mainSwitch(st) ? switchField(f, st) : boolField(f);
      case 'int': return hasChoices ? choiceField(f) : inputField(f, 'number');
      case 'choice': return hasChoices ? choiceField(f) : inputField(f, 'text');
      case 'model': return hasChoices ? selectField(f) : inputField(f, 'text');
      case 'multi': return multiField(f);
      case 'path': return hasChoices ? pathChoiceField(f) : inputField(f, 'text');
      case 'paths': return pathsField(f);
      case 'secret': return secretField(f, st);
      case 'time': return inputField(f, 'time');
      case 'folders': return foldersField(f, st);
      default: return inputField(f, 'text');
    }
  }
  function inputField(f, type) {
    var id = fid(f.key), err = errFor(f), v = S.values[f.key];
    var mono = type !== 'text' || f.type === 'path' || f.type === 'model';
    var short = type === 'number' || type === 'time';
    var desc = ids(f.help && id + '-h', err && id + '-e');
    return '<div class="fld"><label for="' + id + '">' + esc(f.label) + '</label><input class="inp' + (mono ? ' mono' : '') + (short ? ' short' : '') +
      '" id="' + id + '" type="' + type + '" data-key="' + esc(f.key) + '" value="' + esc(v == null ? '' : v) + '"' +
      (type === 'number' ? ' inputmode="numeric"' : '') + (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : '') +
      (desc ? ' aria-describedby="' + desc + '"' : '') + (err ? ' aria-invalid="true"' : '') + ' autocomplete="off" spellcheck="false">' +
      helpHtml(f, id + '-h') + chipsHtml(f) + errHtml(err, id + '-e') + '</div>';
  }
  function readonlyField(f) {
    var id = fid(f.key), v = S.values[f.key], items;
    if (f.type === 'path' || f.type === 'paths' || f.type === 'folders') items = asList(v, ':').map(function (p) { return '<span class="cmd">' + esc(tilde(p)) + '</span>'; });
    else items = ['<span class="cmd">' + esc(display(f, v)) + '</span>'];
    return '<div class="fld"><span class="lbl" id="' + id + '-l">' + esc(f.label) + '</span><div class="wz-row" role="group" aria-labelledby="' + id + '-l">' +
      (items.join('') || '<span class="note">none</span>') + '</div>' + helpHtml(f, id + '-h') + '</div>';
  }
  function selectField(f) {
    var id = fid(f.key), err = errFor(f), v = S.values[f.key], L = f.choices || [];
    var opts = L.map(function (c) { return '<option value="' + esc(c.value) + '"' + (String(c.value) === String(v) ? ' selected' : '') + '>' + esc(plain(c.label == null ? c.value : c.label)) + '</option>'; });
    if (v != null && v !== '' && !choice(f, v)) opts.push('<option value="' + esc(v) + '" selected>' + esc(v) + '</option>');
    var desc = ids(f.help && id + '-h', err && id + '-e');
    return '<div class="fld"><label for="' + id + '">' + esc(f.label) + '</label><select class="inp" id="' + id + '" data-key="' + esc(f.key) + '"' +
      (desc ? ' aria-describedby="' + desc + '"' : '') + (err ? ' aria-invalid="true"' : '') + '>' + opts.join('') + '</select>' +
      helpHtml(f, id + '-h') + errHtml(err, id + '-e') + '</div>';
  }
  function segField(f, items, detail) {
    var id = fid(f.key), err = errFor(f), v = S.values[f.key];
    var eq = f.type === 'bool' ? function (a, b) { return norm(a) === norm(b); } : function (a, b) { return String(a) === String(b == null ? '' : b); };
    var desc = ids(f.help && id + '-h', err && id + '-e');
    return '<div class="fld"><span class="lbl" id="' + id + '-l">' + esc(f.label) + '</span><div class="seg" role="group" aria-labelledby="' + id + '-l"' + (desc ? ' aria-describedby="' + desc + '"' : '') + '>' +
      items.map(function (it, j) {
        return '<button type="button" id="' + id + '-' + j + '" data-act="seg" data-key="' + esc(f.key) + '" data-val="' + esc(it.value) + '" aria-pressed="' + eq(it.value, v) + '">' + esc(it.label) + '</button>';
      }).join('') + '</div>' + (detail ? '<p class="note" id="' + id + '-c" aria-live="polite">' + fmt(detail) + '</p>' : '') + helpHtml(f, id + '-h') + errHtml(err, id + '-e') + '</div>';
  }
  function switchField(f) { return segField(f, [{ value: 'on', label: 'On' }, { value: 'off', label: 'Off' }]); }
  function choiceField(f) {
    var k = choiceKind(f);
    if (k === 'cards') return cardsField(f);
    if (k === 'seg') {
      var c = choice(f, S.values[f.key]);
      return segField(f, f.choices.map(function (c) { return { value: c.value, label: plain(c.label == null ? c.value : c.label) }; }), c && c.detail);
    }
    return selectField(f);
  }
  function card(f, c, j, on, title, extra) {
    var id = fid(f.key) + '-' + j, dis = !!c.disabled;
    return '<label class="opt' + (on ? ' is-on' : '') + (dis ? ' is-off' : '') + '"><input type="radio" name="' + fid(f.key) + '" id="' + id + '" value="' + esc(c.value) +
      '" data-key="' + esc(f.key) + '"' + (c.other ? ' data-other="1"' : '') + ' aria-labelledby="' + id + '-t" aria-describedby="' + id + '-d"' + (on ? ' checked' : '') + (dis ? ' disabled' : '') +
      '><span class="ot" id="' + id + '-t">' + title + '</span><span class="od" id="' + id + '-d">' + fmt(c.detail) +
      (on && c.layout ? layoutList(c.layout) : '') + (on && c.note ? '<p>' + fmt(c.note) + '</p>' : '') + (extra || '') + '</span></label>';
  }
  function layoutList(items) {
    return '<ul class="layout">' + items.map(function (r) {
      return '<li><span>' + ICON.ok + '<span class="vh">Found: </span></span><span><b>' + esc(r.label) + '</b> ' + (r.path ? '<code>' + esc(r.path) + '</code> ' : '') + (r.detail ? '· ' + fmt(r.detail) : '') + '</span></li>';
    }).join('') + '</ul>';
  }
  function cardTitle(c) { return fmt(c.label == null ? c.value : c.label) + (c.badge ? ' <span class="pill p-ok">' + esc(c.badge) + '</span>' : ''); }
  function cardsField(f) {
    var id = fid(f.key), err = errFor(f), v = S.values[f.key];
    return '<fieldset class="fld"' + (err ? ' aria-describedby="' + id + '-e"' : '') + '><legend>' + esc(f.label) + '</legend>' + helpHtml(f, id + '-h') + '<div class="opts">' +
      f.choices.map(function (c, j) { return card(f, c, j, String(c.value) === String(v == null ? '' : v), cardTitle(c)); }).join('') +
      '</div>' + errHtml(err, id + '-e') + '</fieldset>';
  }
  // A folder picked from what setup found, another folder typed in, or none.
  function pathChoiceField(f) {
    var id = fid(f.key), err = errFor(f), v = String(S.values[f.key] == null ? '' : S.values[f.key]);
    var L = f.choices || [], none = choice(f, ''), known = !!choice(f, v) && !S.other[f.key];
    var cards = L.filter(function (c) { return String(c.value) !== ''; }).map(function (c, j) {
      var t = '<code>' + esc(tilde(c.value)) + '</code>' + (c.label ? ' <span class="pill p-info">' + esc(plain(c.label)) + '</span>' : '');
      return card(f, c, j, known && String(c.value) === v, t);
    });
    var otherVal = known ? (S.otherVal[f.key] || '') : v;
    var otherIn = !known ? '<span class="wz-row"><input class="inp mono" id="' + id + '-other" data-key="' + esc(f.key) + '" data-path-other="1" value="' + esc(tilde(otherVal)) +
      '" placeholder="' + esc(home() ? '~/work/notes' : '/path/to/folder') + '" aria-label="Path of the folder" autocomplete="off" spellcheck="false"' +
      (err ? ' aria-invalid="true" aria-describedby="' + id + '-e"' : '') + '></span>' : '';
    cards.push(card(f, { value: '', other: true, detail: known ? 'Type its path.' : '' }, 'o', !known, 'Another folder', otherIn));
    if (none) cards.push(card(f, none, 'n', known && v === '', fmt(none.label || 'None')));
    return '<fieldset class="fld"' + (err ? ' aria-describedby="' + id + '-e"' : '') + '><legend>' + esc(f.label) + '</legend>' + helpHtml(f, id + '-h') +
      '<div class="opts">' + cards.join('') + '</div>' + errHtml(err, id + '-e') + '</fieldset>';
  }
  function multiField(f) {
    var id = fid(f.key), err = errFor(f), v = S.values[f.key], sel = asList(v, Array.isArray(v) ? null : ',');
    return '<fieldset class="fld"' + (err ? ' aria-describedby="' + id + '-e"' : '') + '><legend>' + esc(f.label) + '</legend>' + helpHtml(f, id + '-h') + '<div class="cbxs">' +
      (f.choices || []).map(function (c, j) {
        var cid = id + '-' + j;
        return '<div class="cbx"><input type="checkbox" id="' + cid + '" data-act="multi" data-key="' + esc(f.key) + '" data-val="' + esc(c.value) + '"' +
          (sel.indexOf(String(c.value)) >= 0 ? ' checked' : '') + (c.disabled ? ' disabled' : '') + (c.detail ? ' aria-describedby="' + cid + '-d"' : '') +
          '><label class="ot" for="' + cid + '">' + fmt(c.label == null ? c.value : c.label) + '</label>' + (c.detail ? '<span class="od" id="' + cid + '-d">' + fmt(c.detail) + '</span>' : '') + '</div>';
      }).join('') + '</div>' + errHtml(err, id + '-e') + '</fieldset>';
  }
  function boolField(f) {
    var id = fid(f.key), err = errFor(f);
    return '<div class="cbx"><input type="checkbox" id="' + id + '" data-act="bool" data-key="' + esc(f.key) + '"' + (isOn(S.values[f.key]) ? ' checked' : '') +
      (f.help || err ? ' aria-describedby="' + ids(f.help && id + '-h', err && id + '-e') + '"' : '') + (err ? ' aria-invalid="true"' : '') + '><label class="ot" for="' + id + '">' + esc(f.label) + '</label>' +
      (f.help ? '<span class="od" id="' + id + '-h">' + fmt(f.help) + '</span>' : '') + chipsHtml(f) + (err ? '<div class="msgs">' + errHtml(err, id + '-e') + '</div>' : '') + '</div>';
  }
  function pathsField(f) {
    var id = fid(f.key), err = errFor(f), v = S.values[f.key];
    return '<div class="fld"><label for="' + id + '">' + esc(f.label) + '</label><textarea class="inp mono wide" id="' + id + '" data-key="' + esc(f.key) + '" data-list=":" rows="3" spellcheck="false" aria-describedby="' +
      ids(id + '-n', f.help && id + '-h', err && id + '-e') + '"' + (err ? ' aria-invalid="true"' : '') + '>' + esc(asList(v, ':').map(tilde).join('\n')) + '</textarea>' +
      '<p class="note" id="' + id + '-n">One folder per line.</p>' + helpHtml(f, id + '-h') + errHtml(err, id + '-e') + '</div>';
  }

  // A secret: typed into a password field, checked or taken by the server, then shown masked only.
  function secretField(f, st) {
    var id = fid(f.key), v = S.values[f.key] || {}, ss = S.sec[f.key] || (S.sec[f.key] = {});
    var act = secretAction(f, st), isSet = !!v.set, editing = !isSet || ss.edit, err = errFor(f), busy = S.busy === 'sec:' + f.key;
    var body;
    if (!editing) {
      var pill = ss.state === 'ok' ? '<span class="pill p-ok">works</span>' : ss.state === 'bad' ? '<span class="pill p-bad">doesn’t work</span>' :
        v.pending ? '<span class="pill p-info">new</span>' : '<span class="pill p-mute">saved</span>';
      var again = act === 'check_token' ? 'Check again' : act === 'test_notify' ? 'Send a test' : '';
      body = '<div class="secret"><code id="' + id + '-v" aria-label="' + esc(f.label + ', hidden: ' + (v.masked || 'set')) + '">' + esc(v.masked || '••••••••') + '</code>' + pill +
        '<button type="button" class="btn sm" id="' + id + '-rep" data-act="sec-replace" data-key="' + esc(f.key) + '">Replace</button>' +
        (again ? '<button type="button" class="btn sm" id="' + id + '-again" data-act="sec-run" data-key="' + esc(f.key) + '"' + (busy ? ' disabled' : '') + '>' + (busy ? SPIN : '') + again + '</button>' : '') + '</div>';
    } else {
      var cmd = f.command || (act === 'check_token' ? 'claude setup-token' : '');
      var verb = act === 'check_token' ? 'Check' : 'Use it';
      body = (cmd ? '<div class="wz-row"><span class="cmd">' + esc(cmd) + '</span><button type="button" class="btn sm" data-act="copy" data-copy="' + esc(cmd) + '">Copy</button><span class="note">Run it in any terminal.</span></div>' : '') +
        '<div class="wz-row"><input class="inp mono" id="' + id + '" type="password" data-key="' + esc(f.key) + '" data-secret="1" autocomplete="off" spellcheck="false" placeholder="' +
        esc(f.placeholder || (act === 'check_token' ? 'sk-ant-oat01-…' : act === 'test_notify' ? 'https://…' : '')) + '" aria-describedby="' + ids(f.help && id + '-h', id + '-k', err && id + '-e') + '"' + (err ? ' aria-invalid="true"' : '') +
        '><button type="button" class="btn" id="' + id + '-use" data-act="sec-use" data-key="' + esc(f.key) + '"' + (busy ? ' disabled' : '') + '>' + (busy ? SPIN : '') + verb + '</button>' +
        (isSet ? '<button type="button" class="btn sm quiet" data-act="sec-keep" data-key="' + esc(f.key) + '">Keep the old one</button>' : '') + '</div>' +
        '<p class="help" id="' + id + '-k" aria-live="polite">' + esc(kindHint(act, S.typed[f.key] || '')) + '</p>';
    }
    return '<div class="fld">' + (editing ? '<label for="' + id + '">' + esc(f.label) + '</label>' : '<span class="lbl">' + esc(f.label) + '</span>') + helpHtml(f, id + '-h') + body +
      '<div id="' + id + '-m" role="status">' + (ss.msg ? wmsg(ss.msg.level, fmt(ss.msg.text)) : '') + '</div>' + errHtml(err, id + '-e') + '</div>';
  }
  function kindHint(act, text) {
    var t = String(text || '').trim();
    if (act === 'check_token') return t ? (/^sk-ant-api/.test(t) ? 'That looks like an API key, not a login.' : 'Press Check: setup asks Claude whether it works.') : 'It stays hidden, and only its last characters are shown afterwards.';
    if (!t) return 'Paste the URL. It stays hidden.';
    if (/^https:\/\/(?:(?:ptb|canary)\.)?discord(?:app)?\.com\/api\/webhooks\//.test(t)) return 'Discord webhook. Press Use it.';
    if (/^https:\/\/hooks\.slack\.com\/services\//.test(t)) return 'Slack webhook. Press Use it.';
    if (/^https:\/\/ntfy\./.test(t)) return 'ntfy topic. Press Use it.';
    if (/^https:\/\/\S+$/.test(t)) return 'A webhook URL. Press Use it.';
    return 'Not a webhook URL yet: it starts with https://';
  }

  // Folders found from the sessions. A ticked folder includes everything below it, unless its row is
  // "exact" (the home folder itself), so rows below a ticked one show as included.
  function folderList(f, st) { return f.folders || (st.info && st.info.folders) || []; }
  function picked(f, list) {
    if (f.key in S.values) return asList(S.values[f.key], ':');
    return list.filter(function (it) { return it.selected; }).map(function (it) { return it.path; });
  }
  function coveredBy(path, sel, list) {
    for (var i = 0; i < sel.length; i++) {
      var p = sel[i];
      if (p === path || !below(path, p)) continue;
      var row = list.filter(function (it) { return it.path === p; })[0];
      if (!row || !row.exact) return p;
    }
    return '';
  }
  function cumulative(list) {
    return list.every(function (it) {
      if (it.exact) return true;
      var kids = list.filter(function (k) { return below(k.path, it.path) && !list.some(function (m) { return m !== it && below(k.path, m.path) && below(m.path, it.path); }); });
      if (!kids.length || it.sessions == null) return true;
      return it.sessions >= kids.reduce(function (n, k) { return n + (k.sessions || 0); }, 0);
    });
  }
  function foldersField(f, st) {
    var id = fid(f.key), err = errFor(f), list = folderList(f, st), sel = picked(f, list), info = st.info || {};
    var rows = list.map(function (it, j) {
      var cid = id + '-' + j, by = coveredBy(it.path, sel, list), checked = sel.indexOf(it.path) >= 0 || !!by;
      var depth = list.filter(function (o) { return o !== it && below(it.path, o.path); }).length;
      var meta = [it.sessions != null ? it.sessions + (it.sessions === 1 ? ' session' : ' sessions') : '', it.last || '',
        it.repos ? it.repos + (it.repos === 1 ? ' repo' : ' repos') : '', it.hub ? 'hub ' + it.hub : ''].filter(Boolean).join(' · ');
      return '<li class="' + (depth ? 'd' + Math.min(depth, 4) : 'root') + (by ? ' covered' : '') + '"><input type="checkbox" id="' + cid + '" data-act="folder" data-key="' + esc(f.key) +
        '" data-path="' + esc(it.path) + '"' + (checked ? ' checked' : '') + (by ? ' disabled' : '') + (meta ? ' aria-describedby="' + cid + '-n"' : '') + '><label for="' + cid + '">' +
        esc(it.label || tilde(it.path)) + (it.note ? '<small>' + esc(it.note) + '</small>' : '') + (by ? '<small>included, below ' + esc(tilde(by)) + '</small>' : '') +
        '</label><span class="n" id="' + cid + '-n">' + esc(meta) + '</span></li>';
    });
    sel.forEach(function (p, j) {
      if (list.some(function (it) { return it.path === p; })) return;
      var cid = id + '-x' + j;
      rows.push('<li class="root"><input type="checkbox" id="' + cid + '" data-act="folder" data-key="' + esc(f.key) + '" data-path="' + esc(p) + '" checked><label for="' + cid + '">' +
        esc(tilde(p)) + '<small>added by you</small></label><span class="n"></span></li>');
    });
    var total = info.total || 0, sumTotal = !total, cum = cumulative(list), count = 0, any = false;
    list.forEach(function (it) {
      var by = coveredBy(it.path, sel, list), on = sel.indexOf(it.path) >= 0;
      if (it.sessions == null) return;
      // With cumulative counts a row already includes the rows below it, unless the row above is exact.
      var inner = cum && list.some(function (o) { return o !== it && !o.exact && below(it.path, o.path); });
      if (sumTotal && !inner) total += it.sessions;
      if ((on && !by) || (!cum && by)) { count += it.sessions; any = true; }
    });
    var summary = '';
    if (!sel.length) summary = wmsg('warn', '<b>No folder is ticked.</b>' + (st.id === 'sessions' ? ' The secretary reads only the docs folder, if you pick one next.' : ''));
    else if (st.id === 'sessions' && any && total) {
      var tops = sel.filter(function (p) { return !coveredBy(p, sel, list); });
      var one = tops.length === 1 && !(list.filter(function (it) { return it.path === tops[0]; })[0] || {}).exact;
      summary = wmsg('info', '<b>The secretary reads ' + count + ' of ' + total + ' sessions.</b> ' + (one ? 'Everything started in <code>' + esc(tilde(tops[0])) + '</code> or below it, including folders you make later.' : 'Sessions started in the ticked folders, or below them. New folders elsewhere are not added by themselves.'));
    } else summary = wmsg('info', '<b>' + sel.length + (sel.length === 1 ? ' folder' : ' folders') + ' picked.</b>');
    var legendNote = total ? ' <span class="note">· ' + total + ' sessions in ' + list.length + ' folders</span>' : '';
    return '<fieldset class="fld"' + (err ? ' aria-describedby="' + id + '-e"' : '') + '><legend>' + esc(f.label) + legendNote + '</legend>' +
      (rows.length ? '<ul class="tree"' + (f.help ? ' aria-describedby="' + id + '-h"' : '') + '>' + rows.join('') + '</ul>' : '') +
      '<div class="wz-row mt-s"><input class="inp mono" id="' + id + '-add" data-add-for="' + esc(f.key) + '" placeholder="Another folder, like ~/work/notes" aria-label="Add another folder" autocomplete="off" spellcheck="false">' +
      '<button type="button" class="btn sm" data-act="folder-add" data-key="' + esc(f.key) + '">Add</button></div>' +
      helpHtml(f, id + '-h') + errHtml(err, id + '-e') + '</fieldset><div role="status">' + summary + '</div>';
  }

  // ------------------------------------------------------------ previews and other action results

  function previewBlock(st) {
    var acts = stepActions(st).filter(function (a) { return a.name !== 'recheck'; });
    var sw = mainSwitch(st);
    if (!acts.length || (sw && !isOn(S.values[sw.key]))) return '';
    var p = S.preview[st.id] || {}, busy = acts.some(function (a) { return S.busy === a.name; });
    var btns = acts.map(function (a) {
      return '<button type="button" class="btn sm" id="act-' + esc(a.name) + '" data-act="step-action" data-name="' + esc(a.name) + '"' + (busy ? ' disabled' : '') + '>' +
        (S.busy === a.name ? SPIN : '') + esc(p.name === a.name ? 'Show again' : a.label || 'Show') + '</button>';
    }).join('');
    var body;
    if (busy) body = '<p class="note">' + SPIN + ' Working out the changes…</p>';
    else if (p.message || p.data) body = (p.message ? wmsg(p.ok ? 'info' : 'bad', fmt(p.message)) : '') + (p.data && (p.ok || hasData(p.data)) ? renderData(p.data) : '');
    else body = '<p class="note">Nothing changes until you press Install.</p>';
    return '<section class="block stack tight" aria-labelledby="pv-' + esc(st.id) + '"><div class="codehead"><h2 class="h2" id="pv-' + esc(st.id) + '">' +
      esc((st.info && st.info.preview_title) || PREVIEW_TITLE[st.id] || 'What setup will change') + '</h2>' + btns + '</div><div role="status">' + body + '</div></section>';
  }
  function colorDiff(text) {
    return String(text).split('\n').map(function (ln) {
      var e = esc(ln);
      if (/^\+/.test(ln)) return '<span class="lg-ok">' + e + '</span>';
      if (/^-/.test(ln)) return '<span class="lg-bad">' + e + '</span>';
      if (/^(@@|#)/.test(ln)) return '<span class="lg-dim">' + e + '</span>';
      return e;
    }).join('\n');
  }
  function renderItem(x) {
    if (x == null) return '';
    if (typeof x !== 'object') return '<li>' + fmt(String(x)) + '</li>';
    var title = x.what || x.name || x.path || x.label || x.title || '';
    var where = x.where && x.where !== title ? '<code>' + esc(tilde(x.where)) + '</code>' : '';
    var kind = x.kind ? '<span class="pill p-mute">' + esc(x.kind) + '</span>' : '';
    var body = x.diff || x.detail || x.text || '';
    var long = /\n/.test(body) || body.length > 110 || !!x.diff;
    return '<li><div class="rt">' + kind + '<span>' + fmt(tilde(title)) + '</span>' + where + (x.removable === false ? '<span class="pill p-mute">kept</span>' : '') + '</div>' +
      (body ? (long ? '<pre class="code">' + colorDiff(body) + '</pre>' : '<span class="note">' + fmt(body) + '</span>') : '') + '</li>';
  }
  function hasData(d) {
    return Object.keys(d || {}).some(function (k) { var v = d[k]; return ['model', 'step', 'values', 'keep', 'secrets', 'errors'].indexOf(k) < 0 && (Array.isArray(v) ? v.length : v); });
  }
  function renderData(d) {
    var parts = [];
    Object.keys(d).forEach(function (k) {
      if (['model', 'step', 'values', 'keep', 'secrets', 'ok', 'errors', 'folders'].indexOf(k) >= 0) return;
      if (k === 'changes' && (d.diffs || d.settings)) return; // the same changes, split into diffs and settings
      var v = d[k], label = k in DATA_LABEL ? DATA_LABEL[k] : k.replace(/_/g, ' ');
      var head = label ? '<p class="label">' + esc(label) + '</p>' : '';
      if (Array.isArray(v)) {
        if (!v.length) return;
        if (k === 'warnings' || k === 'notes') { parts.push('<div>' + v.map(function (t) { return wmsg(k === 'warnings' ? 'warn' : 'info', fmt(t)); }).join('') + '</div>'); return; }
        parts.push('<div>' + head + '<ul class="rows">' + v.map(renderItem).join('') + '</ul></div>');
      } else if (v && typeof v === 'object') {
        parts.push('<div>' + head + '<pre class="code">' + esc(JSON.stringify(v, null, 2)) + '</pre></div>');
      } else if (v !== '' && v != null) {
        parts.push('<div>' + head + '<p class="note">' + fmt(String(v)) + '</p></div>');
      }
    });
    return parts.length ? '<div class="data">' + parts.join('') + '</div>' : '<p class="note">Nothing to change.</p>';
  }

  // ------------------------------------------------------------ check

  function crow(c) {
    var st = c.status === 'fail' ? 'bad' : c.status === 'warn' ? 'warn' : c.status === 'ok' ? 'ok' : 'wait';
    var fix = c.fix ? '<div class="fix">' + wmsg(st === 'bad' ? 'bad' : st === 'warn' ? 'warn' : 'info', fixHtml(c.fix)) + '</div>' : '';
    return '<li class="crow"><span class="ci ' + st + '">' + ICON[st] + '<span class="vh">' + ICON_WORD[st] + ': </span></span><span class="ct">' + fmt(c.label) +
      '</span><span class="cd">' + fmt(c.detail || '') + '</span>' + fix + '</li>';
  }
  function checkBody(st) {
    var checks = st.checks || [];
    var fails = checks.filter(function (c) { return c.status === 'fail'; }), warns = checks.filter(function (c) { return c.status === 'warn'; });
    var head = '';
    if (fails.length) head = wmsg('bad', '<b>' + (fails.length === 1 ? '1 problem' : fails.length + ' problems') + ':</b> ' + fmt(fails[0].label) + '. You can carry on: Install waits until ' + (fails.length === 1 ? 'it is' : 'they are') + ' fixed.');
    else if (warns.length) head = wmsg('warn', '<b>' + (warns.length === 1 ? '1 thing needs' : warns.length + ' things need') + ' a look.</b> You can carry on.');
    else if (checks.length) head = wmsg('ok', '<b>All ' + checks.length + ' checks passed.</b> ' + (edit() ? 'Deskmate is installed and running.' : 'Continue to answer a few questions.'));
    var busy = S.busy === 'recheck';
    var again = '<div class="wz-row mt"><button type="button" class="btn sm" id="act-recheck" data-act="step-action" data-name="recheck"' + (busy ? ' disabled' : '') + '>' +
      (busy ? SPIN + 'Checking…' : 'Check again') + '</button><span class="note">' + (S.checkedAt ? 'Checked at ' + clock(S.checkedAt) + '. ' : '') + 'Setup also checks again before it installs.</span></div>';
    return '<div role="status">' + head + '</div><section class="block"><ul class="crows">' + checks.map(crow).join('') + '</ul>' + again + '</section>' +
      messages(st, false) + fieldBlocks(st) + advanced(st);
  }

  // ------------------------------------------------------------ review and install

  function changes() {
    if (!S.saved) return [];
    var out = [];
    allFields().forEach(function (x) {
      var f = x.f, a = S.saved[f.key], b = S.values[f.key];
      if (f.type === 'secret') { if (!(b && b.pending)) return; }
      else if (same(a, b) || display(f, a) === display(f, b)) return;
      var tag = x.st.id === 'desk' ? 'restarts the desk' : x.st.id === 'connect' || x.st.id === 'habits' ? 'new sessions only' : 'restarts the hub';
      out.push({ label: f.label, from: display(f, a), to: f.type === 'secret' ? 'replaced: ' + (b.masked || 'new') : display(f, b), step: x.st.id, tag: tag });
    });
    return out;
  }
  function advNote(st) {
    return (st.fields || []).filter(function (f) { return f.advanced && f.type !== 'secret' && visible(f, st); }).map(function (f) {
      var d = shortValue(f);
      return !d ? '' : f.type === 'bool' ? d : plain(f.label) + ' ' + d;
    }).filter(Boolean).join(' · ');
  }
  // A value worth showing in a one-line summary: empty answers and switches that are off say nothing.
  function shortValue(f) {
    var v = S.values[f.key];
    if (f.type === 'bool') return isOn(v) ? plain(f.label) : '';
    var d = display(f, v);
    return d === '—' || d === 'none' ? '' : d;
  }
  function summaryRows(st) {
    var info = st.info || {};
    if (Array.isArray(info.summary) && info.summary.length) return info.summary;
    if (Array.isArray(info.rows) && info.rows.length) return info.rows.map(function (r) {
      var s = stepById(r.step);
      return { step: r.step, label: r.title || (s && s.title) || r.step, value: r.summary || (r.skip ? 'Skipped' : '—'), note: r.skip || !s ? '' : advNote(s), status: r.skip ? '' : r.status };
    });
    return steps().filter(function (s) { return ['check', 'review', 'done'].indexOf(s.id) < 0; }).map(function (s) {
      return { step: s.id, label: s.title, value: isSkipped(s) ? (s.summary || 'Skipped') : (s.summary || '—'), note: isSkipped(s) ? '' : advNote(s), status: isSkipped(s) ? '' : s.status };
    });
  }
  function summaryList(st) {
    return '<dl class="sum">' + summaryRows(st).map(function (r) {
      var pill = r.status === 'warn' ? ' <span class="pill p-warn">needs a look</span>' : r.status === 'fail' ? ' <span class="pill p-bad">problem</span>' : '';
      return '<div><dt>' + esc(r.label) + '</dt><dd>' + fmt(r.value) + pill + (r.note ? '<span class="note">' + fmt(r.note) + '</span>' : '') + '</dd>' +
        '<a class="btn sm" href="#' + esc(r.step) + '" aria-label="Change ' + esc(r.label) + '">Change</a></div>';
    }).join('') + '</dl>';
  }
  function reviewBody(st) {
    var parts = [], ed = edit(), r = S.run, bl = blockers();
    if (bl.length && !(r && r.ok)) parts.push(wmsg('bad', '<b>' + (ed ? 'Apply' : 'Install') + ' waits until this is fixed:</b> ' + bl.map(function (c) { return fmt(c.label); }).join('; ') + '. <a href="#check">Back to Check</a>'));
    parts.push(messages(st, true));
    if (ed) {
      var d = changes();
      parts.push('<section class="block"><h2>Your changes <span class="count pill p-mute">' + d.length + '</span></h2>' +
        (d.length ? '<ul class="chg mt-s">' + d.map(function (x) {
          return '<li><b>' + esc(x.label) + '</b><s>' + esc(x.from) + '</s><span aria-hidden="true">→</span><span class="vh">changes to</span><span>' + esc(x.to) + '</span><span class="pill p-mute">' + esc(x.tag) + '</span><a class="src" href="#' + esc(x.step) + '">Change</a></li>';
        }).join('') + '</ul>' : '<p class="note mt-s">Nothing changed yet. Change any section below.</p>') +
        (r && !r.done ? '' : '<div class="wz-row mt"><button type="button" class="btn primary" id="btn-install" data-act="install"' + (d.length && !bl.length && !S.busy ? '' : ' disabled') + '>' + (S.busy === 'install' ? SPIN : '') + 'Apply changes</button>' +
          (d.length ? '<button type="button" class="btn" data-act="discard">Discard changes</button>' : '') + '</div>') + '</section>');
    }
    if (r) parts.push(runBlock());
    parts.push('<section class="block">' + (ed ? '<h2>All settings</h2><div class="mt-s">' + summaryList(st) + '</div>' : summaryList(st)) + '</section>');
    if (!r && !ed) {
      var rp = repo();
      var files = Array.isArray(st.info && st.info.files) ? '<b>Setup writes:</b><ul class="files">' + st.info.files.map(function (t) { return '<li>' + fmt(tilde(t)) + '</li>'; }).join('') + '</ul>' :
        (st.info && st.info.files) || ('Setup writes <code>.env</code>, your settings, which only you can read, and <code>compose.local.yaml</code>' + (rp ? ', both in <code>' + esc(tilde(rp)) + '</code>' : '') +
        ' and both kept out of git. Secrets go to files in the data folder that only you can read. Then it builds, starts, connects Claude Code and checks that everything works.');
      var filesHtml = Array.isArray(st.info && st.info.files) ? '<div class="note">' + files + '</div>' : '<p class="note">' + (st.info && st.info.files ? fmt(files) : files) + '</p>';
      parts.push('<section class="block stack tight">' + filesHtml + '<div class="wz-row"><button type="button" class="btn primary" id="btn-install" data-act="install"' +
        (bl.length || S.busy ? ' disabled' : '') + (bl.length ? ' aria-describedby="install-wait"' : '') + '>' + (S.busy === 'install' ? SPIN : '') + 'Install</button><span class="note" id="install-wait">' +
        (bl.length ? 'Install waits until Check passes.' : 'About 5 to 10 minutes and 2.6 GB the first time.') + '</span></div></section>');
    }
    return parts.join('');
  }

  function runBlock() {
    return '<section class="block stack tight" aria-labelledby="run-h"><h2 class="vh" id="run-h">' + (edit() ? 'Applying your changes' : 'Install') + '</h2>' +
      '<div id="run-msg" tabindex="-1"></div><ol class="phases" id="run-ph"></ol>' +
      '<div class="wz-prog" id="run-prog" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i></i></div>' +
      '<div class="codehead"><span class="label" id="log-l">Log</span><button type="button" class="btn sm" data-act="copy-log">Copy log</button></div>' +
      '<pre class="code log" id="run-log" role="log" aria-live="polite" aria-labelledby="log-l" tabindex="0"></pre></section>';
  }
  function phaseNames(r) {
    var rv = stepById('review'), base = (rv && rv.info && rv.info.phases) || PHASES.filter(function (p) { return p !== 'Working habits' || isOn(S.values.HABITS); });
    var names = base.slice();
    r.events.forEach(function (ev) { if (ev.phase && names.indexOf(ev.phase) < 0) names.push(ev.phase); });
    return names;
  }
  function phases(r) {
    var names = phaseNames(r), by = {}, last = -1;
    r.events.forEach(function (ev) { if (ev.phase) (by[ev.phase] = by[ev.phase] || []).push(ev); });
    names.forEach(function (n, i) { if (by[n]) last = i; });
    var res = r.result || {}, failed = r.done && !r.ok ? (res.failed_phase || names[Math.max(last, 0)]) : null;
    var now = Date.now() / 1000;
    return names.map(function (n, i) {
      var evs = by[n] || [], state;
      if (failed === n) state = 'failed';
      else if (i < last) state = 'done';
      else if (i === last) state = r.done ? 'done' : 'run';
      else state = r.done && r.ok ? 'done' : 'wait';
      var t0 = evs.length ? seconds(evs[0].ts) : null, t1 = null;
      for (var j = i + 1; j < names.length && t1 == null; j++) if (by[names[j]]) t1 = seconds(by[names[j]][0].ts);
      if (t1 == null && evs.length) t1 = state === 'run' ? now : seconds(evs[evs.length - 1].ts);
      var okText = evs.filter(function (e) { return e.level === 'ok'; }).pop(), errText = evs.filter(function (e) { return e.level === 'error'; }).pop();
      var desc = state === 'run' && evs.length ? evs[evs.length - 1].text : state === 'failed' && errText ? errText.text : okText ? okText.text : PHASE_NOTE[n] || '';
      return { name: n, state: state, desc: desc, time: state === 'done' && t0 != null ? took(t1 - t0) : state === 'run' ? 'running' : state === 'failed' ? 'stopped' : '' };
    });
  }
  function phaseLi(p) {
    var c = { done: 'ok', failed: 'bad', run: 'run', wait: 'wait' }[p.state];
    var word = { done: 'Done', failed: 'Failed', run: 'Running', wait: 'Waiting' }[p.state];
    return '<li><span class="ci ' + c + '">' + (c === 'run' ? SPIN : ICON[c]) + '<span class="vh">' + word + ': </span></span><span class="pn">' + esc(p.name) +
      '</span><span class="pt">' + esc(p.time) + '</span><span class="pd">' + esc(p.desc) + '</span></li>';
  }
  function logLine(ev, prevPhase) {
    var t = esc(ev.text), line;
    if (ev.level === 'ok') line = '<span class="lg-ok">✔</span> ' + t;
    else if (ev.level === 'error') line = '<span class="lg-bad">✘ ' + t + '</span>';
    else if (ev.level === 'warn') line = '<span class="lg-warn">!</span> ' + t;
    else line = t;
    return ev.phase && ev.phase !== prevPhase ? '<span class="lg-dim">▸ ' + esc(ev.phase) + '</span>\n' + line : line;
  }
  function logHtml(r) {
    var prev = '';
    var lines = r.events.map(function (ev) { var l = logLine(ev, prev); prev = ev.phase; return l; });
    return (r.prev || []).concat(lines).join('\n');
  }
  function logText(r) {
    var tmp = document.createElement('div');
    tmp.innerHTML = logHtml(r);
    return tmp.textContent;
  }
  function runMessage(r) {
    var res = r.result || {}, ed = edit();
    if (!r.done) return r.lost ? wmsg('warn', 'Lost contact with setup for a moment. Trying again…') :
      wmsg('busy', '<b>' + (ed ? 'Applying your changes.' : 'Installing.') + '</b> ' + (ed ? 'Only what changed is rebuilt or restarted.' : 'This takes 5 to 10 minutes the first time. The terminal keeps going if you close this tab.'));
    if (r.ok) {
      var warns = r.events.filter(function (e) { return e.level === 'warn'; }).length;
      return wmsg(warns ? 'warn' : 'ok', '<b>' + (ed ? 'Done. Your changes are applied.' : 'Deskmate is installed and running.') + '</b> ' +
        (warns ? (warns === 1 ? '1 check needs' : warns + ' checks need') + ' a look: see the log. ' : '') + (ed ? '' : 'Continue for your sign-in link.'));
    }
    return wmsg('bad', '<b>' + esc(ed ? 'Applying stopped' : 'Install stopped') + (res.failed_phase ? ' at ' + esc(res.failed_phase) : '') + '.</b> ' + fmt(res.hint || 'The log says why.') +
      '<div class="wz-row"><button type="button" class="btn primary" id="btn-retry" data-act="retry">Retry</button><button type="button" class="btn" data-act="copy-log">Copy log</button></div>');
  }
  // Updates the run section in place, so focus, the log's scroll position and screen readers keep their place.
  function paintRun() {
    var r = S.run;
    if (!r) return;
    var ph = $('#run-ph');
    if (!ph) return;
    var list = phases(r);
    ph.innerHTML = list.map(phaseLi).join('');
    var done = list.filter(function (p) { return p.state === 'done'; }).length, running = list.some(function (p) { return p.state === 'run'; });
    var pct = r.done && r.ok ? 100 : Math.round((done + (running ? 0.5 : 0)) / Math.max(1, list.length) * 100);
    var prog = $('#run-prog');
    prog.setAttribute('aria-valuenow', String(pct));
    prog.className = 'wz-prog' + (r.done ? (r.ok ? ' ok' : ' bad') : '');
    prog.firstChild.style.width = pct + '%';
    var msg = $('#run-msg'), html = runMessage(r);
    if (msg.getAttribute('data-k') !== html) { msg.innerHTML = html; msg.setAttribute('data-k', html); }
    var log = $('#run-log');
    if (log.getAttribute('data-n') !== String(r.events.length) + '/' + (r.prev || []).length) {
      var stick = log.scrollHeight - log.scrollTop - log.clientHeight < 30 || !log.textContent;
      log.innerHTML = logHtml(r);
      log.setAttribute('data-n', String(r.events.length) + '/' + (r.prev || []).length);
      if (stick) log.scrollTop = log.scrollHeight;
    }
  }

  function install(retry) {
    if (S.busy || (S.run && !S.run.done)) return;
    S.busy = 'install';
    render(retry ? 'run-msg' : 'btn-install');
    api('POST', '/api/validate', { step: 'review', values: S.values }).then(function (res) {
      if (!res.ok) { S.busy = ''; if (!fatal(res)) { S.stepMsg.review = { level: 'bad', text: res.data.error }; render(null); } return; }
      var d = res.data;
      applySecrets(d.secrets);
      if (d.model) applyModel(d.model);
      var errs = d.errors || {}, keys = Object.keys(errs);
      if (keys.length) { S.busy = ''; S.errors = errs; return showErrors(keys); }
      if (blockers().length) { S.busy = ''; render('wzH'); return; }
      return api('POST', '/api/apply', { values: S.values }).then(function (r2) {
        S.busy = '';
        if (!r2.ok) { if (!fatal(r2)) { S.stepMsg.review = { level: 'bad', text: r2.data.error }; render(null); } return; }
        follow(r2.data.run, retry);
      });
    });
  }
  function follow(id, retry) {
    var prev = [];
    if (retry && S.run) prev = (S.run.prev || []).concat(S.run.events.length ? [logHtml({ events: S.run.events }), '<span class="lg-dim">— Retry —</span>'] : []);
    S.run = { id: id, events: [], done: false, ok: null, result: null, prev: prev };
    S.stepMsg.review = null;
    render('run-msg');
    announce(edit() ? 'Applying your changes.' : 'Install started.');
    poll();
  }
  function poll() {
    var r = S.run;
    if (!r || r.done || S.ended) return;
    api('GET', '/api/run/' + encodeURIComponent(r.id) + '?since=' + r.events.length).then(function (res) {
      if (S.run !== r) return;
      if (!res.ok) {
        if (res.status === 401 || res.status === 410 || (res.status === 0 && r.lost > 8)) { fatal(res); return; }
        r.lost = (r.lost || 0) + 1;
        paintRun();
        setTimeout(poll, Math.min(5000, 700 * r.lost));
        return;
      }
      r.lost = 0;
      var d = res.data;
      (d.events || []).forEach(function (ev) { r.events.push(ev); });
      if (d.done) {
        r.done = true; r.ok = !!d.ok; r.result = d.result || {};
        if (r.ok && edit()) S.saved = clone(S.values);
        renderSide();
        if (S.step === 'review') { render(null); var m = $('#run-msg'); if (m) m.focus({ preventScroll: true }); }
        announce(r.ok ? (edit() ? 'Your changes are applied.' : 'Deskmate is installed and running.') : 'Install stopped. ' + plain((r.result || {}).hint || ''));
        return;
      }
      renderSide();
      paintRun();
      setTimeout(poll, 600);
    });
  }

  // ------------------------------------------------------------ done

  function doneBody(st) {
    var r = S.run, ed = edit();
    if (!(r && r.done && r.ok)) return wmsg('info', (ed ? 'Apply your changes first. ' : 'Install first. This page shows your sign-in link once Deskmate is running. ') + '<a href="#review">Go to ' + esc((stepById('review') || {}).title || 'Review') + '</a>');
    var res = r.result || {}, info = st.info || {}, parts = [], rp = repo();
    if (res.signin) parts.push('<section class="block stack tight"><h2 class="h2">Sign in</h2><p class="help">A link that signs this browser in to Deskmate. <code>./deskmate open</code> makes a new one any time.</p>' +
      '<div class="linkbox"><code id="signin-url" aria-label="Sign-in link">' + esc(res.signin_url || '') + '</code><a class="btn primary" id="btn-open" href="/signin" target="_blank" rel="noopener noreferrer">Open Deskmate</a>' +
      '<button type="button" class="btn" data-act="copy-signin">Copy link</button></div></section>');
    var prompt = info.prompt || 'Use the desk to open localhost:3000 and tell me what the page shows.';
    if (!ed) parts.push('<section class="block stack tight"><h2 class="h2">Try this first</h2><p class="help">In a new Claude Code session, ask:</p><pre class="code">' + esc(prompt) +
      '</pre><div class="wz-row"><button type="button" class="btn sm" data-act="copy" data-copy="' + esc(prompt) + '">Copy</button><span class="note">Then watch it happen on the Desk tab. Sessions that were already open need a restart first.</span></div></section>');
    var where = (info.where || [
      { label: 'Settings', value: (rp ? rp + '/' : '') + '.env', note: 'Your answers. Only you can read it.' },
      { label: 'Generated', value: 'compose.local.yaml', note: 'Setup writes it from .env. Don’t edit it.' },
      { label: 'Data', value: S.values.DESKMATE_DATA_DIR || '', note: 'The secretary’s database, the secret files and the file exchange with the desk. The desk’s logins live in a Docker volume.' },
      { label: 'Claude Code', value: 'deskmate', note: 'The MCP server and the plugin, for every session. See them with claude mcp list and /plugin.' }
    ]).map(whereItem);
    parts.push('<section class="block"><h2>Where things live</h2><dl class="sum two mt-s">' + where.filter(function (w) { return w.value || w.text; }).map(function (w) {
      return '<div><dt>' + esc(w.label) + '</dt><dd>' + (w.value ? '<code>' + esc(tilde(w.value)) + '</code>' : fmt(w.text)) + (w.note ? '<span class="note">' + fmt(w.note) + '</span>' : '') + '</dd></div>';
    }).join('') + '</dl></section>');
    var cmds = Array.isArray(info.commands) ? info.commands.join('\n') : info.commands || '<span class="c"># change any answer: this wizard again, with your settings</span>\n./deskmate setup\n\n<span class="c"># is everything working?</span>\n./deskmate doctor\n\n<span class="c"># a new sign-in link</span>\n./deskmate open\n\n<span class="c"># remove Deskmate: the plugin, both containers and their images.</span>\n<span class="c"># It asks before deleting the desk’s logins and the secretary’s data.</span>\n./deskmate uninstall';
    parts.push('<section class="block"><h2>Change or remove</h2><pre class="code mt-s">' + (info.commands ? esc(cmds) : cmds) + '</pre></section>');
    return parts.join('');
  }

  // The model may give "Label: /a/path (a note)" strings; the page shows them like its own {label, value, note}.
  function whereItem(w) {
    if (typeof w !== 'string') return w;
    var m = /^([^:]{1,30}):\s+(.*)$/.exec(w);
    if (!m) return { label: '', text: w };
    var n = /^([~\/][^\s(]*)(?:\s+\((.*)\))?$/.exec(m[2]);
    return n ? { label: m[1], value: n[1], note: n[2] || '' } : { label: m[1], text: m[2] };
  }

  function renderEnded() {
    var e = S.ended;
    $('#wz').classList.add('is-ended');
    $('#wzSteps').innerHTML = '';
    $('#wzSideCard').innerHTML = '';
    $('#wzMain').innerHTML = '<div class="ended"><h1 class="h1" id="wzH" tabindex="-1">' + esc(e.title) + '</h1>' + wmsg(e.level, fmt(e.text)) + '</div>';
    focusHeading();
  }

  // ------------------------------------------------------------ navigation and answers

  function go(id, replace) {
    if (!stepById(id)) return;
    S.step = id;
    S.seen[id] = 1;
    S.confirm = false;
    if (location.hash.slice(1) !== id) {
      if (replace) history.replaceState(null, '', '#' + id);
      else location.hash = id;
    }
    window.scrollTo(0, 0);
    render(null);
    focusHeading();
    autoActions(stepById(id));
  }
  function jump(id) {
    if (!stepById(id) || id === S.step) return;
    refreshModel().then(function () { if (!S.ended) go(id); });
  }
  function autoActions(st) {
    if (!st || isSkipped(st) || (S.run && !S.run.done)) return;
    var sw = mainSwitch(st);
    if (sw && !isOn(S.values[sw.key])) return;
    stepActions(st).forEach(function (a) {
      var p = S.preview[st.id];
      if (a.auto && !S.busy && (!p || p.at !== JSON.stringify(S.values))) stepAction(a.name, null);
    });
  }
  function stepAction(name, btnId) {
    var st = cur();
    S.busy = name;
    render(btnId || undefined);
    api('POST', '/api/action', { name: name, values: S.values }).then(function (res) {
      S.busy = '';
      if (!res.ok) { S.queued = false; if (!fatal(res)) { S.stepMsg[st.id] = { level: 'bad', text: res.data.error || 'That didn’t work.' }; render(btnId || undefined); } return; }
      var d = res.data, data = d.data || {};
      applySecrets(d.secrets);
      if (data.model) applyModel(data.model);
      else if (data.step) replaceStep(data.step);
      if (name === 'recheck') {
        S.checkedAt = new Date();
        S.stepMsg[st.id] = d.ok ? null : { level: 'bad', text: d.message || 'The check didn’t run.' };
        var after = function () { render(btnId || undefined); announce(d.message || 'Checked again.'); idle(); };
        if (!data.model && !data.step) refreshModel().then(after); else after();
        return;
      }
      S.preview[st.id] = { name: name, ok: d.ok, message: d.message, data: data, at: JSON.stringify(S.values) };
      render(btnId || undefined);
      if (d.message) announce(plain(d.message));
      idle();
    });
  }
  // Continue pressed while a check or preview runs: go on once it is done instead of ignoring the press.
  function idle() {
    if (S.queued && !S.busy) { S.queued = false; onContinue(); }
  }
  function onContinue() {
    var st = cur();
    if (!st) return;
    if (S.busy) {
      if (S.busy !== 'continue' && S.busy !== 'install' && !S.queued) { S.queued = true; announce('Continuing when this finishes.'); }
      return;
    }
    if (st.id === 'review') { if (S.run && S.run.done && S.run.ok) go(nextId('review', 1) || 'done'); return; }
    if (st.id === 'done') return;
    var typed = (st.fields || []).filter(function (f) { return f.type === 'secret' && visible(f, st) && (S.typed[f.key] || '').trim(); })[0];
    if (typed) {
      var verb = secretAction(typed, st) === 'check_token' ? 'Check' : 'Use it';
      (S.sec[typed.key] = S.sec[typed.key] || {}).msg = { level: 'warn', text: 'Press **' + verb + '** first, or clear the field.' };
      render(fid(typed.key));
      announce('Press ' + verb + ' first, or clear the field.');
      return;
    }
    S.busy = 'continue';
    render('btn-continue');
    api('POST', '/api/validate', { step: st.id, values: S.values }).then(function (res) {
      S.busy = '';
      if (!res.ok) { if (!fatal(res)) { S.stepMsg[st.id] = { level: 'bad', text: res.data.error }; render('btn-continue'); } return; }
      var d = res.data;
      applySecrets(d.secrets);
      if (d.model) applyModel(d.model);
      else if (d.step) replaceStep(d.step);
      var errs = d.errors || {}, keys = Object.keys(errs);
      if (keys.length) { Object.keys(errs).forEach(function (k) { S.errors[k] = errs[k]; }); showErrors(keys); return; }
      S.stepMsg[st.id] = null;
      S.seen[st.id] = 1;
      var n = nextId(st.id, 1);
      if (n) go(n);
    });
  }
  // Opens the step that holds the first error and puts the focus on its field.
  function showErrors(keys) {
    var first = keys[0], x = fieldOf(first), sid = x ? x.st.id : S.step;
    if (x && x.f.advanced) S.open[sid] = true;
    if (x && x.f.type === 'secret') (S.sec[first] = S.sec[first] || {}).edit = true;
    announce(keys.length === 1 ? 'One answer needs a look.' : keys.length + ' answers need a look.');
    if (sid !== S.step) { S.step = sid; S.seen[sid] = 1; if (location.hash.slice(1) !== sid) location.hash = sid; window.scrollTo(0, 0); }
    render(null);
    var cands = [fid(first), fid(first) + '-0', fid(first) + '-other', fid(first) + '-add', fid(first) + '-rep'];
    for (var i = 0; i < cands.length; i++) { var el = document.getElementById(cands[i]); if (el) { el.focus(); return; } }
    focusHeading();
  }

  function secretUse(key) {
    var st = cur(), x = fieldOf(key);
    if (!x || S.busy) return;
    var f = x.f, act = secretAction(f, st), ss = S.sec[key] || (S.sec[key] = {}), typed = (S.typed[key] || '').trim();
    if (!typed) { ss.msg = { level: 'bad', text: act === 'check_token' ? 'Paste the token first.' : 'Paste the URL first.' }; render(fid(key)); return; }
    var vals = clone(S.values);
    vals[key] = typed;
    S.busy = 'sec:' + key;
    render(fid(key) + '-use');
    var call = act === 'check_token' ? api('POST', '/api/action', { name: act, values: vals }) : api('POST', '/api/validate', { step: st.id, values: vals });
    call.then(function (res) {
      S.busy = '';
      if (!res.ok) { if (!fatal(res)) { ss.msg = { level: 'bad', text: res.data.error || 'That didn’t work.' }; render(fid(key)); } return; }
      var d = res.data, kept = d.secrets && d.secrets[key], ok, text;
      applySecrets(d.secrets);
      if (act === 'check_token') { ok = !!d.ok; text = d.message; }
      else {
        var err = (d.errors || {})[key];
        ok = !err && !!kept;
        text = err || (act === 'test_notify' ? 'Taken. It is saved when you install. Press **Send a test** to try it now.' : 'Taken. It is saved when you install.');
        if (d.step) replaceStep(d.step);
      }
      if (kept) {
        S.typed[key] = '';
        ss.edit = false;
        ss.state = act === 'check_token' ? (ok ? 'ok' : '') : '';
        ss.msg = { level: ok ? 'ok' : 'warn', text: text || (ok ? 'Works.' : 'Saved, but not checked.') };
        delete S.errors[key];
        S.dirty[key] = true;
        render(null);
        var fe = document.getElementById(fid(key) + '-again') || document.getElementById(fid(key) + '-rep');
        if (fe) fe.focus();
      } else {
        ss.state = 'bad';
        ss.msg = { level: 'bad', text: text || 'That didn’t work.' };
        render(fid(key));
      }
      announce(plain(text || ''));
      if (kept) idle(); else S.queued = false;
    });
  }
  function secretRun(key) {
    var st = cur(), x = fieldOf(key);
    if (!x || S.busy) return;
    var act = secretAction(x.f, st), ss = S.sec[key] || (S.sec[key] = {});
    if (!act) return;
    S.busy = 'sec:' + key;
    render(fid(key) + '-again');
    api('POST', '/api/action', { name: act, values: S.values }).then(function (res) {
      S.busy = '';
      if (!res.ok) { if (!fatal(res)) { ss.msg = { level: 'bad', text: res.data.error }; render(fid(key) + '-again'); } return; }
      var d = res.data;
      applySecrets(d.secrets);
      ss.state = d.ok ? 'ok' : 'bad';
      ss.msg = { level: d.ok ? 'ok' : 'bad', text: d.message || (d.ok ? 'Works.' : 'That didn’t work.') };
      render(fid(key) + '-again');
      announce(plain(ss.msg.text));
      idle();
    });
  }

  function toggleFolder(key, path, on) {
    var x = fieldOf(key);
    if (!x) return;
    var list = folderList(x.f, x.st), sel = picked(x.f, list).filter(function (p) { return p !== path; });
    if (on) sel = sel.filter(function (p) { return !below(p, path) || (list.filter(function (it) { return it.path === path; })[0] || {}).exact; }).concat([path]);
    setList(x.f, sel, ':');
    S.dirty[key] = true;
    delete S.errors[key];
  }
  function addFolder(key) {
    var input = document.getElementById(fid(key) + '-add');
    var p = untilde(input && input.value);
    if (!p) { if (input) input.focus(); return; }
    toggleFolder(key, p.replace(/\/+$/, '') || '/', true);
    render(fid(key) + '-add');
    toast('Added ' + tilde(p) + '.');
  }
  function toggleMulti(key, val, on) {
    var x = fieldOf(key);
    if (!x) return;
    var v = S.values[key], sel = asList(v, Array.isArray(v) ? null : ',').filter(function (s) { return s !== val; });
    if (on) sel.push(val);
    setList(x.f, sel, ',');
    S.dirty[key] = true;
    delete S.errors[key];
  }

  function copyText(text, done) {
    var ok = function () { toast(done || 'Copied.'); };
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(ok, function () { fallbackCopy(text) && ok(); }); return; }
    if (fallbackCopy(text)) ok(); else toast('Copy didn’t work here. Select the text and copy it yourself.');
  }
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.className = 'vh';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }

  // ------------------------------------------------------------ events

  function onClick(e) {
    var b = e.target.closest && e.target.closest('[data-act]');
    if (!b || b.disabled) return;
    var act = b.getAttribute('data-act'), key = b.getAttribute('data-key');
    if (['folder', 'multi', 'bool'].indexOf(act) >= 0) return; // checkboxes: handled on change
    e.preventDefault();
    switch (act) {
      case 'seg': setVal(key, b.getAttribute('data-val')); render(b.id); if (key === 'SECRETARY' || mainSwitchKey(key)) refreshModel().then(function () { render(b.id); autoActions(cur()); }); break;
      case 'back': go(nextId(S.step, -1)); break;
      case 'step-action': stepAction(b.getAttribute('data-name'), b.id); break;
      case 'sec-replace': (S.sec[key] = S.sec[key] || {}).edit = true; S.sec[key].msg = null; render(fid(key)); break;
      case 'sec-keep': S.sec[key].edit = false; S.sec[key].msg = null; S.typed[key] = ''; render(fid(key) + '-rep'); break;
      case 'sec-use': secretUse(key); break;
      case 'sec-run': secretRun(key); break;
      case 'copy': copyText(b.getAttribute('data-copy')); break;
      case 'copy-log': if (S.run) copyText(logText(S.run), 'Log copied.'); break;
      case 'copy-signin':
        if (S.run && S.run.result && S.run.result.signin_url) { copyText(S.run.result.signin_url, 'Sign-in link copied. It works for one browser.'); break; }
        api('POST', '/api/signin', {}).then(function (res) {
          if (res.ok && res.data.url) copyText(res.data.url, 'Sign-in link copied. It works for one browser.');
          else if (!fatal(res)) toast(res.data.error || 'There is no sign-in link yet.');
        });
        break;
      case 'install': install(false); break;
      case 'retry': install(true); break;
      case 'discard':
        api('POST', '/api/discard', {}).then(function (res) {
          if (!res.ok) { fatal(res); return; }
          S.values = clone(S.saved); S.errors = {}; S.dirty = {}; S.typed = {}; S.sec = {};
          refreshModel().then(function () { render('wzH'); toast('Changes discarded.'); });
        });
        break;
      case 'secretary-on': setVal('SECRETARY', 'on'); refreshModel().then(function () { render(null); focusHeading(); }); break;
      case 'cancel': S.confirm = true; render('cc-no'); break;
      case 'cancel-no': S.confirm = false; render('wzH'); break;
      case 'cancel-yes':
        api('POST', '/api/cancel', {}).then(function (res) {
          if (res.ok) end(edit() ? 'Setup closed' : 'Setup cancelled', 'info', S.run && S.run.done && !S.run.ok ?
            'Install didn’t finish. Run `./deskmate setup` again to retry: it opens with your answers.' : 'Nothing on ' + platformWord() + ' was changed. Run `./deskmate setup` again any time.');
          else if (!fatal(res)) { S.confirm = false; S.stepMsg[S.step] = { level: 'bad', text: res.data.error }; render(null); }
        });
        break;
      case 'finish':
        api('POST', '/api/finish', {}).then(function (res) {
          if (res.ok) end('Setup is finished', 'ok', 'Deskmate is running. You can close this tab. `./deskmate open` gives you a new sign-in link any time.');
          else if (!fatal(res)) toast(res.data.error || 'That didn’t work.');
        });
        break;
      case 'folder-add': addFolder(key); break;
    }
  }
  function mainSwitchKey(key) { var x = fieldOf(key); return !!(x && mainSwitch(x.st) === x.f); }
  function onInput(e) {
    var el = e.target, key = el.getAttribute && el.getAttribute('data-key');
    ping();
    if (!key) return;
    if (el.getAttribute('data-secret')) {
      S.typed[key] = el.value;
      var x = fieldOf(key), k = document.getElementById(fid(key) + '-k');
      if (k && x) k.textContent = kindHint(secretAction(x.f, x.st), el.value);
      return;
    }
    if (el.type === 'radio' || el.type === 'checkbox' || el.tagName === 'SELECT') return;
    if (el.getAttribute('data-list')) {
      var xf = fieldOf(key);
      if (xf) setList(xf.f, el.value.split('\n').map(function (s) { return untilde(s); }).filter(Boolean), ':');
    } else if (el.getAttribute('data-path-other')) {
      S.otherVal[key] = untilde(el.value);
      S.values[key] = untilde(el.value);
    } else setVal(key, el.value);
    S.dirty[key] = true;
    delete S.errors[key];
    if (el.getAttribute('aria-invalid')) {
      el.removeAttribute('aria-invalid');
      var em = document.getElementById(fid(key) + '-e');
      if (em) em.remove();
    }
  }
  function onChange(e) {
    var el = e.target, key = el.getAttribute && el.getAttribute('data-key'), act = el.getAttribute && el.getAttribute('data-act');
    if (!key) return;
    if (act === 'folder') { toggleFolder(key, el.getAttribute('data-path'), el.checked); render(el.id); return; }
    if (act === 'multi') { toggleMulti(key, el.getAttribute('data-val'), el.checked); render(el.id); return; }
    if (act === 'bool') { setVal(key, el.checked ? 'on' : 'off'); render(el.id); return; }
    if (el.type === 'radio') {
      if (el.getAttribute('data-other')) { S.other[key] = true; S.values[key] = S.otherVal[key] || ''; S.dirty[key] = true; render(fid(key) + '-other'); }
      else { S.other[key] = false; setVal(key, el.value); render(el.id); }
      return;
    }
    if (el.tagName === 'SELECT') { setVal(key, el.value); render(el.id); }
  }
  function onKey(e) {
    ping();
    var el = e.target;
    if (e.key === 'Escape' && S.confirm) { S.confirm = false; render('wzH'); return; }
    if (e.key !== 'Enter' || !el.getAttribute) return;
    if (el.getAttribute('data-secret')) { e.preventDefault(); secretUse(el.getAttribute('data-key')); }
    else if (el.getAttribute('data-add-for')) { e.preventDefault(); addFolder(el.getAttribute('data-add-for')); }
  }
  function onHash() {
    var id = decodeURIComponent(location.hash.slice(1));
    if (S.ended || !S.model || id === S.step) return;
    if (stepById(id)) jump(id);
  }

  // ------------------------------------------------------------ theme and start

  function setTheme(t) {
    var root = document.documentElement;
    if (t === 'auto') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
    var btn = $('#themeBtn');
    btn.setAttribute('aria-label', 'Colour theme: ' + (t === 'auto' ? 'follows this computer' : t) + '. Press to change.');
    btn.setAttribute('data-theme', t);
    try { localStorage.setItem('deskmate-setup-theme', t); } catch (_) { /* private mode */ }
  }

  function start() {
    var saved = 'auto';
    try { saved = localStorage.getItem('deskmate-setup-theme') || 'auto'; } catch (_) { /* private mode */ }
    setTheme(THEMES.indexOf(saved) >= 0 ? saved : 'auto');
    $('#themeBtn').addEventListener('click', function () {
      var t = THEMES[(THEMES.indexOf(this.getAttribute('data-theme') || 'auto') + 1) % THEMES.length];
      setTheme(t);
      toast(t === 'auto' ? 'Theme follows this computer.' : 'Theme: ' + t + '.');
    });
    document.addEventListener('click', onClick);
    document.addEventListener('input', onInput);
    document.addEventListener('change', onChange);
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', ping);
    document.addEventListener('submit', function (e) { e.preventDefault(); onContinue(); });
    window.addEventListener('hashchange', onHash);
    api('GET', '/api/model').then(function (res) {
      if (!res.ok) {
        if (fatal(res)) return;
        $('#wzMain').innerHTML = wmsg('bad', '<b>Setup couldn’t load its questions.</b> ' + fmt(res.data.error || '') + '<div class="wz-row"><button type="button" class="btn sm" id="reload">Try again</button></div>');
        $('#reload').addEventListener('click', function () { location.reload(); });
        return;
      }
      if (!res.data.steps || !res.data.steps.length) {
        $('#wzMain').innerHTML = wmsg('bad', '<b>Setup sent no questions.</b> Run <code>./deskmate setup --terminal</code> instead, and report this.');
        return;
      }
      applyModel(res.data);
      if (edit()) steps().forEach(function (s) { S.seen[s.id] = 1; });
      var srv = res.data.server || {}, hash = decodeURIComponent(location.hash.slice(1));
      var first = stepById(hash) ? hash : edit() ? 'review' : steps()[0].id;
      if (srv.run) {
        S.run = { id: srv.run.id, events: [], done: false, ok: null, result: null, prev: [] };
        if (!(hash === 'done' && srv.run.done && srv.run.ok)) first = 'review';
      }
      go(first, true);
      if (S.run) poll();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
