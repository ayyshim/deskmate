"""The web wizard behind `./deskmate setup`: a small HTTP server on 127.0.0.1 and the page in web/.

The page renders the step model from steps.py (build contract §8) and calls back here to validate
answers, run checks and install. Nothing is written before Install: the answers, and any secret the
user pastes, stay in this process until steps.apply() runs.

Every program on this computer, and every page open in the browser, can reach 127.0.0.1, so:
- the link the browser opens carries a one-time key. The first visit trades it for an HttpOnly,
  SameSite=Strict cookie and redirects to a clean URL, and every /api/ call needs that cookie;
- the Host header must be 127.0.0.1:<port> or localhost:<port>, which stops DNS rebinding;
- a POST needs an Origin equal to this server's own and a JSON body, which stops cross-site forms,
  and a browser that says the request came from another site (Sec-Fetch-Site) is refused;
- secrets never go back to the page. It gets {"set", "masked"} only, and tokens, webhook URLs and
  sign-in links are masked in every message and log line it receives. The one exception is the
  sign-in link of a finished install: the page shows it (that is the point of the last step), so a
  run's result carries it, and so do POST /api/signin and GET /signin. Log lines never do.
"""

from __future__ import annotations

import hmac
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

from . import util

WEB_DIR = Path(__file__).resolve().parent / "web"
IDLE_TIMEOUT = 30 * 60  # seconds without the user doing anything; a running install never counts as idle
MAX_BODY = 1 << 20
EXIT_OK, EXIT_FAILED, EXIT_CANCELLED = 0, 1, 130

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
# The page is plain files from WEB_DIR: no inline script or style, nothing from another host.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
_STATIC_RE = re.compile(r"^(?:[A-Za-z0-9_-]+\.(?:html|css|js|svg)|fonts/[A-Za-z0-9_.-]+\.(?:woff2|txt))$")
_RUN_RE = re.compile(r"^/api/run/([A-Za-z0-9_-]{1,40})$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Secret keys known before the model says so: settings.read_secret() names and the old .env names.
KNOWN_SECRET_KEYS = frozenset({
    "claude-token", "notify-url", "hub-token", "CLAUDE_TOKEN", "NOTIFY_URL", "CLAUDE_CODE_OAUTH_TOKEN",
    "NOTIFY_WEBHOOK_URL", "DESKMATE_TOKEN",
})
LEVELS = ("info", "ok", "warn", "error")


def mask(value: str) -> str:
    """How the page shows a secret: enough to recognise it, never enough to use it."""
    value = str(value or "").strip()
    return util.mask_url(value) if value.startswith(("http://", "https://")) else util.mask_token(value)


def _clean(obj, known):
    """obj with every string scrubbed of the known secrets and of anything shaped like one."""
    if isinstance(obj, str):
        return util.scrub(obj, known) if obj else obj
    if isinstance(obj, dict):
        return {k: _clean(v, known) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v, known) for v in obj]
    return obj


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Run:
    """One install (or apply) run: the events steps.apply() emitted so far, and its result."""

    def __init__(self, rid: str):
        self.id = rid
        self.events = []
        self.done = False
        self.ok = None
        self.result = None
        self.phase = ""
        self.lock = threading.Lock()

    def add(self, event, known) -> tuple:
        """Store one event, scrubbed; returns (event, whether it starts a new phase)."""
        ev = dict(event) if isinstance(event, dict) else {"text": str(event)}
        with self.lock:
            ev = _clean(ev, known)
            ev["text"] = str(ev.get("text") if ev.get("text") is not None else "")
            ev["level"] = ev.get("level") if ev.get("level") in LEVELS else "info"
            ev["phase"] = str(ev.get("phase") or self.phase or "")
            ev.setdefault("ts", time.time())
            ev.setdefault("n", len(self.events) + 1)
            ev["i"] = len(self.events)
            self.events.append(ev)
            new_phase = ev["phase"] != self.phase
            self.phase = ev["phase"]
        return ev, new_phase

    def finish(self, ok: bool, result: dict) -> None:
        with self.lock:
            self.ok, self.result, self.done = ok, result, True

    def view(self, since: int = 0) -> dict:
        with self.lock:
            since = max(0, min(since, len(self.events)))
            return {"id": self.id, "done": self.done, "ok": self.ok, "events": self.events[since:],
                    "result": self.result, "n": len(self.events)}


class Wizard:
    """What the request threads share: the answers so far, pasted secrets, and install runs.

    `provider` is steps.py (or a stand-in with the same four functions: model, validate, action, apply).
    """

    def __init__(self, provider, out=None, idle_timeout: float = IDLE_TIMEOUT):
        self.provider = provider
        self.out = out or sys.stdout
        self.style = util.Style(self.out)
        self.idle_timeout = idle_timeout
        self.port = 0
        self.key = secrets.token_urlsafe(24)
        self.session = secrets.token_urlsafe(32)
        self.key_used = False
        self.values = None  # the page's latest answers, secrets as {set, masked}
        self.pending = {}  # KEY -> a secret the user pasted and a check accepted; written by apply only
        self.secret_keys = set(KNOWN_SECRET_KEYS)
        self.runs = {}
        self.active = None  # the run in progress
        self.last_run = None
        self.signin_url = ""
        self.lock = threading.RLock()  # one call into the provider at a time, and the state above
        self.stopped = threading.Event()
        self.exit_code = None
        self.touched = time.monotonic()

    # ------------------------------------------------------------ identity

    @property
    def hosts(self) -> set:
        return {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}

    @property
    def cookie_name(self) -> str:
        # Cookies ignore ports, so the hub (7800) and other local servers see this one too: the port
        # in the name keeps two wizards, or a wizard and the hub, from clobbering each other.
        return "deskmate_setup_%d" % self.port

    def url(self) -> str:
        return "http://127.0.0.1:%d/?key=%s" % (self.port, self.key)

    def use_key(self, key: str) -> bool:
        """True once: for the right key, the first time it is used."""
        with self.lock:
            if self.key_used or not hmac.compare_digest(str(key).encode(), self.key.encode()):
                return False
            self.key_used = True
            return True

    def session_ok(self, value: str) -> bool:
        return bool(value) and hmac.compare_digest(str(value).encode(), self.session.encode())

    def touch(self) -> None:
        self.touched = time.monotonic()

    # ------------------------------------------------------------ secrets

    def known_secrets(self) -> list:
        known = list(self.pending.values())
        if self.signin_url:
            known.append(self.signin_url)
        return known

    def clean(self, obj, extra=()):
        return _clean(obj, self.known_secrets() + [v for v in extra if v])

    def _merge(self, values, keep_new: bool = True):
        """The page's answers with the secrets it pasted earlier put back in.

        Returns (values for the provider, secrets the page sent in this request). The page sends a
        secret's text once, when the user presses Check or Use it; afterwards it holds {set, masked}.
        """
        vals = dict(values) if isinstance(values, dict) else dict(self.values or {})
        for k, v in vals.items():
            if isinstance(v, dict) and "set" in v and "masked" in v:
                self.secret_keys.add(k)
        new = {}
        for k in self.secret_keys:
            v = vals.get(k)
            if isinstance(v, str) and v.strip() and keep_new:
                new[k] = v.strip()
                vals[k] = v.strip()
            elif k in self.pending:
                vals[k] = self.pending[k]
            elif isinstance(v, str) and v.strip():  # a model refresh never takes a new secret
                vals.pop(k, None)
        return vals, new

    def _remember(self, values) -> None:
        """Keep the page's answers for a reload, never with a secret's text in them."""
        if not isinstance(values, dict):
            return
        old = self.values or {}
        kept = {}
        for k, v in values.items():
            if k in self.secret_keys and not isinstance(v, dict):
                v = self._secret_view(k, old.get(k))
            kept[k] = v
        self.values = kept

    def _secret_view(self, key, value) -> dict:
        if key in self.pending:
            return {"set": True, "masked": mask(self.pending[key]), "pending": True}
        if isinstance(value, dict):
            view = dict(value)
            view["set"] = bool(value.get("set"))
            view["masked"] = str(value.get("masked") or "")
            return view
        if isinstance(value, str) and value.strip():
            return {"set": True, "masked": mask(value)}
        return {"set": False, "masked": ""}

    def _secrets(self) -> dict:
        return {k: {"set": True, "masked": mask(v), "pending": True} for k, v in self.pending.items()}

    def _learn(self, model) -> None:
        for step in (model or {}).get("steps") or []:
            for field in step.get("fields") or []:
                if field.get("type") == "secret" and field.get("key"):
                    self.secret_keys.add(field["key"])

    def _mask_step(self, step):
        if not isinstance(step, dict):
            return step
        step = dict(step)
        fields = []
        for field in step.get("fields") or []:
            field = dict(field)
            if field.get("type") == "secret" or field.get("key") in self.secret_keys:
                field["value"] = self._secret_view(field.get("key"), field.get("value"))
                if not isinstance(field.get("default"), dict):
                    field["default"] = None
            fields.append(field)
        if "fields" in step:
            step["fields"] = fields
        return step

    def _mask_model(self, model):
        if not isinstance(model, dict):
            return model
        self._learn(model)
        model = dict(model)
        model["steps"] = [self._mask_step(s) for s in model.get("steps") or []]
        values = model.get("values")
        if isinstance(values, dict):
            values = dict(values)
            for k in list(values):
                if k in self.secret_keys:
                    values[k] = self._secret_view(k, values[k])
            model["values"] = values
        return model

    # ------------------------------------------------------------ the API

    def _busy(self) -> None:
        if self.active is not None and not self.active.done:
            raise HttpError(409, "Install is running. This page follows it; the terminal keeps going if you close the tab.")

    def server_info(self) -> dict:
        run = self.active or self.last_run
        return {
            "port": self.port,
            "run": {"id": run.id, "done": run.done, "ok": run.ok} if run else None,
            "signin": bool(self.signin_url),
            "ssh": bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")),
        }

    def get_model(self, values=None) -> dict:
        with self.lock:
            if isinstance(values, dict):
                self._remember(values)
            if self.values is None and not self.pending:
                model = self.provider.model(None)
            else:
                model = self.provider.model(self._merge(self.values, keep_new=False)[0])
            out = self._mask_model(model)
            out["server"] = self.server_info()
            out["secrets"] = self._secrets()
            return self.clean(out)

    def validate(self, body: dict) -> dict:
        step_id = str(body.get("step") or "")
        with self.lock:
            self._busy()
            vals, new = self._merge(body.get("values"))
            res = self.provider.validate(step_id, vals) or {}
            errors = {k: v for k, v in (res.get("errors") or {}).items() if v}
            for k, v in new.items():
                if not errors.get(k):
                    self.pending[k] = v
            self._remember(body.get("values"))
            out = {"errors": errors, "step": self._mask_step(res.get("step"))}
            if not errors:
                out["model"] = self._mask_model(self.provider.model(self._merge(self.values, keep_new=False)[0]))
            out["secrets"] = self._secrets()
            return self.clean(out, extra=new.values())

    def action(self, body: dict) -> dict:
        name = str(body.get("name") or "")
        if not _ACTION_RE.match(name):
            raise HttpError(400, "Unknown action.")
        with self.lock:
            self._busy()
            vals, new = self._merge(body.get("values"))
            res = self.provider.action(name, vals) or {}
            ok = bool(res.get("ok"))
            data = res.get("data") if isinstance(res.get("data"), dict) else {}
            # A pasted secret is kept only when its check passed, or the check could not run (data.keep).
            if new and (ok or data.get("keep")):
                self.pending.update(new)
            self._remember(body.get("values"))
            data = dict(data)
            if isinstance(data.get("model"), dict):
                data["model"] = self._mask_model(data["model"])
            if isinstance(data.get("step"), dict):
                data["step"] = self._mask_step(data["step"])
            out = {"ok": ok, "message": str(res.get("message") or ""), "data": data, "secrets": self._secrets()}
            return self.clean(out, extra=new.values())

    def start_apply(self, body: dict) -> dict:
        with self.lock:
            self._busy()
            vals, new = self._merge(body.get("values"))
            self.pending.update(new)
            self._remember(body.get("values"))
            run = Run("r%d%s" % (len(self.runs) + 1, secrets.token_hex(3)))
            self.runs[run.id] = run
            self.active = run
        threading.Thread(target=self._apply, args=(run, vals), name="deskmate-apply", daemon=True).start()
        return {"run": run.id}

    def run_view(self, rid: str, since: int) -> dict:
        run = self.runs.get(rid)
        if run is None:
            raise HttpError(404, "There is no such install run.")
        return run.view(since)

    def _apply(self, run: Run, values: dict) -> None:
        st = self.style
        self.say(st.bold("Installing.") + " The browser shows the whole log; here are the phases.")

        def emit(event):
            ev, new_phase = run.add(event, self.known_secrets())
            if new_phase and ev["phase"]:
                self.say("  %s %s" % ("▸" if st.utf8 else ">", ev["phase"]))
            if ev["level"] in ("warn", "error"):
                self.say("    %s %s" % (st.mark("warn" if ev["level"] == "warn" else "fail"), ev["text"]))

        try:
            result = self.provider.apply(values, emit)
            result = dict(result) if isinstance(result, dict) else {"ok": bool(result)}
        except Exception as exc:  # noqa: BLE001 - report it on the page and in the terminal, never crash the server
            result = {
                "ok": False, "failed_phase": run.phase or None,
                "hint": "Setup stopped on an unexpected error (%s). The terminal shows the details. Retry runs it again." % type(exc).__name__,
            }
            self.say(util.scrub(traceback.format_exc(), self.known_secrets()), err=True)
        ok = bool(result.get("ok"))
        url = str(result.get("signin_url") or "")
        if ok and url:
            self.signin_url = url
        shown = self.clean(dict(result))
        shown["ok"] = ok
        shown["signin"] = bool(ok and url)
        if ok and url:
            shown["signin_url"] = url  # the page shows and opens it; every log line keeps it masked
        run.finish(ok, shown)
        with self.lock:
            self.last_run = run
            self.active = None
        if ok:
            self.say(st.green(st.ok_mark) + " " + st.bold("Deskmate is running."))
            if url:
                self.say("  Sign in (the link works for this browser): " + url)
            self.say("  Finish in the browser, or press Ctrl+C here.")
        else:
            where = result.get("failed_phase") or run.phase or "a phase"
            self.say(st.mark("fail") + " Install stopped at %s. %s" % (where, util.scrub(str(result.get("hint") or ""), self.known_secrets())))
            self.say("  Press Retry in the browser. Finished phases are kept, so a retry is quick.")

    def signin(self) -> dict:
        if not self.signin_url:
            raise HttpError(404, "There is no sign-in link yet: install first.")
        return {"url": self.signin_url}

    def discard(self) -> dict:
        with self.lock:
            self._busy()
            self.pending.clear()
            self.values = None
        return {"ok": True}

    # ------------------------------------------------------------ stopping

    def _exit_without_finish(self) -> int:
        if self.last_run is None:
            return EXIT_CANCELLED
        return EXIT_OK if self.last_run.ok else EXIT_FAILED

    def finish(self) -> dict:
        with self.lock:
            self._busy()
            code = EXIT_OK if self.last_run is None or self.last_run.ok else EXIT_FAILED
        self.stop(code, "Setup is finished." if code == EXIT_OK else "Setup closed. Install didn't finish: run ./deskmate setup again to retry.")
        return {"ok": True}

    def cancel(self) -> dict:
        with self.lock:
            self._busy()
            code = self._exit_without_finish()
        self.stop(code, self._closing_words(code, "Setup cancelled."))
        return {"ok": True}

    def _closing_words(self, code: int, head: str) -> str:
        if code == EXIT_CANCELLED:
            return head + " Nothing was changed."
        if code == EXIT_FAILED:
            return head + " Install didn't finish: run ./deskmate setup again to retry."
        return head

    def check_idle(self) -> None:
        if self.active is not None and not self.active.done:
            self.touch()
            return
        if time.monotonic() - self.touched > self.idle_timeout:
            minutes = max(1, int(round(self.idle_timeout / 60.0)))
            code = self._exit_without_finish()
            self.stop(code, self._closing_words(code, "Setup closed after %d minutes without activity." % minutes))

    def interrupted(self) -> None:
        if self.active is not None and not self.active.done:
            self.stop(EXIT_CANCELLED, "Setup stopped while installing. Run ./deskmate setup again: it picks up where it stopped.")
        else:
            code = self._exit_without_finish()
            self.stop(code, self._closing_words(code, "Setup stopped."))

    def stop(self, code: int, message: str = "") -> None:
        with self.lock:
            if self.stopped.is_set():
                return
            self.exit_code = code
            self.stopped.set()
        if message:
            self.say(message)

    # ------------------------------------------------------------ the terminal

    def say(self, text: str, err: bool = False) -> None:
        stream = sys.stderr if err else self.out
        try:
            stream.write(text + "\n")
            stream.flush()
        except (OSError, ValueError, UnicodeEncodeError):
            pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    wizard = None  # set by make_server


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "deskmate-setup"
    sys_version = ""
    protocol_version = "HTTP/1.0"  # one request per connection: nothing lingers after the server stops

    def log_message(self, fmt, *args):  # quiet: the first request line carries the one-time key
        return

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _not_allowed(self):
        self._send_json(405, {"error": "Not allowed."})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    # ------------------------------------------------------------ routing

    def _dispatch(self, method: str) -> None:
        wiz = self.server.wizard
        try:
            host = (self.headers.get("Host") or "").strip().lower()
            if host not in wiz.hosts:
                return self._send_text(400, "This page answers only on 127.0.0.1:%d.\n" % wiz.port)
            if wiz.stopped.is_set():
                return self._send_json(410, {"error": "Setup has ended. Run ./deskmate setup again to change anything."})
            path, _, query = self.path.partition("?")
            if not path.startswith("/"):
                return self._send_text(400, "Bad request.\n")
            if path.startswith("/api/"):
                return self._api(method, path, query, host)
            if method != "GET":
                return self._send_json(405, {"error": "Not allowed."})
            if path == "/signin":
                return self._signin_redirect()
            return self._static(path, query)
        except HttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - one bad request must not take the wizard down
            wiz.say(util.scrub(traceback.format_exc(), wiz.known_secrets()), err=True)
            try:
                self._send_json(500, {"error": "Setup hit an unexpected error (%s). The terminal shows the details." % type(exc).__name__})
            except OSError:
                pass

    def _api(self, method: str, path: str, query: str, host: str) -> None:
        wiz = self.server.wizard
        if not wiz.session_ok(self._cookie(wiz.cookie_name)):
            return self._send_json(401, {"error": "This tab isn't signed in to setup. Open the link the terminal printed, or run ./deskmate setup again."})
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return self._send_json(403, {"error": "Refused: the request came from another site."})
        body = {}
        if method == "POST":
            if self.headers.get("Origin") != "http://" + host:
                return self._send_json(403, {"error": "Refused: the request didn't come from the setup page."})
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._send_json(415, {"error": "Send JSON."})
            body = self._read_json()
        wiz.touch()
        if method == "GET" and path == "/api/model":
            return self._send_json(200, wiz.get_model())
        m = _RUN_RE.match(path)
        if method == "GET" and m:
            try:
                since = int(urllib.parse.parse_qs(query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            return self._send_json(200, wiz.run_view(m.group(1), since))
        if method != "POST":
            return self._send_json(404 if method == "GET" else 405, {"error": "Not found."})
        if path == "/api/model":
            return self._send_json(200, wiz.get_model(body.get("values")))
        if path == "/api/validate":
            return self._send_json(200, wiz.validate(body))
        if path == "/api/action":
            return self._send_json(200, wiz.action(body))
        if path == "/api/apply":
            return self._send_json(202, wiz.start_apply(body))
        if path == "/api/ping":
            return self._send_json(200, {"ok": True})
        if path == "/api/discard":
            return self._send_json(200, wiz.discard())
        if path == "/api/signin":
            return self._send_json(200, wiz.signin())
        if path == "/api/cancel":
            out = wiz.cancel()
            return self._send_json(200, out)
        if path == "/api/finish":
            out = wiz.finish()
            return self._send_json(200, out)
        return self._send_json(404, {"error": "Not found."})

    def _static(self, path: str, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        if path == "/" and "key" in params:
            return self._exchange_key(params["key"][0])
        name = "index.html" if path in ("/", "/index.html") else urllib.parse.unquote(path[1:])
        if not _STATIC_RE.match(name):
            return self._send_text(404, "Not found.\n")
        file = (WEB_DIR / name).resolve()
        try:
            file.relative_to(WEB_DIR)
            data = file.read_bytes()
        except (ValueError, OSError):
            return self._send_text(404, "Not found.\n")
        ctype = CONTENT_TYPES.get(file.suffix, "application/octet-stream")
        self._send(200, data, ctype)

    def _exchange_key(self, key: str) -> None:
        wiz = self.server.wizard
        first = wiz.use_key(key)
        if first or wiz.session_ok(self._cookie(wiz.cookie_name)):
            extra = [("Location", "/")]
            if first:
                extra.append(("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (wiz.cookie_name, wiz.session)))
            return self._send(303, b"", "text/plain; charset=utf-8", extra)
        self._send_text(403, "This setup link was used already, or it is wrong.\n"
                             "If this tab lost its place, run ./deskmate setup again for a new link.\n")

    def _signin_redirect(self) -> None:
        wiz = self.server.wizard
        site = self.headers.get("Sec-Fetch-Site")
        if not wiz.session_ok(self._cookie(wiz.cookie_name)) or (site and site not in ("same-origin", "none")):
            return self._send_text(401, "Open this from the setup page.\n")
        if not wiz.signin_url:
            return self._send_text(404, "There is no sign-in link yet: install first.\n")
        wiz.touch()
        self._send(303, b"", "text/plain; charset=utf-8", [("Location", wiz.signin_url)])

    # ------------------------------------------------------------ helpers

    def _cookie(self, name: str) -> str:
        # By hand: SimpleCookie gives up on a whole header when another 127.0.0.1 app sets an odd cookie.
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, sep, v = part.strip().partition("=")
            if sep and k == name:
                return v.strip()
        return ""

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Bad Content-Length.")
        if length < 0 or length > MAX_BODY:
            raise HttpError(413, "That request is too large.")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "That isn't valid JSON.")
        if not isinstance(body, dict):
            raise HttpError(400, "Send a JSON object.")
        return body

    def _send_json(self, status: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send(self, status: int, data: bytes, ctype: str, extra=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)


def make_server(wizard: Wizard, port: int = 0) -> _Server:
    """Bind 127.0.0.1:port (0 picks a free one) and attach the wizard. Raises OSError if it is taken."""
    httpd = _Server(("127.0.0.1", port), _Handler)
    httpd.wizard = wizard
    wizard.port = httpd.server_address[1]
    return httpd


def serve(open_browser: bool = True, port: int = 0, *, provider=None, idle_timeout: float = IDLE_TIMEOUT, out=None) -> int:
    """Run the web wizard until the user finishes or cancels it, presses Ctrl+C, or leaves it idle.

    Returns 0 when setup finished (or was closed after a successful install), 1 when the last install
    failed, 130 when it was cancelled before anything was installed. `provider` defaults to steps.py.
    """
    out = out or sys.stdout
    if provider is None:
        from . import steps as provider
    wiz = Wizard(provider, out=out, idle_timeout=idle_timeout)
    try:
        httpd = make_server(wiz, port)
    except OSError as exc:
        wiz.say("Setup couldn't listen on 127.0.0.1:%d (%s). Choose another port with --port, or leave it out."
                % (port, exc.strerror or exc))
        return EXIT_FAILED
    url = wiz.url()
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, name="deskmate-setup-http", daemon=True).start()
    opened = util.open_url(url) if open_browser else False
    st = wiz.style
    wiz.say(st.bold("Deskmate setup") + (" is open in your browser. If it didn't open, use this link:" if opened
                                        else ": open this link in your browser."))
    wiz.say("  " + url)
    wiz.say(st.dim("The link works once. Nothing changes on this computer until you confirm on the last step."))
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        wiz.say(st.dim("Over SSH: on your own computer run  ssh -L %d:127.0.0.1:%d <this host>  and open the link there."
                       % (wiz.port, wiz.port)))
    wiz.say(st.dim("Press Ctrl+C to stop setup."))
    try:
        while not wiz.stopped.wait(0.5):
            wiz.check_idle()
    except KeyboardInterrupt:
        wiz.say("")
        wiz.interrupted()
    finally:
        httpd.shutdown()
        httpd.server_close()
    return wiz.exit_code if wiz.exit_code is not None else EXIT_CANCELLED
