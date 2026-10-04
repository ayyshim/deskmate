"""When the secretary does what (design §3.3, §3.4): hooks, settle, ingest, digests, sweep, brief, morning post.

Everything heavy runs off the event loop: parsing, the sweep and the condensing run in worker threads
(asyncio.to_thread), one parse at a time. The model calls are the model layer's coroutines (llm.py) and
are awaited here with a timeout. Live events go through store.publish, which is safe from any thread.

Settle: a Stop hook schedules the session for 120 s later, and every new Stop pushes that back; SessionEnd
and PreCompact flush at once. Without hooks (not connected yet), a scan every 15 s still reads what changed,
and a session idle for 10 minutes is considered for a digest as well, within the last three days only, newest
first, so a first start does not spend the day's cap on old sessions (older ones get the "Write a digest"
button, POST /api/sec/sessions/<id>/digest).

Gate, checked before every digest: secretary on, not paused by the user, a Claude login, under the daily
cap (model_runs rows of kind digest today, failures included), and plan usage under SECRETARY_PAUSE_AT (the
5-hour window) and SECRETARY_PAUSE_AT_WEEK (the 7-day window); llm.usage_state() decides, gate.py reads it.
Queued sessions show why they wait (digest_state paused / capped / no_token) and run when the gate opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time

from .. import config
from . import condense, gate, gitlog, store, sweep, transcripts, util, views
from .redact import scrub, scrub_obj

log = logging.getLogger(__name__)

SETTLE_S = 120.0
SCAN_S = 15.0
IDLE_DIGEST_S = 600.0
BACKFILL_DAYS = 3  # a first start digests only sessions active in the last 3 days (§13); older: the button
SWEEP_S = 300.0
DIGEST_TIMEOUT = 240.0
BRIEF_TIMEOUT = 420.0
ASK_TIMEOUT = 420.0
MAX_ASKS = 2
MORNING_WINDOW_H = 6
WAITING = ("queued", "paused", "capped", "no_token")
OUTCOMES = ("shipped", "pushed", "in_progress", "explored", "blocked")
# Ask's deny list is ask.py's (DENY_DIRS, DENY_FILES and the broad credential rules): one list, the strictest.
# Nothing is added here; a source file named credentials.py stays readable, an env or key file never is.
ASK_DENY: list[str] = []
HOOK_EVENTS = {"stop": "stop", "session-end": "session-end", "sessionend": "session-end",
               "pre-compact": "pre-compact", "precompact": "pre-compact"}


class Busy(RuntimeError):
    pass


class NoLogin(RuntimeError):
    pass


class Off(RuntimeError):
    pass


def _hm(value: str, default: str) -> str:
    return value if re.fullmatch(r"[0-2]\d:[0-5]\d", value or "") else default


class Scheduler:
    def __init__(self) -> None:
        self.wake: asyncio.Event | None = None
        self.due: dict[str, dict] = {}
        self.ingest_lock: asyncio.Lock | None = None
        self.running: str | None = None
        self.digesting: str | None = None
        self.sweeping = False
        self.briefing: set[str] = set()
        self.asks = 0
        self.last_scan = 0.0
        self.tasks: list[asyncio.Task] = []
        self.started = False
        self.stats: dict = {}

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> list[asyncio.Task]:
        loop = asyncio.get_running_loop()
        store.set_loop(loop)
        store.ensure()
        self.wake = asyncio.Event()
        self.ingest_lock = asyncio.Lock()
        if not config.SECRETARY:
            log.info("secretary is off: nothing is read and no model is called")
            return []
        self.started = True
        self.tasks = [asyncio.create_task(self._main(), name="secretary-main"),
                      asyncio.create_task(self._clock(), name="secretary-clock")]
        return list(self.tasks)

    def _poke(self) -> None:
        if self.wake is not None:
            self.wake.set()

    # ----------------------------------------------------------------- hooks

    def hook(self, event: str, payload: dict) -> None:
        """Claude Code's Stop / SessionEnd / PreCompact. Runs on the loop inside the request: no I/O here."""
        if not config.SECRETARY or not self.started or not isinstance(payload, dict):
            return
        ev = HOOK_EVENTS.get(str(event or "").lower().replace("_", "-"))
        if ev is None:
            return
        found = transcripts.transcript_from_hook(payload.get("transcript_path"))
        if found is None:
            return
        d, slug, path = found
        sid = os.path.basename(path)[:-6]
        now = time.time()
        when = now + SETTLE_S if ev == "stop" else now
        prev = self.due.get(sid)
        if prev and prev["event"] in ("session-end", "pre-compact") and prev["at"] <= now:
            when = prev["at"]  # a flush already waiting stays a flush
        self.due[sid] = {"at": when, "event": ev if not (prev and prev["event"] == "session-end") else "session-end",
                         "path": path, "dir": d, "slug": slug, "hook_ts": now}
        self._poke()

    # ----------------------------------------------------------------- main loop

    async def _main(self) -> None:
        try:
            gone = await asyncio.to_thread(transcripts.forget_outside_roots)
            if gone:
                log.info("secretary: forgot %d sessions outside the configured folders", gone)
        except Exception:
            log.exception("secretary: roots check failed")
        await self._scan(first=True)
        await self.run_sweep()
        next_sweep = time.monotonic() + SWEEP_S
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self._next_wait())
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                if time.monotonic() - self.last_scan >= SCAN_S:
                    await self._scan()
                now = time.time()
                for sid in [s for s, d in self.due.items() if d["at"] <= now]:
                    await self._settle(sid)
                await self._digest_next()
                if time.monotonic() >= next_sweep:
                    next_sweep = time.monotonic() + SWEEP_S
                    asyncio.create_task(self.run_sweep())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("secretary loop")
                await asyncio.to_thread(store.kv_put, "sec.last_error", f"{exc.__class__.__name__}")

    def _next_wait(self) -> float:
        waits = [SCAN_S]
        if self.due:
            waits.append(max(0.5, min(d["at"] for d in self.due.values()) - time.time()))
        return min(waits)

    async def _ingest(self, path: str, d: str | None, slug: str | None) -> dict | None:
        async with self.ingest_lock:
            try:
                res = await asyncio.to_thread(transcripts.ingest, path, d, slug)
            except Exception:
                log.exception("secretary: ingest failed")
                return None
        if res and res.get("new_items"):
            row = await asyncio.to_thread(store.one, "select * from cc_sessions where id = ?", (res["id"],))
            if row is not None and (row["prompts"] or 0) > 0:
                store.publish_session(res["id"], views.session_state(row), row["digest_state"] or "none")
        return res

    async def _scan(self, first: bool = False) -> None:
        self.last_scan = time.monotonic()
        t0 = time.monotonic()
        changed = await asyncio.to_thread(transcripts.changed_transcripts)
        new = 0
        for d, slug, path in changed:
            res = await self._ingest(path, d, slug)
            if res and res.get("new_items"):
                new += 1
        if first:
            self.stats["first_scan"] = {"transcripts": len(changed), "ingested": new, "ms": util.monotonic_ms(t0)}
        if new:
            store.publish("sec_state", None)
            store.publish("sec_today", {"day": util.today()})
        await asyncio.to_thread(self._idle_candidates)

    def _idle_candidates(self) -> None:
        """Sessions without hooks: consider a digest once they have been idle for a while (thread)."""
        now = time.time()
        rows = store.q(f"""select id from cc_sessions where {views.VISIBLE} and read_offset > coalesce(digested_offset, 0)
                           and coalesce(digest_checked, -1) != read_offset and digest_state not in
                           ('queued', 'running', 'paused', 'capped', 'no_token') and mtime < ? and last_ts > ?""",
                       (now - IDLE_DIGEST_S, now - BACKFILL_DAYS * 86400))
        for r in rows:
            if r["id"] not in self.due:
                self.consider_digest(r["id"])

    async def _settle(self, sid: str) -> None:
        d = self.due.pop(sid, None)
        if d is None:
            return
        await self._ingest(d["path"], d["dir"], d["slug"])

        def mark() -> None:
            ended = 1 if d["event"] == "session-end" else 0
            store.run("update cc_sessions set last_hook = ?, last_hook_ts = ?, ended = max(coalesce(ended, 0), ?) "
                      "where id = ?", (d["event"], d["hook_ts"], ended, sid))
            self.consider_digest(sid)

        await asyncio.to_thread(mark)
        row = await asyncio.to_thread(store.one, "select * from cc_sessions where id = ?", (sid,))
        if row is not None and (row["prompts"] or 0) > 0:
            store.publish_session(sid, views.session_state(row), row["digest_state"] or "none")

    # ----------------------------------------------------------------- digests

    def consider_digest(self, sid: str) -> str | None:
        """Pre-filter one session's new delta and queue it, or mark it skipped (thread). Returns the new state."""
        row = store.one(f"select * from cc_sessions where id = ? and {views.VISIBLE}", (sid,))
        if row is None:
            return None
        ro, do = int(row["read_offset"] or 0), int(row["digested_offset"] or 0)
        if ro <= do:
            return row["digest_state"]
        if row["digest_state"] in WAITING + ("running",):
            store.run("update cc_sessions set digest_checked = ? where id = ?", (ro, sid))
            return row["digest_state"]
        stats = condense.delta_stats(sid)
        if condense.trivial(stats):
            if do > 0:
                # A digest exists and only a little came after it: the session keeps its digest (and its state);
                # the page offers "Update the digest" for the rest. Checked again once the session grows.
                store.run("update cc_sessions set digest_checked = ? where id = ?", (ro, sid))
                return row["digest_state"]
            store.run("update cc_sessions set digest_state = 'skipped', digest_error = 'too small', digest_checked = ? "
                      "where id = ?", (ro, sid))
            return "skipped"
        store.run("update cc_sessions set digest_state = 'queued', digest_error = null, pending_since = ?, "
                  "digest_checked = ? where id = ?", (time.time(), ro, sid))
        self._poke_threadsafe()
        return "queued"

    def queue_digest(self, sid: str) -> dict | None:
        """The "Write a digest" button (thread): queue one session now, whatever its age or size. None: unknown id."""
        row = store.one(f"select * from cc_sessions where id = ? and {views.VISIBLE}", (sid,))
        if row is None:
            return None
        state = row["digest_state"] or "none"
        if int(row["read_offset"] or 0) <= int(row["digested_offset"] or 0) or state in WAITING + ("running",):
            return {"ok": state in WAITING + ("running",), "session": sid, "digest_state": state}
        store.run("update cc_sessions set digest_state = 'queued', digest_error = null, pending_since = ?, "
                  "digest_checked = read_offset where id = ?", (time.time(), sid))
        self._poke_threadsafe()
        return {"ok": True, "session": sid, "digest_state": "queued"}

    def _poke_threadsafe(self) -> None:
        loop = store._loop
        if loop is not None and self.wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.wake.set)

    async def gate_state(self) -> str:
        if not config.SECRETARY:
            return "off"
        if await asyncio.to_thread(store.kv_get, "sec.paused") == "1":
            return "paused"
        if not gate.available():
            return "no_token"
        if await asyncio.to_thread(views.digests_today) >= config.MAX_DIGESTS:
            return "capped"
        await gate.probe()
        if gate.usage_snapshot()["over_pause"]:
            return "paused"
        return "ok"

    async def _digest_next(self) -> None:
        if self.digesting:
            return
        rows = await asyncio.to_thread(store.q, f"""select id, digest_state from cc_sessions where {views.VISIBLE}
                                       and digest_state in ('queued', 'paused', 'capped', 'no_token')
                                       order by last_ts desc""")
        if not rows:
            return
        g = await self.gate_state()
        if g != "ok":
            waiting = "paused" if g in ("paused", "off") else g
            changed = [r["id"] for r in rows if r["digest_state"] != waiting]
            if changed:
                await asyncio.to_thread(self._set_states, changed, waiting)
                store.publish("sec_state", None)
            return
        await self.run_digest(rows[0]["id"])
        self._poke()

    @staticmethod
    def _set_states(ids: list[str], state: str) -> None:
        with store.tx() as c:
            c.executemany("update cc_sessions set digest_state = ? where id = ?", [(state, i) for i in ids])

    async def run_digest(self, sid: str) -> bool:
        m = gate.llm()
        if m is None or not gate.available():
            return False
        self.digesting, self.running = sid, "digest"
        t0 = time.monotonic()
        ok, note, code, use = False, None, None, None
        material = None
        try:
            material = await asyncio.to_thread(condense.session_delta, sid)
            await asyncio.to_thread(store.run, "update cc_sessions set digest_state = 'running' where id = ?", (sid,))
            store.publish_session(sid, "open", "running")
            store.publish("sec_state", None)
            res = await asyncio.wait_for(m.digest_session(material), timeout=DIGEST_TIMEOUT)
            use = res.get("usage") if isinstance(res, dict) else None
            clean = normalize_digest(res, material)
            await asyncio.to_thread(save_digest, sid, material, clean)
            ok = True
        except Exception as exc:
            # The model layer raises a ModelError subclass named for the cause (ModelRateLimited, ModelTimeout,
            # …) that carries what the call cost; only the class name is stored, never the message.
            note, code = exc.__class__.__name__, gate.failure_code(exc)
            use = use or getattr(exc, "usage", None)
            log.warning("secretary: digest failed for %s: %s", sid, note)
            await asyncio.to_thread(store.run, "update cc_sessions set digest_state = 'failed', digest_error = ? "
                                    "where id = ?", (note[:200], sid))
        finally:
            ms = util.monotonic_ms(t0)
            await asyncio.to_thread(_model_run, "digest", config.DIGEST_MODEL, ok, ms, note, use, code)
            self.digesting, self.running = None, None
        row = await asyncio.to_thread(store.one, "select * from cc_sessions where id = ?", (sid,))
        if row is not None:
            store.publish_session(sid, views.session_state(row), row["digest_state"] or "none")
        store.publish("sec_today", {"day": (material or {}).get("day") or util.today()})
        store.publish("sec_state", None)
        return ok

    # ----------------------------------------------------------------- sweep

    async def run_sweep(self) -> bool:
        if self.sweeping:
            return False
        self.sweeping = True
        prev = self.running
        self.running = self.running or "sweep"
        try:
            res = await asyncio.to_thread(sweep.run_sweep)
            self.stats["last_sweep"] = res
            store.publish("sec_loops", {"counts": await asyncio.to_thread(views.loop_counts)})
            store.publish("sec_today", {"day": util.today()})
            store.publish("sec_state", None)
        except Exception as exc:
            log.exception("secretary: sweep failed")
            await asyncio.to_thread(store.kv_put, "sec.last_error", f"sweep: {exc.__class__.__name__}")
        finally:
            self.sweeping = False
            if self.running == "sweep":
                self.running = prev if prev != "sweep" else None
        return True

    def request_sweep(self) -> bool:
        if self.sweeping or not config.SECRETARY:
            return False
        asyncio.get_running_loop().create_task(self.run_sweep())
        return True

    # ----------------------------------------------------------------- brief and morning post

    async def _clock(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("secretary clock")

    async def _tick(self) -> None:
        now = util.now_local()
        today = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")
        if hm >= _hm(config.BRIEF_AT, "18:30") and today not in self.briefing:
            done = await asyncio.to_thread(store.kv_get, f"sec.brief_sched.{today}")
            if not done and await self.gate_state() == "ok":
                await asyncio.to_thread(store.kv_put, f"sec.brief_sched.{today}", time.time())
                await self.run_brief(today, "schedule")
        post_at = _hm(config.MORNING_POST_AT, "09:00")
        if hm >= post_at:
            h, m = (int(x) for x in post_at.split(":"))
            late = (now.hour * 60 + now.minute) - (h * 60 + m) > MORNING_WINDOW_H * 60
            done = await asyncio.to_thread(store.kv_get, f"sec.morning.{today}")
            if not done and not late:
                await asyncio.to_thread(store.kv_put, f"sec.morning.{today}", time.time())
                await self.morning_post(util.add_days(today, -1))

    async def run_brief(self, day: str, trigger: str) -> None:
        if day in self.briefing:
            raise Busy("A brief for that day is being written.")
        m = gate.llm()
        if not config.SECRETARY:
            raise Off("The secretary is off.")
        if m is None or not gate.available():
            raise NoLogin("The brief needs the secretary's Claude login.")
        self.briefing.add(day)
        await asyncio.to_thread(_brief_row, day, trigger)
        store.publish("sec_brief", {"day": day, "status": "running"})
        asyncio.get_running_loop().create_task(self._brief_task(m, day, trigger))

    async def _brief_task(self, m, day: str, trigger: str) -> None:
        prev = self.running
        self.running = "brief"
        t0 = time.monotonic()
        ok, note, code, use, status = False, None, None, None, "failed"
        try:
            material = await asyncio.to_thread(condense.brief_material, day)
            res = await asyncio.wait_for(m.daily_brief(material), timeout=BRIEF_TIMEOUT)
            res = res if isinstance(res, dict) else {}
            use = res.get("usage")
            headline = scrub(str(res.get("headline") or ""))[:300] or None
            lede = scrub(str(res.get("lede") or ""))[:4000] or None
            if not headline and not lede:
                raise ValueError("empty brief")
            await asyncio.to_thread(_brief_done, day, headline, lede,
                                    scrub(str(res.get("discord_text") or ""))[:1900] or None)
            ok, status = True, "ready"
        except Exception as exc:  # a ModelError subclass from the model layer, with .usage; or a timeout here
            note, code = exc.__class__.__name__, gate.failure_code(exc)
            use = use or getattr(exc, "usage", None)
            log.warning("secretary: the brief for %s failed: %s", day, note)
            await asyncio.to_thread(store.run, "update briefs set status = 'failed', error = ? where day = ?",
                                    (f"The brief failed: {gate.failure_phrase(code)}.", day))
        finally:
            await asyncio.to_thread(_model_run, "brief", config.BRIEF_MODEL, ok, util.monotonic_ms(t0), note, use, code)
            self.briefing.discard(day)
            self.running = prev if prev != "brief" else None
        store.publish("sec_brief", {"day": day, "status": status})
        store.publish("sec_today", {"day": day})
        store.publish("sec_state", None)

    async def morning_post(self, day: str) -> bool:
        from .. import notify

        if notify.kind() == "none":
            return False
        b = await asyncio.to_thread(views.get_brief, day)
        text = b["discord_text"] if b is not None and b["status"] == "ready" and b["discord_text"] else None
        if text is None:
            t = await asyncio.to_thread(views.today_page, day)
            if not t["stats"]["sessions"] and not t["shipped"] and not t["needs_you_total"]:
                return False
            text = await asyncio.to_thread(condense.morning_text, day)
        ok = await post(scrub(text), day)
        if ok and b is not None:
            await asyncio.to_thread(store.run, "update briefs set posted = 1, posted_ts = ? where day = ?",
                                    (time.time(), day))
        return ok

    # ----------------------------------------------------------------- ask

    async def run_ask(self, question: str, origin: str = "ui") -> int:
        if not config.SECRETARY:
            raise Off("The secretary is off.")
        m = gate.llm()
        if m is None or not gate.available():
            raise NoLogin("Ask needs the secretary's Claude login.")
        if self.asks >= MAX_ASKS:
            raise Busy("Two questions are already being answered. Ask again in a moment.")
        warning = await asyncio.to_thread(views.ask_warning)
        aid = await asyncio.to_thread(store.run, "insert into asks(ts, origin, question, status, model, warning, citations) "
                                      "values(?, ?, ?, 'running', ?, ?, '[]')",
                                      (time.time(), origin, scrub(question), config.ASK_MODEL, warning))
        self.asks += 1
        asyncio.get_running_loop().create_task(self._ask_task(m, aid, question))
        return aid

    async def _ask_task(self, m, aid: int, question: str) -> None:
        """llm.ask never raises for its own failures: it answers {ok, error, answer_md, partial_md, citations,
        refused, usage}, error being a code. A failed ask shows that code's sentence (gate.FAILURES), and what
        the model had written before it failed, if anything."""
        prev = self.running
        self.running = self.running or "ask"
        t0 = time.monotonic()
        ok, note, code, use, status = False, None, None, None, "failed"
        try:
            roots = await asyncio.to_thread(ask_roots)
            res = await asyncio.wait_for(m.ask(scrub(question), roots, list(ASK_DENY), max_turns=8), timeout=ASK_TIMEOUT)
            if not isinstance(res, dict):
                raise ValueError("no answer object")
            use = res.get("usage")
            cites: list[dict] = []
            for c in res.get("citations") or []:
                src = await asyncio.to_thread(cite_source, c)
                if src is not None and src not in cites:
                    cites.append(src)
            if res.get("ok", True) is False:
                code = str(res.get("error") or "failed")
                code = code if code in gate.FAILURES else "failed"
                note = f"ask:{code}"
                partial = scrub(str(res.get("partial_md") or "")).strip() or None
                await asyncio.to_thread(store.run, "update asks set status = 'failed', error = ?, answer = ?, "
                                        "citations = ?, ms = ? where id = ?",
                                        (gate.failure_sentence(code), partial[:20000] if partial else None,
                                         json.dumps(cites[:30]), util.monotonic_ms(t0), aid))
            else:
                answer = scrub(str(res.get("answer_md") or "")).strip() or "No answer."
                await asyncio.to_thread(store.run, "update asks set status = 'done', answer = ?, citations = ?, ms = ? "
                                        "where id = ?", (answer[:20000], json.dumps(cites[:30]), util.monotonic_ms(t0), aid))
                ok, status = True, "done"
        except Exception as exc:
            note, code = exc.__class__.__name__, gate.failure_code(exc)
            await asyncio.to_thread(store.run, "update asks set status = 'failed', error = ?, ms = ? where id = ?",
                                    (gate.failure_sentence(code), util.monotonic_ms(t0), aid))
        finally:
            self.asks = max(0, self.asks - 1)
            await asyncio.to_thread(_model_run, "ask", config.ASK_MODEL, ok, util.monotonic_ms(t0), note, use, code)
            if self.running == "ask":
                self.running = prev if prev != "ask" else None
        store.publish("sec_ask", {"id": aid, "status": status})


SCHED = Scheduler()


# ---------------------------------------------------------------- helpers (worker threads)


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _model_run(kind: str, model: str, ok: bool, ms: int, note: str | None, usage: dict | None = None,
               error: str | None = None) -> None:
    """One row per model call, failures included (the daily cap counts them). usage is llm.py's dict:
    tokens and the CLI's cost estimate, also for a call that failed half way."""
    u = usage if isinstance(usage, dict) else {}
    try:
        cost = float(u["cost_usd"]) if u.get("cost_usd") is not None else None
    except (TypeError, ValueError):
        cost = None
    store.run("insert into model_runs(ts, day, kind, model, ok, ms, note, error, input_tokens, output_tokens, "
              "cache_read_tokens, cache_write_tokens, cost_usd) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (time.time(), util.today(), kind, str(u.get("model") or model), 1 if ok else 0, ms, note,
               None if ok else (error or "failed"), _int(u.get("input_tokens")), _int(u.get("output_tokens")),
               _int(u.get("cache_read_tokens")), _int(u.get("cache_write_tokens")), cost))


def _strs(v, limit: int = 12, n: int = 400) -> list[str]:
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, (str, int, float)) and str(x).strip():
            s = str(x).strip()
            out.append(s if len(s) <= n else s[: n - 1] + "…")
    return out[:limit]


def normalize_digest(res, material: dict) -> dict:
    """The model's digest, checked: right types, known outcome and repos, links only if they were in the input."""
    if not isinstance(res, dict):
        raise ValueError("digest is not an object")
    known = set(material.get("repos") or []) | {r["key"] for r in gitlog.repos()} | (
        {gitlog.docs_key()} if gitlog.docs_key() else set())
    loops = []
    for x in res.get("open_loops") or []:
        if isinstance(x, dict) and str(x.get("text") or "").strip():
            loops.append({"text": str(x["text"]).strip()[:400], "owner": x.get("owner") if x.get("owner") in ("you", "claude") else "you"})
        elif isinstance(x, str) and x.strip():
            loops.append({"text": x.strip()[:400], "owner": "you"})
    blob = json.dumps(material)
    links = [u for u in _strs(res.get("links"), 20, 500) if u.startswith("https://") and u in blob]
    title = str(res.get("title") or "").strip()[:200]
    summary = str(res.get("summary") or "").strip()[:2000]
    if not title and not summary:
        raise ValueError("empty digest")
    return scrub_obj({"title": title or (material.get("title_hint") or "Session")[:200],
                      "repos": [r for r in _strs(res.get("repos"), 8, 80) if r in known],
                      "outcome": res.get("outcome") if res.get("outcome") in OUTCOMES else None,
                      "summary": summary, "shipped": _strs(res.get("shipped")), "decisions": _strs(res.get("decisions")),
                      "open_loops": loops[:12], "blockers": _strs(res.get("blockers")), "links": links})


def save_digest(sid: str, material: dict, d: dict) -> None:
    delta = material.get("delta") or {}
    now = time.time()
    with store.tx() as c:
        c.execute("""insert into digests(session, ts, day, model, title, repos, summary, shipped, decisions, open_loops,
                     blockers, links, from_offset, to_offset, outcome) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (sid, now, material.get("day") or util.today(), config.DIGEST_MODEL, d["title"], json.dumps(d["repos"]),
                   d["summary"], json.dumps(d["shipped"]), json.dumps(d["decisions"]), json.dumps(d["open_loops"]),
                   json.dumps(d["blockers"]), json.dumps(d["links"]), delta.get("from_offset"), delta.get("to_offset"),
                   d["outcome"]))
        c.execute("""update cc_sessions set digested_offset = max(coalesce(digested_offset, 0), ?), digest_state = 'ready',
                     digest_error = null, pending_since = null, title = ? where id = ?""",
                  (int(delta.get("to_offset") or 0), d["title"], sid))
    transcripts.refresh_repos(sid)
    row = store.one("select read_offset, digested_offset from cc_sessions where id = ?", (sid,))
    if row is not None and (row["read_offset"] or 0) > (row["digested_offset"] or 0):
        store.run("update cc_sessions set digest_checked = null where id = ?", (sid,))


def _brief_row(day: str, trigger: str) -> None:
    store.run("""insert into briefs(day, ts, model, body, posted, status, trigger) values(?, ?, ?, null, 0, 'running', ?)
                 on conflict(day) do update set ts = excluded.ts, model = excluded.model, status = 'running',
                 trigger = excluded.trigger, error = null""", (day, time.time(), config.BRIEF_MODEL, trigger))


def _brief_done(day: str, headline: str | None, lede: str | None, discord_text: str | None) -> None:
    with store.tx() as c:
        c.execute("update briefs set status = 'ready', ts = ?, headline = ?, body = ?, discord_text = ?, error = null "
                  "where day = ?", (time.time(), headline, lede, discord_text, day))
        c.execute("update notes set consumed = 1 where consumed = 0 and ts <= ?", (time.time(),))


def ask_roots() -> list[str]:
    """What Ask may read: the roots, the docs folder, and the memory notes the secretary reads. Never transcripts."""
    from . import hubdocs

    roots = list(config.SESSIONS_ROOTS)
    d = config.DOCS_DIR
    if d and os.path.isdir(d) and not any(d == r or d.startswith(r.rstrip("/") + "/") for r in roots):
        roots.append(d)
    roots += hubdocs.memory_dirs(sweep.in_scope_projects())
    return roots


def cite_source(c) -> dict | None:
    """One Ask citation as a Source. llm.ask gives {path relative to its root, line, root, abs} (contract §10);
    a bare absolute path is taken too. Only a file inside what the hub may read becomes a link."""
    if not isinstance(c, dict):
        return None
    try:
        line = int(c["line"]) if c.get("line") is not None else None
    except (TypeError, ValueError):
        line = None
    line = line if line and line > 0 else None
    raw, root, absolute = c.get("path"), c.get("root"), c.get("abs")
    if isinstance(absolute, str) and os.path.isabs(absolute):
        p = absolute
    elif isinstance(raw, str) and os.path.isabs(raw):
        p = raw
    elif isinstance(raw, str) and raw.strip() and isinstance(root, str) and os.path.isabs(root):
        p = os.path.join(root, raw)
    elif isinstance(raw, str) and raw.strip():  # relative with no root: the first ask root that has it
        p = next((os.path.join(r, raw) for r in ask_roots() if os.path.isfile(os.path.join(r, raw))), None)
    else:
        p = None
    if not p:
        return None
    p = os.path.normpath(p)
    if not config.is_readable(p):
        return None
    if config.DOCS_DIR and p.startswith(os.path.join(config.DOCS_DIR, "changelog") + "/"):
        e = None
        if line:
            e = store.one("select * from hub_entries where kind = 'changelog' and path = ? and line <= ? "
                          "order by line desc limit 1", (p, line))
        if e is not None:
            return dict(views.changelog_source(e), line=line, href=f"vscode://file{p}:{line}")
        return util.source("changelog", util.rel(p), path=p, line=line)
    if config.DOCS_DIR and p.startswith(os.path.join(config.DOCS_DIR, "design") + "/"):
        return util.source("design", util.rel(p), path=p, line=line)
    label = util.rel(p)
    return util.source("memory" if label.startswith("memory/") else "file", label, path=p, line=line)


async def post(text: str, day: str) -> bool:
    """Send the morning message through notify.py (whatever channel the user chose); it never raises."""
    from .. import notify

    try:
        r = await notify.send(text, title=f"Deskmate · {util.day_label(day)}", event="brief")
    except Exception as exc:  # notify.send promises not to raise; a bug there must not stop the clock
        log.warning("morning post failed: %s", exc.__class__.__name__)
        return False
    if not r.get("ok"):
        log.info("morning post not sent: %s", r.get("detail"))
    return bool(r.get("ok"))


def current() -> str | None:
    return SCHED.running


def stop() -> None:
    for t in SCHED.tasks:
        with contextlib.suppress(Exception):
            t.cancel()
