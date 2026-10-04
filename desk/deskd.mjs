#!/usr/bin/env node
// deskd — the control API for the desk's screen, mouse, keyboard, clipboard and files.
//
// Adapted from Deskfish's desktop daemon (Apache-2.0, Copyright 2026 Iman Reihanian; see NOTICE).
// Changes: listens on a unix socket shared only with the hub; the Firefox page bridge and the
// websockify proxy are gone (the hub reads pages over CDP and bridges VNC itself); the
// `application` action opens Chromium; new `restart_browser` action.
//
// Endpoints (on $DESKD_SOCKET, default /run/desk/deskd.sock)
//   POST /computer-use/computer   { action, ...params }        → { success, data? , error? }
//                                 (+ set_clipboard {text} / get_clipboard → {text})
//                                 (+ read_file {path} / write_file {path,data} / list_files {path},
//                                    all confined to $HOME)
//   GET  /                        health + screen size
//   GET  /screenshot.png          current screen
import { execFile, spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { promisify } from 'node:util';

const run = promisify(execFile);
const SOCKET = process.env.DESKD_SOCKET || '/run/desk/deskd.sock';
const TOKEN = process.env.DAEMON_TOKEN || '';
const HOME = process.env.HOME || '/home/bot';
const SHOT = '/tmp/screenshot.png';

// ---------- keys: accept xdotool names, nut.js names and common aliases ----------
const KEY_ALIASES = {
  enter: 'Return', return: 'Return', kp_enter: 'KP_Enter',
  esc: 'Escape', escape: 'Escape',
  backspace: 'BackSpace', bksp: 'BackSpace',
  tab: 'Tab', space: 'space', spacebar: 'space',
  delete: 'Delete', del: 'Delete', insert: 'Insert',
  home: 'Home', end: 'End',
  pageup: 'Page_Up', page_up: 'Page_Up', prior: 'Page_Up',
  pagedown: 'Page_Down', page_down: 'Page_Down', next: 'Page_Down',
  up: 'Up', down: 'Down', left: 'Left', right: 'Right',
  ctrl: 'ctrl', control: 'ctrl', ctl: 'ctrl', leftcontrol: 'ctrl', rightcontrol: 'ctrl', control_l: 'ctrl', control_r: 'ctrl',
  alt: 'alt', leftalt: 'alt', rightalt: 'alt', alt_l: 'alt', alt_r: 'alt', option: 'alt',
  shift: 'shift', leftshift: 'shift', rightshift: 'shift', shift_l: 'shift', shift_r: 'shift',
  super: 'super', meta: 'super', win: 'super', cmd: 'super', command: 'super', leftsuper: 'super', super_l: 'super',
  capslock: 'Caps_Lock', caps_lock: 'Caps_Lock', printscreen: 'Print', print: 'Print',
  minus: 'minus', plus: 'plus', equal: 'equal', comma: 'comma', period: 'period', slash: 'slash',
  backslash: 'backslash', semicolon: 'semicolon', apostrophe: 'apostrophe', quote: 'apostrophe', grave: 'grave',
  bracketleft: 'bracketleft', bracketright: 'bracketright',
};
const CHAR_KEYSYMS = {
  ' ': 'space', '-': 'minus', '+': 'plus', '=': 'equal', ',': 'comma', '.': 'period', '/': 'slash', '\\': 'backslash',
  ';': 'semicolon', "'": 'apostrophe', '`': 'grave', '[': 'bracketleft', ']': 'bracketright', '\n': 'Return', '\t': 'Tab',
};
function xKey(k) {
  const s = String(k).trim();
  const lower = s.toLowerCase();
  if (KEY_ALIASES[lower]) return KEY_ALIASES[lower];
  if (/^f([1-9]|1[0-9]|2[0-4])$/.test(lower)) return 'F' + lower.slice(1);
  if (s.length === 1) return CHAR_KEYSYMS[s] ?? s;
  return s; // assume an X keysym name (e.g. "Page_Down", "XF86AudioMute")
}
const BUTTONS = { left: 1, middle: 2, right: 3 };

// ---------- xdotool helpers ----------
const xdo = (...args) => run('xdotool', args.map(String));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- stuck keys and buttons ----------
// Everything that reaches this display is injected through XTEST: xdotool (the agent) and x11vnc
// (the user). A release that never arrives — the user let go of the mouse outside the live view,
// alt-tabbed away with Alt down, VS Code swallowed a key-up — leaves a key or button held in the
// X server, and from then on every click is a shift-click or an Alt-drag (Openbox moves the
// window). `xinput --query-state` on the XTEST devices shows exactly what is held, so
// release_input lets go of precisely that and reports it, and a watchdog does the same for
// anything held longer than a person would.
const XTEST_KEYBOARD = 'Virtual core XTEST keyboard';
const XTEST_POINTER = 'Virtual core XTEST pointer';
const MODIFIER_RE = /^(Shift|Control|Alt|Super|Meta|Hyper|ISO_Level3_Shift|Mode_switch)/;
let keyNames; // keycode → keysym name, from xmodmap -pke (once)

async function keyName(code) {
  if (!keyNames) {
    keyNames = new Map();
    const { stdout } = await run('xmodmap', ['-pke']).catch(() => ({ stdout: '' }));
    for (const line of stdout.split('\n')) {
      const m = line.match(/^keycode\s+(\d+)\s*=\s*(\S+)?/);
      if (m && m[2]) keyNames.set(Number(m[1]), m[2]);
    }
  }
  return keyNames.get(code) ?? `keycode ${code}`;
}

/** What XTEST currently holds down: `{ keys: [{code, name}], buttons: [n] }`; null if xinput is unavailable. */
async function inputState() {
  try {
    const [kb, pt] = await Promise.all([run('xinput', ['--query-state', XTEST_KEYBOARD]), run('xinput', ['--query-state', XTEST_POINTER])]);
    const keys = [];
    for (const m of kb.stdout.matchAll(/key\[(\d+)\]=down/g)) keys.push({ code: Number(m[1]), name: await keyName(Number(m[1])) });
    const buttons = [...pt.stdout.matchAll(/button\[(\d+)\]=down/g)].map((m) => Number(m[1]));
    return { keys, buttons };
  } catch {
    return null;
  }
}

/** Let go of everything held; returns the names of what was released. Without xinput: the blanket release. */
async function releaseInput(reason) {
  const state = await inputState();
  const released = [];
  if (!state) {
    await xdo('keyup', 'ctrl', 'shift', 'alt', 'super').catch(() => undefined);
    for (const b of [1, 2, 3]) await xdo('mouseup', b).catch(() => undefined);
    return { released, blind: true };
  }
  for (const k of state.keys) {
    await xdo('keyup', k.code).catch(() => undefined);
    released.push(k.name);
  }
  for (const b of state.buttons) {
    await xdo('mouseup', b).catch(() => undefined);
    released.push(`button ${b}`);
  }
  if (released.length) console.log(`${new Date().toISOString()} release_input (${reason}): released ${released.join(', ')}`);
  return { released, blind: false };
}

// Watchdog: a modifier or button held for 30 s straight is stuck, not in use (the agent's own
// holds last a fraction of a second; a person's drag or shift-click, a few seconds).
const HELD_TOO_LONG_MS = 30_000;
const heldSince = new Map(); // "key:50" / "button:1" → first seen down
async function watchdog() {
  const state = await inputState();
  if (!state) return;
  const now = Date.now();
  const seen = new Set();
  const stuck = [];
  for (const k of state.keys) {
    const id = `key:${k.code}`;
    seen.add(id);
    if (!heldSince.has(id)) heldSince.set(id, now);
    else if (now - heldSince.get(id) >= HELD_TOO_LONG_MS && (MODIFIER_RE.test(k.name) || now - heldSince.get(id) >= 2 * HELD_TOO_LONG_MS)) stuck.push({ kind: 'key', id, code: k.code, name: k.name });
  }
  for (const b of state.buttons) {
    const id = `button:${b}`;
    seen.add(id);
    if (!heldSince.has(id)) heldSince.set(id, now);
    else if (now - heldSince.get(id) >= HELD_TOO_LONG_MS) stuck.push({ kind: 'button', id, code: b, name: `button ${b}` });
  }
  for (const id of [...heldSince.keys()]) if (!seen.has(id)) heldSince.delete(id);
  for (const s of stuck) {
    const held = Math.round((now - heldSince.get(s.id)) / 1000);
    await (s.kind === 'key' ? xdo('keyup', s.code) : xdo('mouseup', s.code)).catch(() => undefined);
    heldSince.delete(s.id);
    console.log(`${new Date().toISOString()} watchdog: released ${s.name}, held for ${held} s`);
  }
}
setInterval(() => enqueue(watchdog).catch(() => undefined), 5_000).unref();

async function withHeldKeys(holdKeys, fn) {
  const keys = (holdKeys ?? []).map(xKey);
  if (keys.length) await xdo('keydown', ...keys);
  try {
    return await fn();
  } finally {
    if (keys.length) await xdo('keyup', ...keys.reverse());
  }
}

/** Text longer than this is typed at the fast key delay. */
const PASTE_OVER = 200;

/** One line via xdotool type (stdin, so no shell quoting); if xdotool cannot map a character, paste the line instead. */
async function typeRun(text, delay) {
  try {
    await new Promise((resolve, reject) => {
      const child = execFile('xdotool', ['type', '--delay', String(delay), '--file', '-'], { env: { ...process.env, LC_ALL: 'C.UTF-8', LANG: 'C.UTF-8' } }, (err) => (err ? reject(err) : resolve()));
      child.stdin.end(text);
    });
  } catch (err) {
    console.log(`type_text: xdotool could not type a line of ${text.length} characters (${err.message.split('\n')[1] ?? err.message}); pasting instead`);
    await pasteText(text);
  }
}

/**
 * Put `text` on both selections and press shift+Insert, which pastes in Firefox/GTK (CLIPBOARD)
 * and in xterm (PRIMARY) alike. The previous clipboard (or nothing) is put back right after, so
 * the extension's clipboard mirror does not carry the pasted text to the user's machine.
 */
async function pasteText(text) {
  const before = await run('xclip', ['-selection', 'clipboard', '-o']).then((r) => r.stdout, () => '');
  for (const sel of ['clipboard', 'primary']) await xclipWrite(sel, text);
  await xdo('key', '--delay', '40', 'shift+Insert');
  await sleep(120);
  for (const sel of ['clipboard', 'primary']) await xclipWrite(sel, before).catch(() => undefined);
}

function xclipWrite(selection, text) {
  return new Promise((resolve, reject) => {
    const child = spawn('xclip', ['-selection', selection, '-i'], { stdio: ['pipe', 'ignore', 'ignore'], detached: true });
    child.on('error', reject);
    child.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`xclip exited ${code}`))));
    child.stdin.end(text);
    child.unref();
  });
}

async function screenSize() {
  const { stdout } = await xdo('getdisplaygeometry');
  const [w, h] = stdout.trim().split(/\s+/).map(Number);
  return { width: w, height: h };
}

async function screenshotPng() {
  await run('scrot', ['-o', '-z', SHOT]);
  return fs.readFile(SHOT);
}

function safePath(p) {
  const abs = path.resolve(HOME, String(p));
  if (abs !== HOME && !abs.startsWith(HOME + path.sep)) throw new Error(`path must be inside ${HOME}`);
  return abs;
}

// ---------- actions ----------
async function handle(body) {
  const { action } = body;
  const coords = body.coordinates;
  const moveIf = async () => {
    if (coords && Number.isFinite(coords.x) && Number.isFinite(coords.y)) await xdo('mousemove', Math.round(coords.x), Math.round(coords.y));
  };
  switch (action) {
    case 'screenshot':
      return { image: (await screenshotPng()).toString('base64') };

    case 'cursor_position': {
      const { stdout } = await xdo('getmouselocation', '--shell');
      const m = Object.fromEntries(stdout.trim().split('\n').map((l) => l.split('=')));
      return { x: Number(m.X), y: Number(m.Y) };
    }

    case 'move_mouse':
      if (!coords) throw new Error('move_mouse needs coordinates');
      await xdo('mousemove', Math.round(coords.x), Math.round(coords.y));
      return;

    case 'click_mouse': {
      await moveIf();
      const button = BUTTONS[body.button ?? 'left'] ?? 1;
      const count = Math.max(1, Math.min(3, Number(body.clickCount ?? 1)));
      await withHeldKeys(body.holdKeys, () => xdo('click', '--repeat', count, '--delay', 90, button));
      return;
    }

    case 'press_mouse': {
      await moveIf();
      const button = BUTTONS[body.button ?? 'left'] ?? 1;
      await xdo(body.press === 'up' ? 'mouseup' : 'mousedown', button);
      return;
    }

    case 'drag_mouse': {
      const pts = body.path ?? [];
      if (pts.length < 2) throw new Error('drag_mouse needs a path of at least 2 points');
      const button = BUTTONS[body.button ?? 'left'] ?? 1;
      await withHeldKeys(body.holdKeys, async () => {
        await xdo('mousemove', Math.round(pts[0].x), Math.round(pts[0].y));
        await xdo('mousedown', button);
        for (const p of pts.slice(1)) {
          await sleep(60);
          await xdo('mousemove', Math.round(p.x), Math.round(p.y));
        }
        await sleep(60);
        await xdo('mouseup', button);
      });
      return;
    }

    case 'trace_mouse':
      for (const p of body.path ?? []) {
        await xdo('mousemove', Math.round(p.x), Math.round(p.y));
        await sleep(30);
      }
      return;

    case 'scroll': {
      await moveIf();
      const button = { up: 4, down: 5, left: 6, right: 7 }[body.direction ?? 'down'] ?? 5;
      const count = Math.max(1, Math.min(50, Number(body.scrollCount ?? 3)));
      await withHeldKeys(body.holdKeys, () => xdo('click', '--repeat', count, '--delay', 40, button));
      return;
    }

    case 'type_text':
    case 'paste_text': {
      const text = String(body.text ?? '');
      if (!text) return;
      // paste_text (Bytebot's explicit paste) goes through the clipboard; type_text is typed key by
      // key so the clipboard — the user's channel, mirrored to their machine — stays untouched.
      if (action === 'paste_text') {
        await pasteText(text);
        return;
      }
      // xdotool type drops "\n" (verified 2026-09-07), so lines are typed one by one with a Return
      // between them; the UTF-8 locale lets it type anything beyond ASCII (em dashes, accents, CJK).
      // Long text (an email body) is typed faster: 3 ms a key is still in order in Firefox and xterm.
      const delay = body.delay ?? (text.length > PASTE_OVER ? 3 : 12);
      const lines = text.split('\n');
      for (let i = 0; i < lines.length; i++) {
        if (i) await xdo('key', '--delay', '40', 'Return');
        if (lines[i]) await typeRun(lines[i], delay);
      }
      return;
    }

    case 'type_keys': {
      const keys = (body.keys ?? []).map(xKey);
      for (const k of keys) {
        await xdo('key', '--delay', body.delay ?? 40, k);
      }
      return;
    }

    case 'press_keys': {
      const keys = (body.keys ?? []).map(xKey);
      if (!keys.length) throw new Error('press_keys needs keys');
      await xdo(body.press === 'up' ? 'keyup' : 'keydown', ...keys);
      return;
    }

    case 'wait':
      await sleep(Math.min(30000, Math.max(0, Number(body.duration ?? 0))));
      return;

    case 'application': {
      const app = String(body.application ?? '');
      // A window of the desk's one Chromium, with the same network rules (see chromium-window.sh).
      const browser = ['/opt/desk/chromium-window.sh'];
      const cmd = { browser, chromium: browser, terminal: ['xterm', '-fa', 'Monospace', '-fs', '11'] }[app];
      if (!cmd) throw new Error(`unknown application "${app}" (browser, terminal)`);
      const child = execFile(cmd[0], cmd.slice(1), { detached: true, stdio: 'ignore' });
      child.unref();
      return;
    }

    case 'restart_browser':
      // browser.sh starts it again within a second.
      await run('pkill', ['-TERM', '-x', 'chromium']).catch(() => undefined);
      return { message: 'Chromium restarting' };

    case 'set_clipboard': {
      // Both selections so ctrl+v (CLIPBOARD) and middle-click (PRIMARY) paste the same text.
      const text = String(body.text ?? '');
      for (const sel of ['clipboard', 'primary']) await xclipWrite(sel, text);
      return;
    }

    case 'get_clipboard': {
      const { stdout } = await run('xclip', ['-selection', 'clipboard', '-o']).catch(() => ({ stdout: '' }));
      return { text: stdout };
    }

    case 'read_file': {
      const abs = safePath(body.path);
      const data = await fs.readFile(abs);
      return { data: data.toString('base64'), name: path.basename(abs), size: data.length, mediaType: 'application/octet-stream' };
    }

    case 'write_file': {
      const abs = safePath(body.path);
      await fs.mkdir(path.dirname(abs), { recursive: true });
      await fs.writeFile(abs, Buffer.from(String(body.data ?? ''), 'base64'));
      return { message: `wrote ${abs}` };
    }

    case 'list_files': {
      // Non-recursive listing, newest first. Used by the extension to watch ~/Downloads.
      const abs = safePath(body.path ?? '.');
      const names = await fs.readdir(abs).catch((err) => {
        if (err.code === 'ENOENT') return [];
        throw err;
      });
      const entries = [];
      for (const name of names) {
        const st = await fs.stat(path.join(abs, name)).catch(() => undefined);
        if (!st) continue;
        entries.push({ name, size: st.size, mtime: st.mtimeMs, dir: st.isDirectory() });
      }
      entries.sort((a, b) => b.mtime - a.mtime);
      return { path: abs, entries };
    }

    case 'release_input':
      // Let go of every key and button XTEST holds (see "stuck keys and buttons" above).
      return await releaseInput(String(body.reason ?? 'asked'));

    case 'input_state': {
      const state = await inputState();
      return state ?? { keys: [], buttons: [], unknown: true };
    }

    default:
      throw new Error(`unknown action "${action}"`);
  }
}

// actions are serialised: xdotool calls must not interleave
let queue = Promise.resolve();
const enqueue = (fn) => {
  const p = queue.then(fn, fn);
  queue = p.catch(() => {});
  return p;
};

// ---------- http ----------
/**
 * An action as the log shows it: typed and pasted text, clipboard text and file contents appear as
 * their length only. A session types passwords and one-time codes as readily as search terms, and
 * `docker logs` is easy to paste into a chat when asking for help.
 */
function forLog(body) {
  const out = { ...body };
  if (typeof out.text === 'string') out.text = `<${out.text.length} characters>`;
  if (typeof out.data === 'string') out.data = `<${out.data.length} base64 characters>`;
  return out;
}

function authorized(req) {
  if (!TOKEN) return true;
  const auth = req.headers.authorization ?? '';
  if (auth === `Bearer ${TOKEN}`) return true;
  const url = new URL(req.url, 'http://localhost');
  return url.searchParams.get('token') === TOKEN;
}

function json(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (!authorized(req)) return json(res, 401, { success: false, error: 'unauthorized' });

  if (req.method === 'GET' && url.pathname === '/') {
    try {
      return json(res, 200, { ok: true, screen: await screenSize(), auth: !!TOKEN });
    } catch (err) {
      return json(res, 500, { ok: false, error: String(err) });
    }
  }
  if (req.method === 'GET' && url.pathname === '/screenshot.png') {
    try {
      const png = await enqueue(screenshotPng);
      res.writeHead(200, { 'content-type': 'image/png', 'cache-control': 'no-store' });
      return res.end(png);
    } catch (err) {
      return json(res, 500, { success: false, error: String(err) });
    }
  }
  if (req.method === 'POST' && url.pathname.startsWith('/computer-use')) {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', async () => {
      let body;
      try {
        body = JSON.parse(raw || '{}');
      } catch {
        return json(res, 400, { success: false, error: 'invalid JSON' });
      }
      try {
        const data = await enqueue(() => handle(body));
        console.log(`${new Date().toISOString()} ${JSON.stringify(forLog(body))}`.slice(0, 200));
        return json(res, 200, data === undefined ? { success: true } : { success: true, data });
      } catch (err) {
        console.log(`${new Date().toISOString()} ${body.action} ERROR ${err.message}`);
        return json(res, 200, { success: false, error: err.message ?? String(err) });
      }
    });
    return;
  }
  json(res, 404, { success: false, error: 'not found' });
});

await fs.rm(SOCKET, { force: true });
server.listen(SOCKET, async () => {
  await fs.chmod(SOCKET, 0o666).catch(() => undefined);
  const size = await screenSize().catch(() => null);
  console.log(`deskd on ${SOCKET} screen=${size ? `${size.width}x${size.height}` : '?'} auth=${TOKEN ? 'token' : 'none'}`);
});
