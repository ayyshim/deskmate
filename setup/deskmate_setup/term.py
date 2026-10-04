"""The setup wizard in a terminal (./deskmate setup --terminal, and over SSH), plus the non-interactive
setup (--defaults, --answers FILE). It walks the same step model as the web wizard (steps.model,
validate, action, apply), so the defaults, the checks and the wording are the same.

How it asks: the current value in brackets, Enter keeps it; yes/no as [Y/n]; choices as numbered lists;
folders as a numbered checklist (numbers toggle, Enter accepts); secrets without echo, repeated back
masked. Nothing on the computer changes before Install; Ctrl+C before it says so.
"""

from __future__ import annotations

import os
import re
import sys

from . import compose, detect, settings, steps, util

OK, FAILED, USAGE, PREREQ, CANCELLED = 0, 1, 2, 3, 130
MAX_LIST = 15  # longer choice lists (time zones) are typed, not numbered
_SECRET_NAMES = {"claude-token", "notify-url", "hub-token"} | set(settings.ALIASES) | {"DESKMATE_TOKEN"}
# Secrets in non-interactive mode: only from the environment, or from a file named by a *_FILE key.
SECRET_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "claude-token", "NOTIFY_WEBHOOK_URL": "notify-url", "NOTIFY_URL": "notify-url"}
SECRET_FILE_KEYS = {"CLAUDE_CODE_OAUTH_TOKEN_FILE": "claude-token", "NOTIFY_WEBHOOK_FILE": "NOTIFY_URL_FILE"}


class Cancelled(Exception):
    """Ctrl+C, or the input ended, before anything was changed."""


def _plain_md(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", str(text or ""))


class Term:
    """Reading and writing lines. Input and output are streams so tests can drive it."""

    def __init__(self, inp=None, out=None):
        self.inp = inp or sys.stdin
        self.out = out or sys.stdout
        self.style = util.Style(self.out)

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def ask(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        try:
            line = self.inp.readline()
        except KeyboardInterrupt:
            raise Cancelled() from None
        if not line:
            raise Cancelled()
        return line.rstrip("\r\n")

    def secret(self, prompt: str) -> str:
        """Read without echo on a terminal (getpass); from the stream as is otherwise (tests, pipes)."""
        if self.inp is sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            import getpass

            try:
                return getpass.getpass(prompt).strip()
            except (KeyboardInterrupt, EOFError):
                raise Cancelled() from None
        return self.ask(prompt).strip()

    def yes_no(self, prompt: str, default: bool) -> bool:
        while True:
            ans = self.ask(f"{prompt} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
            if not ans:
                return default
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            self.say("  Type y or n.")

    def check_line(self, c: dict) -> None:
        st = self.style
        self.say(f"  {st.mark(c.get('status', 'ok'))} {c.get('label', '')}")
        if c.get("status") != "ok" and c.get("detail"):
            self.say(f"      {st.dim(c['detail'])}")
        if c.get("status") != "ok" and c.get("fix"):
            self.say(f"      {st.arrow} {c['fix']}")


# ---------------------------------------------------------------- one field


def _matches(cur, wanted) -> bool:
    for w in wanted if isinstance(wanted, (list, tuple)) else [wanted]:
        if isinstance(w, bool):
            if util.is_on(cur) == w and str(cur).strip().lower() in ("on", "off", "true", "false", "1", "0", "yes", "no", "y", "n"):
                return True
        elif str(cur) == str(w):
            return True
    return False


class Wizard:
    def __init__(self, term: Term, answers: dict | None = None):
        self.t = term
        self.answers = answers if answers is not None else {}
        self.base = {}

    # values as they stand: what the user typed over what is saved or detected
    def current(self, key: str):
        if key in self.answers:
            return self.answers[key]
        return self.base.get(key)

    def visible(self, f: dict) -> bool:
        for k, wanted in (f.get("show_if") or {}).items():
            cur = self.current(k)
            if isinstance(cur, dict):
                cur = "on" if cur.get("set") else ""
            if not _matches("" if cur is None else (("on" if cur else "off") if isinstance(cur, bool) else cur), wanted):
                return False
        return True

    def _help(self, f: dict) -> None:
        if f.get("help"):
            self.t.say("  " + self.t.style.dim(f["help"]))

    def _shown(self, f: dict) -> str:
        v = f.get("value")
        if isinstance(v, bool):
            return "on" if v else "off"
        if isinstance(v, list):
            return ", ".join(util.tilde(str(x)) for x in v) or "none"
        return util.tilde(str("" if v is None else v))

    def ask_field(self, f: dict, step: dict) -> None:
        key, kind = f["key"], f.get("type")
        label = f.get("label") or key
        if f.get("readonly"):
            self.t.say(f"  {label}: {self._shown(f)}")
            return
        if kind == "secret":
            return self._ask_secret(f)
        if kind == "bool":
            self._help(f)
            self.answers[key] = "on" if self.t.yes_no(f"{label}?", util.is_on(f.get("value"))) else "off"
            return
        if kind == "folders":
            return self._ask_folders(f, step)
        if kind in ("multi", "paths") and f.get("choices"):
            return self._ask_checklist(f, sep="," if kind == "multi" else ":")
        if f.get("choices") and len(f["choices"]) <= MAX_LIST:
            return self._ask_choice(f)
        self._help(f)
        cur = self._shown(f) if not isinstance(f.get("value"), list) else ":".join(f["value"])
        while True:
            raw = self.t.ask(f"{label} [{cur}]: ").strip()
            if not raw:
                if key not in self.answers and f.get("value") not in (None, ""):
                    self.answers[key] = ":".join(f["value"]) if isinstance(f["value"], list) else str(f["value"])
                return
            if raw == "-" and kind in ("path", "paths", "text"):
                self.answers[key] = ""
                return
            if kind in ("path", "paths"):
                raw = ":".join(os.path.abspath(os.path.expanduser(p)) for p in util.split_list(raw))
            if f.get("choices"):  # a long list, typed: accept the value or its label, any case
                found = [c["value"] for c in f["choices"] if raw.lower() in (str(c["value"]).lower(), str(c["label"]).lower())]
                if not found:
                    self.t.say("  That isn't in the list. " + ("For example: " + ", ".join(
                        c["value"] for c in f["choices"] if raw.lower() in str(c["value"]).lower())[:200]
                        if any(raw.lower() in str(c["value"]).lower() for c in f["choices"]) else ""))
                    continue
                raw = found[0]
            self.answers[key] = raw
            return

    def _ask_choice(self, f: dict) -> None:
        self._help(f)
        choices = f["choices"]
        cur = str(f.get("value") if not isinstance(f.get("value"), bool) else ("on" if f["value"] else "off"))
        default = next((i for i, c in enumerate(choices, 1) if str(c["value"]) == cur), None)
        for i, c in enumerate(choices, 1):
            off = " (not available here)" if c.get("disabled") else ""
            detail = f" {self.t.style.dim(c['detail'])}" if c.get("detail") else ""
            self.t.say(f"  {i}) {c['label']}{off}{detail}")
        while True:
            raw = self.t.ask(f"{f.get('label') or f['key']} [{default if default else cur}]: ").strip()
            if not raw:
                if default:
                    self.answers[f["key"]] = str(choices[default - 1]["value"])
                return
            pick = None
            if f.get("type") == "path" and (raw.startswith("/") or raw.startswith("~")):
                self.answers[f["key"]] = os.path.abspath(os.path.expanduser(raw))
                return
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                pick = choices[int(raw) - 1]
            else:
                pick = next((c for c in choices if str(c["value"]).lower() == raw.lower()), None)
            if pick is None:
                self.t.say(f"  Type a number from 1 to {len(choices)}.")
                continue
            if pick.get("disabled"):
                self.t.say("  That one can't be used here: " + str(pick.get("detail", "")))
                continue
            self.answers[f["key"]] = str(pick["value"])
            return

    def _toggle_loop(self, rows: list, chosen: list, title: str, allow_paths: bool) -> list:
        """Numbered checklist: numbers toggle, a path adds a folder, Enter accepts."""
        st = self.t.style
        while True:
            for i, r in enumerate(rows, 1):
                mark = "[x]" if r["value"] in chosen else ("[~]" if r.get("included") and any(
                    r.get("parent") == c for c in chosen) else "[ ]")
                pad = "    " * int(r.get("depth") or 0)
                extra = f"  {st.dim(r['detail'])}" if r.get("detail") else ""
                self.t.say(f"  {mark} {i:>2} {pad}{r['label']}{extra}")
            hint = "Numbers toggle" + (", a path adds a folder" if allow_paths else "") + ", Enter accepts: "
            raw = self.t.ask(f"{title}. {hint}").strip()
            if not raw:
                return chosen
            if allow_paths and (raw.startswith("/") or raw.startswith("~")):
                p = os.path.abspath(os.path.expanduser(raw)).rstrip("/") or "/"
                if p not in chosen:
                    chosen.append(p)
                if all(r["value"] != p for r in rows):
                    rows.append({"value": p, "label": util.tilde(p), "detail": "added", "depth": 0})
                continue
            nums = re.split(r"[\s,]+", raw)
            if not all(n.isdigit() and 1 <= int(n) <= len(rows) for n in nums):
                self.t.say(f"  Type numbers from 1 to {len(rows)}" + (", or a folder's path." if allow_paths else "."))
                continue
            for n in nums:
                v = rows[int(n) - 1]["value"]
                if v in chosen:
                    chosen.remove(v)
                else:
                    chosen.append(v)

    def _ask_checklist(self, f: dict, sep: str) -> None:
        self._help(f)
        rows = [{"value": c["value"], "label": c["label"], "detail": c.get("detail", "")} for c in f["choices"]]
        chosen = [str(x) for x in (f.get("value") or [])]
        for c in chosen:
            if all(r["value"] != c for r in rows):
                rows.append({"value": c, "label": util.tilde(c), "detail": ""})
        chosen = self._toggle_loop(rows, list(chosen), f.get("label") or f["key"], allow_paths=f.get("type") == "paths")
        self.answers[f["key"]] = sep.join(chosen)

    def _ask_folders(self, f: dict, step: dict) -> None:
        self._help(f)
        rows = []
        for r in (step.get("info") or {}).get("folders") or []:
            bits = []
            if r.get("sessions"):
                bits.append(f"{r['sessions']} session{'s' if r['sessions'] != 1 else ''}")
            if r.get("last"):
                bits.append(r["last"])
            if r.get("repos"):
                bits.append(f"{r['repos']} repo{'s' if r['repos'] != 1 else ''}")
            if r.get("hub"):
                bits.append(f"hub {r['hub']}")
            if r.get("note"):
                bits.append(r["note"])
            rows.append({"value": r["path"], "label": r.get("label") or util.tilde(r["path"]),
                         "detail": " · ".join(bits), "depth": r.get("depth", 0), "parent": r.get("parent"),
                         "included": r.get("included")})
        chosen = [str(x) for x in (f.get("value") or [])]
        for c in chosen:
            if all(r["value"] != c for r in rows):
                rows.append({"value": c, "label": util.tilde(c), "detail": "", "depth": 0})
        if (step.get("info") or {}).get("line"):
            self.t.say("  " + step["info"]["line"])
        chosen = self._toggle_loop(rows, list(chosen), f.get("label") or f["key"], allow_paths=True)
        self.answers[f["key"]] = ":".join(chosen)

    def _ask_secret(self, f: dict) -> None:
        key = f["key"]
        state = f.get("value") if isinstance(f.get("value"), dict) else {}
        self._help(f)
        if f.get("command"):
            self.t.say(f"  In another terminal, run: {f['command']}")
        keep = state.get("masked") if state.get("set") else "none"
        raw = self.t.secret(f"{f.get('label') or key} (hidden; Enter keeps {keep}; - removes it): ")
        if not raw:
            return
        if raw == "-":
            self.answers[key] = ""
            self.t.say("  Removed.")
            return
        self.answers[key] = raw
        masked = util.mask(raw, "url" if key == "notify-url" else "token")
        if f.get("action") == "check_token":
            res = steps.action("check_token", self.answers)
            self.t.say(f"  {masked} {self.t.style.mark('ok' if res.get('ok') else 'warn')} {_plain_md(res.get('message'))}")
        elif f.get("action") == "test_notify":
            self.t.say(f"  {masked}")
            if self.t.yes_no("Send a test message now?", False):
                res = steps.action("test_notify", self.answers)
                self.t.say(f"  {self.t.style.mark('ok' if res.get('ok') else 'warn')} {_plain_md(res.get('message'))}")
        else:
            self.t.say(f"  {masked}")

    # ---------------------------------------------------------------- one step

    def _print_preview(self, name: str) -> None:
        res = steps.action(name, self.answers)
        data = res.get("data") or {}
        items = data.get("changes") or []
        if not res.get("ok") and res.get("message"):
            self.t.say(f"  {self.t.style.mark('warn')} {util.scrub(res['message'])}")
        if not items:
            if res.get("ok"):
                self.t.say("  " + (_plain_md(res.get("message")) or "Nothing changes."))
            return
        self.t.say("  What changes:")
        for c in items:
            where = f" ({util.tilde(str(c.get('where')))})" if c.get("where") else ""
            self.t.say(f"    - {c.get('what', '')}{where}")
            if c.get("detail"):
                self.t.say(f"      {self.t.style.dim(str(c['detail']))}")
        diffs = [c for c in items if c.get("diff")]
        if diffs and self.t.yes_no("Show the text that changes?", False):
            for c in diffs:
                self.t.say(f"  --- {util.tilde(str(c.get('where')))}")
                for line in str(c["diff"]).splitlines():
                    self.t.say("    " + line)

    def step(self, sid: str, number: int, total: int) -> dict:
        res = steps.validate(sid, self.answers)
        s = res["step"]
        self.t.say()
        self.t.say(self.t.style.bold(f"{number}/{total} {s['title']}"))
        if s.get("skip"):
            self.t.say(f"  {s.get('summary', '')}")
            return s
        if s.get("intro"):
            self.t.say(s["intro"])
        for c in s.get("checks") or []:
            self.t.check_line(c)
        if sid == "connect" and any(a.get("name") == "preview_connect" for a in (s.get("info") or {}).get("actions") or []):
            self._print_preview("preview_connect")
        fields = [f for f in s.get("fields") or []]
        for f in [f for f in fields if not f.get("advanced")]:
            if self.visible(f):
                self.ask_field(self._fresh(s, f), s)
                s = self._refresh(sid, s, f)
        adv = [f for f in fields if f.get("advanced")]
        if adv and any(self.visible(f) for f in adv):
            title = (s.get("info") or {}).get("advanced_title") or "the advanced settings"
            if self.t.yes_no(f"Change {title[0].lower() + title[1:]}?", False):
                for f in adv:
                    if self.visible(f):
                        self.ask_field(self._fresh(s, f), s)
                        s = self._refresh(sid, s, f)
        if sid == "habits" and util.is_on(self.current("HABITS")):
            if self.t.yes_no("Show the changes in your folders?", True):
                self._print_preview("preview_habits")
        return self._settle(sid)

    @staticmethod
    def _fresh(s: dict, f: dict) -> dict:
        return next((x for x in s.get("fields") or [] if x.get("key") == f.get("key")), f)

    def _refresh(self, sid: str, s: dict, f: dict) -> dict:
        """Steps whose later fields depend on an earlier answer (the folder list on Claude Code's folder)."""
        if f.get("key") in ("CLAUDE_CONFIG_DIRS", "SECRETARY", "HABITS", "NOTIFY_KIND", "HABITS_HUB", "CONNECT"):
            return steps.validate(sid, self.answers)["step"]
        return s

    def _settle(self, sid: str) -> dict:
        """Validate; ask again only the fields with an error, until there are none."""
        for _ in range(20):
            res = steps.validate(sid, self.answers)
            s = res["step"]
            errors = res.get("errors") or {}
            for k, msg in (res.get("warnings") or {}).items():
                if k not in errors:
                    self.t.say(f"  {self.t.style.mark('warn')} {msg}")
            if not errors:
                self.t.say(f"  {self.t.style.mark('ok')} {s.get('summary') or 'Done'}")
                return s
            by_key = {f["key"]: f for f in s.get("fields") or []}
            for k, msg in errors.items():
                self.t.say(f"  {self.t.style.mark('fail')} {by_key.get(k, {}).get('label', k)}: {msg}")
                if k in by_key:
                    self.ask_field(by_key[k], s)
            if not any(k in by_key for k in errors):
                return s
        return s


# ---------------------------------------------------------------- the interactive wizard


ASK_STEPS = steps.STEP_IDS[1:-2]  # you … connect
TOTAL = len(steps.STEP_IDS) - 1   # review is the last numbered step; done is not


def _check_step(w: Wizard) -> dict:
    t = w.t
    m = steps.model(w.answers)
    w.base = m.get("values") or {}
    check = m["steps"][0]
    while True:
        t.say(t.style.bold(f"1/{TOTAL} {check['title']}"))
        t.say(check.get("intro", ""))
        for c in check.get("checks") or []:
            t.check_line(c)
        t.say("  " + str((check.get("info") or {}).get("line", "")))
        if not (check.get("info") or {}).get("blocking"):
            return m
        raw = t.ask("Fix it and press Enter to check again, s to carry on (Install waits), q to stop: ").strip().lower()
        if raw == "q":
            raise Cancelled()
        if raw == "s":
            return m
        res = steps.action("recheck", w.answers)
        m = res["data"]["model"]
        w.base = m.get("values") or {}
        check = m["steps"][0]


def _review(w: Wizard, mode: str) -> dict:
    t = w.t
    res = steps.validate("review", w.answers)
    s = res["step"]
    t.say()
    t.say(t.style.bold(f"{TOTAL}/{TOTAL} {s['title']}"))
    if s.get("intro"):
        t.say(s["intro"])
    for r in (s.get("info") or {}).get("rows") or []:
        t.say(f"  {r['title']:<20} {r['summary']}")
    if mode == "edit":
        ch = (s.get("info") or {}).get("changes") or []
        t.say("  Your changes:" if ch else "  No changes to the saved settings.")
        for c in ch:
            t.say(f"    {c['label']}: {util.tilde(str(c['old']))} {t.style.arrow} {util.tilde(str(c['new']))} ({c['tag']})")
    for c in s.get("checks") or []:
        t.check_line(c)
    errors = res.get("errors") or {}
    for k, msg in errors.items():
        lab = settings.KEYS[k].label if k in settings.KEYS else k
        t.say(f"  {t.style.mark('fail')} {lab}: {msg}")
    return res


def _section_menu(w: Wizard, total: int) -> None:
    """Edit mode: the saved settings as sections; change any of them, in any order."""
    t = w.t
    while True:
        m = steps.model(w.answers)
        rows = [s for s in m["steps"] if s["id"] in ASK_STEPS]
        t.say()
        t.say(t.style.bold("Settings"))
        for i, s in enumerate(rows, 1):
            t.say(f"  {i}) {s['title']:<20} {s.get('summary', '')}")
        raw = t.ask("Number of a section to change, Enter when done: ").strip()
        if not raw:
            return
        if not raw.isdigit() or not 1 <= int(raw) <= len(rows):
            t.say(f"  Type a number from 1 to {len(rows)}.")
            continue
        sid = rows[int(raw) - 1]["id"]
        w.step(sid, steps.STEP_IDS.index(sid) + 1, total)


def _log_path(values: dict):
    return settings.data_dir(values) / "setup.log"


def _install(w: Wizard, yes: bool) -> int:
    t = w.t
    lines = []
    printer = _event_printer(t, lines)
    start = None
    while True:
        result = steps.apply(w.answers, printer, start=start)
        if result.get("ok"):
            return _done(t, result)
        t.say()
        t.say(f"{t.style.mark('fail')} {result.get('failed_phase')}: {result.get('hint')}")
        path = _save_log(lines, w.answers)
        if path:
            t.say(f"  The whole log: {util.tilde(str(path))}")
        if yes:
            return FAILED
        try:
            raw = t.ask("r to retry this phase, Enter to stop: ").strip().lower()
        except Cancelled:
            return FAILED
        if raw != "r":
            t.say("Run ./deskmate setup again to carry on; your answers are saved.")
            return FAILED
        start = result.get("failed_phase")


def _save_log(lines: list, values: dict):
    try:
        path = _log_path(settings.merged(values))
        if not path.parent.is_dir():
            return None
        util.write_private(path, "\n".join(lines) + "\n", 0o600)
        return path
    except OSError:
        return None


def _event_printer(t: Term, lines: list):
    last = {"phase": None}

    def emit(ev: dict) -> None:
        phase = ev.get("phase") or ""
        if phase != last["phase"]:
            last["phase"] = phase
            t.say(t.style.bold(f"{t.style.arrow} {phase}"))
        level = ev.get("level", "info")
        text = str(ev.get("text", ""))
        lines.append(f"[{phase}] {level}: {text}")
        t.say(f"  {text}" if level == "info" else f"  {t.style.mark(level)} {text}")

    return emit


def _done(t: Term, result: dict, open_browser: bool = True) -> int:
    m = steps.validate("done", {})["step"]
    info = m.get("info") or {}
    t.say()
    t.say(t.style.bold(info.get("heading") or "Deskmate is running"))
    url = result.get("signin_url") or ""
    if url:
        t.say("Sign in with this link (it holds your sign-in token; don't share it):")
        t.say(f"  {url}")
        if open_browser and util.can_open_browser():
            util.open_url(url)
    if result.get("stopped_after"):
        t.say(f"Stopped after {result['stopped_after']}, as asked (--dry-run).")
        return OK
    if info.get("prompt"):
        t.say("Try it: in a Claude Code session, ask")
        t.say(f"  “{info['prompt']}”")
    if m.get("intro"):
        t.say(m["intro"])
    for line in info.get("where") or []:
        t.say(f"  {line}")
    t.say("Later: " + ", ".join(info.get("commands") or []))
    return OK


def run(yes: bool = False, inp=None, out=None, answers: dict | None = None) -> int:
    """The interactive terminal wizard. Returns an exit code."""
    t = Term(inp, out)
    w = Wizard(t, answers)
    applied = False
    try:
        mode = "edit" if settings.installed() else "install"
        t.say(t.style.bold("Deskmate setup") + (" — your current settings; Enter keeps each one" if mode == "edit" else ""))
        t.say(f"Enter accepts the value in brackets. Nothing changes until you {'apply' if mode == 'edit' else 'install'}; "
              "Ctrl+C stops.")
        t.say()
        _check_step(w)
        if mode == "edit":
            _section_menu(w, TOTAL)
        else:
            for i, sid in enumerate(ASK_STEPS, 2):
                w.step(sid, i, TOTAL)
        while True:
            res = _review(w, mode)
            s = res["step"]
            errors = res.get("errors") or {}
            if errors:
                raw = t.ask("Enter to fix these answers, q to stop: ").strip().lower()
                if raw == "q":
                    raise Cancelled()
                for sid in ASK_STEPS:
                    if steps.validate(sid, w.answers).get("errors"):
                        w.step(sid, steps.STEP_IDS.index(sid) + 1, TOTAL)
                continue
            if s.get("status") == "fail":
                raw = t.ask("Install waits for the problems above. Fix them, then press Enter to check again, or q to stop: ")
                if raw.strip().lower() == "q":
                    t.say("Nothing changed.")
                    return PREREQ
                detect.clear_cache()
                continue
            if mode == "edit" and not (s.get("info") or {}).get("changes") and not yes:
                if not t.yes_no("Nothing changed. Apply the settings again anyway?", False):
                    t.say("Nothing changed.")
                    return OK
            elif not yes and not t.yes_no("Apply the changes?" if mode == "edit" else "Install?", True):
                t.say("Nothing changed.")
                return FAILED
            break
        applied = True
        return _install(w, yes)
    except KeyboardInterrupt:
        compose.cancel()
        t.say()
        t.say("Stopped. Run ./deskmate setup again to carry on." if applied else "Nothing changed.")
        return CANCELLED
    except Cancelled:
        t.say()
        if applied:
            compose.cancel()
            t.say("Stopped. Run ./deskmate setup again to carry on.")
        else:
            t.say("Nothing changed.")
        return CANCELLED


# ---------------------------------------------------------------- non-interactive


def parse_answers(text: str) -> tuple:
    """(answers, errors) from a KEY=value file: the same keys as .env, no secrets. A secret comes from the
    environment, or from a file a *_FILE key names (CLAUDE_CODE_OAUTH_TOKEN_FILE, NOTIFY_WEBHOOK_FILE)."""
    answers, errors = {}, []
    allowed = {s.key for s in settings.SETTINGS if not s.secret and s.key not in settings.DERIVED}
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = settings._LINE_RE.match(line)
        if not m:
            errors.append(f"line {n}: not KEY=value")
            continue
        key = m.group(1)
        value = settings._unquote(m.group(2))
        if key in _SECRET_NAMES or key in SECRET_ENV or key.lower() in ("claude-token", "notify-url"):
            errors.append(f"line {n}: {key} is a secret. Secrets never go in an answers file: set the environment "
                          f"variable instead, or point a *_FILE key at a file (CLAUDE_CODE_OAUTH_TOKEN_FILE, NOTIFY_WEBHOOK_FILE)")
            continue
        if key in SECRET_FILE_KEYS:
            answers[key] = value
            continue
        if key not in allowed:
            errors.append(f"line {n}: unknown key {key}")
            continue
        answers[key] = value
    return answers, errors


def _secrets_from(answers: dict, env) -> list:
    """Move *_FILE keys and secret environment variables into the answers. Returns error lines."""
    errors = []
    files = {k: answers.pop(k) for k in list(answers) if k in SECRET_FILE_KEYS}
    for k in SECRET_FILE_KEYS:
        if env.get(k):
            files[k] = env[k]
    for k, path in files.items():
        target = SECRET_FILE_KEYS[k]
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            errors.append(f"{k}: {util.tilde(path)} doesn't exist")
            continue
        if target == "NOTIFY_URL_FILE":  # mounted read-only, never copied
            answers["NOTIFY_URL_FILE"] = path
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                answers[target] = fh.read().strip()
        except OSError as exc:
            errors.append(f"{k}: can't read {util.tilde(path)} ({exc.strerror})")
    for k, target in SECRET_ENV.items():
        if env.get(k, "").strip() and target not in answers:
            answers[target] = env[k].strip()
    url = answers.get("notify-url")
    if isinstance(url, str) and url and not answers.get("NOTIFY_KIND"):
        answers["NOTIFY_KIND"] = settings.kind_from_url(url)
    return errors


def noninteractive(defaults: bool = False, answers_file: str | None = None, yes: bool = False, dry_run: bool = False,
                   update: bool = False, out=None, env=None, inp=None) -> int:
    """--defaults / --answers FILE: every value from the file, the environment (secrets), the saved .env or
    detection, in that order. Exit 2 on a bad answers file or answer, 3 when a prerequisite is missing."""
    t = Term(inp, out)
    env = os.environ if env is None else env
    answers = {}
    if answers_file:
        try:
            with open(os.path.expanduser(answers_file), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            t.say(f"Can't read {answers_file}: {exc.strerror}")
            return USAGE
        answers, errors = parse_answers(text)
        if errors:
            for e in errors:
                t.say(f"  {t.style.mark('fail')} {answers_file}, {e}")
            return USAGE
    errors = _secrets_from(answers, env)
    if errors:
        for e in errors:
            t.say(f"  {t.style.mark('fail')} {e}")
        return USAGE
    if update:
        answers["HABITS_UPDATE"] = "on"
    mode = "edit" if settings.installed() else "install"
    t.say(t.style.bold("Deskmate setup") + (" with your answers" if answers_file else
                                            " with your current settings" if mode == "edit" else " with the detected defaults"))
    res = steps.validate("review", answers)
    s = res["step"]
    for r in (s.get("info") or {}).get("rows") or []:
        t.say(f"  {r['title']:<20} {r['summary']}")
    for c in s.get("checks") or []:
        t.check_line(c)
    errs = res.get("errors") or {}
    if errs:
        for k, msg in errs.items():
            lab = settings.KEYS[k].label if k in settings.KEYS else k
            t.say(f"  {t.style.mark('fail')} {k} ({lab}): {msg}")
        t.say("Nothing changed. Fix these answers (--answers FILE), or run ./deskmate setup to choose.")
        return USAGE
    if s.get("status") == "fail":
        t.say("Nothing changed. Fix the problems above, then run this again.")
        return PREREQ
    if not yes:
        try:
            if not t.yes_no("Apply the settings?" if mode == "edit" else "Install?", True):
                t.say("Nothing changed.")
                return FAILED
        except Cancelled:
            t.say("Nothing changed. Add --yes to run without asking.")
            return FAILED
    lines = []
    try:
        result = steps.apply(answers, _event_printer(t, lines), until="Prepare folders" if dry_run else None)
    except KeyboardInterrupt:
        compose.cancel()
        t.say("Stopped. Run it again to carry on.")
        return CANCELLED
    if not result.get("ok"):
        t.say(f"{t.style.mark('fail')} {result.get('failed_phase')}: {result.get('hint')}")
        path = _save_log(lines, answers)
        if path:
            t.say(f"  The whole log: {util.tilde(str(path))}")
        return FAILED
    if dry_run:
        t.say("Wrote .env and compose.local.yaml (--dry-run: nothing was built or started).")
        return OK
    return _done(t, result, open_browser=False)
