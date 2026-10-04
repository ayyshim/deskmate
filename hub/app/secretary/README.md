# The secretary (hub/app/secretary)

Reads the Claude Code sessions, the docs folder, memory notes and git history the user chose in setup, and
turns them into the Secretary pages (`/api/sec/*`), session digests, a daily brief, a morning notification
and answers to questions. Design: §3.3–§3.5 of the Deskmate design doc. Nothing here writes outside the
hub's data folder; every host folder is mounted read-only at its own path (identity mounts).

## What it reads (all from `config`)

| Setting | Effect |
|---|---|
| `SECRETARY=off` | `start()` returns no tasks, hooks are ignored, nothing is read, no model is called. `/api/sec/state` says `secretary.status = "off"`; Ask, the brief and "Write a digest" answer 503. |
| `CLAUDE_CONFIG_DIRS` | Transcripts: `<dir>/projects/<slug>/<uuid>.jsonl` (main files only; never `subagents/`, never `isSidechain` records). Memory: `<dir>/projects/<slug>/memory/*.md`. |
| `SESSIONS_ROOTS` | A session counts if its **first record's `cwd`** is inside a root (`config.in_sessions_roots`). Others keep only a "skipped" marker, no content. Memory notes are read only for projects that hold an in-scope session. |
| `DOCS_DIR` (optional) | `changelog/<repo>/<YYYY-MM-DD>.md`, `design/README.md` + `design/<folder>/*.md`. Unset or missing: no changelog/design/decision rows; `state.sources.docs` says so and the pages show their empty state. |
| `READ_MEMORY`, `READ_GIT` | Off: those sources are skipped and their old rows removed at the next sweep. |
| `OWNER`, `TZ` | Owner name in texts and model material; the day boundary for everything ("today", digests per day, the brief). |
| Repos | Git repos found under the roots (a `.git` folder or file, up to grandchildren; linked worktrees group under their main repo). A folder next to a repo and named after it (`<repo>-wt/<branch>/…`, `<repo>-hotfix/…`) counts as that repo too, so files of a worktree removed after its merge keep their repo (`gitlog.repo_of`). Key = folder name; label = `config.tilde(path)`. The docs folder is a repo key too. |

## Parts

| File | Does |
|---|---|
| `__init__.py` | `start()` (migrations, background tasks; never raises) and `hook(event, payload)`. |
| `store.py` | Migrations on the hub's SQLite (ALTER TABLE for new columns, new tables, FTS5 `cc_fts`; `PRAGMA user_version = 3`; safe to run twice), the secretary's own per-thread connections (WAL), kv, thread-safe live events (`journal.publish` on the loop). |
| `transcripts.py` | Incremental JSONL parser → `cc_items` (prompt / assistant / steps / compact / interrupt), `cc_steps`, `cc_files`, `cc_commands`, `cc_links` (commit, push, artifact, pr, notify), `cc_session_days`, FTS rows. Every text goes through `redact.scrub()` before SQLite; tool outputs are never stored, only status. |
| `hubdocs.py` | Changelog entries, follow-ups, design index and statuses, decisions and questions, memory notes. |
| `gitlog.py` | Repo discovery, unpushed branches, commits of the last two weeks, docs changes. Read-only (`--no-optional-locks`). |
| `sweep.py` | Every 5 minutes (and `POST /api/sec/sweep`): the above into `hub_entries`, open loops (stable `lp_` ids; the user's done/snooze state survives), hygiene checks. No model. |
| `redact.py` | Secret redactor (stdlib, Python 3.12). `scrub(text)`, `scrub_obj(json)`. |
| `condense.py` | The material for the model layer (shapes below) and the digest pre-filter. |
| `scheduler.py` | When things run (below). |
| `gate.py` | The optional model layer (`llm.py`, built separately): missing or broken means "no Claude login"; plan usage for the meter and the pause (usage.py's rule and wording, so every label agrees); `FAILURES`, what each model failure means to the user. |
| `views.py`, `api.py`, `contract.py` | Every `/api/sec` route, its data, its pydantic response model (`extra="forbid"`). |
| `llm.py`, `usage.py`, `prompts.py`, `digest.py`, `brief.py`, `ask.py` | The model layer (separate owner). |

## When it runs (scheduler.py)

- **Hooks** (`POST /hooks/stop|session-end|pre-compact`, from the deskmate plugin): only a main transcript
  inside `CLAUDE_CONFIG_DIRS` is accepted. Stop settles the session for 120 s, each new Stop pushes that back;
  SessionEnd and PreCompact flush at once (and a later Stop does not undo a waiting flush).
- **Scan** every 15 s (stat only) reads transcripts that grew, so it works before the hooks are connected.
  A session idle for 10 minutes is considered for a digest too.
- **Pre-filter**: a delta with no file edits, no commits/pushes/artifacts/PRs/notifications and under 1,500
  characters of answers is marked `skipped` (no model call).
- **First start / backfill**: every session in the roots is parsed (timeline, list and search need no
  model), but only sessions active in the **last 3 days** are digested on their own, newest first, within
  the daily cap. Older ones show "No digest yet"; `POST /api/sec/sessions/<id>/digest` queues one
  (the "Write a digest" button; it skips the pre-filter).
- **Gate** before each digest and the scheduled brief: secretary on, not paused by the user, a Claude login,
  under `SECRETARY_MAX_DIGESTS_PER_DAY` (model_runs of kind digest today, failures included), plan usage
  under `SECRETARY_PAUSE_AT` (5-hour window) and `SECRETARY_PAUSE_AT_WEEK` (7-day window). Waiting sessions
  show why (`digest_state` paused / capped / no_token) and run when the gate opens. Ask is never gated by
  usage (it shows the warning instead).
- **Sweep** every 5 minutes. **Brief** once a day at `BRIEF_AT` for today only. **Morning post** at
  `MORNING_POST_AT` (within 6 hours of it): yesterday's brief text, or titles and counts if there is no
  brief, through `notify.send(..., event="brief")`; nothing when notifications are off.
- Parsing, the sweep and condensing run in worker threads (`asyncio.to_thread`), one parse at a time; model
  calls are the model layer's coroutines, awaited with a timeout (digest 240 s, brief and Ask 420 s).

## Material passed to the model layer

Both dicts are built from SQLite only (already redacted when stored) and redacted again on the way out.
Times are `HH:MM` in `config.TZ`; days `YYYY-MM-DD`. Paths are shown relative to their root (`util.rel`).

### `llm.digest_session(condensed)` — one session's new part since its last digest

```python
{
  "kind": "session_digest", "version": 1,
  "session": "<uuid>", "owner": "Alex", "tz": "Europe/Berlin",
  "title_hint": "custom title, else Claude Code's ai title, else the first prompt (<=120)" | None,
  "cwd": "~/work" | None,
  "repos": ["shop", ...],                 # repo keys the session touched (most first); a digest may only name these or known repos
  "repo_labels": {"shop": "~/work/shop"},
  "day": "2026-10-04",                    # local day of the delta's last item
  "window": {"from": "09:12", "to": "11:40"},
  "model": "claude-opus-5-5" | None,      # the session's most used model
  "previous": None | {"title", "repos", "outcome", "summary", "shipped", "decisions", "open_loops", "blockers"},
  "delta": {
    "from_offset": 0, "to_offset": 48213, "turns_dropped": 0,   # only the last 30 turns are sent
    "turns": [{"turn": 3, "time": "09:40", "prompt": "<=1500 chars" | "(an image)" | None,
               "mid_turn": ["prompts typed while the turn ran"], "answer": "last answer of the turn, <=2500" | None,
               "interrupted": False, "compacted": False}],
    "files": [{"path": "shop/src/total.py", "action": "edit|write|...", "count": 2}],   # changed files, <=40
    "files_read": 7,
    "commands": [{"command": "first line, <=200", "description": str | None, "status": "ok|error|...", "exit_code": int | None}],
                                          # failed ones plus notable ones (git, deploy, docker, tests, migrations), <=40
    "commands_total": 31, "commands_failed": 2,
    "commits": [{"repo": "shop", "sha": "abc1234", "subject": "fix: ...", "branch": "main"}],
    "pushes": [{"repo": "shop", "branch": "main"}],
    "artifacts": [{"title": "...", "url": "https://claude.ai/..."}],
    "notifications": [{"text": "...", "status": "..."}],
    "prs": [{"title": "...", "url": "https://github.com/..."}]
  },
  "stats": {"from_item": 0, "items": 40, "assistant_chars": 9100, "prompts": 4, "edits": 3, "links": 2}
}
```

Expected back: `{title, repos, outcome, summary, shipped, decisions, open_loops: [{text, owner: "you"|"claude"}],
blockers, links?, usage}`. `scheduler.normalize_digest` checks it: unknown repos are dropped, `outcome` must be
one of shipped / pushed / in_progress / explored / blocked, links are kept only if they appeared in the
material, everything is cut to size and redacted again before it is stored.

### `llm.daily_brief(material)` — one day

```python
{
  "kind": "daily_brief", "version": 1,
  "day": "2026-10-04", "day_label": "Sunday 4 October", "owner": "Alex", "tz": "...", "is_today": True,
  "sessions": [{"title", "time": "09:12–11:40", "repos": [...], "outcome": str | None, "prompts": int,
                "summary": "from the digest" | None,
                # when the session has a digest:
                "shipped": [...], "decisions": [...], "open_loops": [{text, owner}], "blockers": [...]}],   # <=30
  "shipped": [{"time", "text", "repo", "state"}],        # changelog entries and digests of the day
  "in_progress": [{"text", "where"}],
  "decisions": [{"text", "where", "ref": "D4" | None}],
  "needs_you": [{"text", "where", "age": "3 d", "kind"}],
  "followups": [{"text", "where"}],                      # open changelog follow-ups, newest first, <=12
  "commits": {"shop": 3},                                # per repo, that day
  "notes": [{"kind": "followup|decision|note", "text"}], # notes added since the last brief, <=20
  "hygiene": ["one-line warnings", ...],                 # <=8
  "counts": {"sessions", "shipped", "open_loops", "needs_you", "decisions", "commits"}
}
```

Expected back: `{headline, lede, discord_text, usage}`. `discord_text` is the morning post (cut to 1,900
characters by notify.py); it is redacted again before it is stored and before it is sent.

### `llm.ask(question, roots, deny_globs, max_turns=8)`

`roots`: `SESSIONS_ROOTS`, `DOCS_DIR` when it is outside them, and the memory folders of in-scope projects
(never transcripts). `deny_globs`: none (`scheduler.ASK_DENY` is empty). Ask has one deny list, ask.py's own:
env and key files under any name, keystores, credential stores, secrets folders, `*.jsonl`, `.claude.json`
and more, enforced twice (the CLI's permission rules and the PreToolUse guard). A source file named
`credentials.py` may be read (Grep skips it); an env or key file never is.

It never raises for its own failures: it answers `{answer_md, citations, files_read, refused, usage, ok, error,
partial_md}`. Citations are `{path relative to its root, line, root, abs}`; `scheduler.cite_source` turns each
into a Source (changelog with the entry's time / design / memory / file, linked as `vscode://file<abs>:<line>`).
When `ok` is false, the ask is stored as failed with `gate.failure_sentence(error)` as its one sentence, and
`partial_md` (what the model wrote before it failed, if anything) as its answer.

### Failures and usage

`digest_session` and `daily_brief` raise `llm.ModelError` subclasses named for the cause (ModelTimeout,
ModelMaxTurns, ModelRateLimited, ModelAuthFailed, ModelBadOutput, ModelUnavailable, ModelCliMissing,
ModelCliError, ModelFailed), each with `.kind` and `.usage`. A failed digest stores the class name in
`cc_sessions.digest_error` (its label reads "digest failed: <why>"); a failed brief stores "The brief failed:
<why>." Every call, failed or not, is one `model_runs` row with its error code, tokens and the CLI's cost
estimate (note: CLI 2.1.226 prices Claude Sonnet 5.5 at older Sonnet rates, so its estimate runs high).

Plan usage: `usage.window_over` is the one pause rule (a window at or above its limit, or "rejected") and
`usage.pause_reason` the one sentence; `llm.usage_state()` and `gate.usage_snapshot()` both use them. The meter's refresh (`GET /api/sec/usage?refresh=1`, once a minute while a
Secretary tab is visible) probes at most once a minute, and not at all while the user has paused the secretary.

Thinking: digests and the brief switch thinking off where the model allows it (Haiku 4.5); the models that
refuse a disabled thinking config (Claude Sonnet 5.5, Opus 5.5, Fable, Mythos) think adaptively at low
effort instead (`llm.thinking_options`).

## Data

`transcripts.PARSER_VERSION`: raise it when what is stored changes (the parser or the redactor). Every in-scope transcript
is then read again from the start at the next scan, so old rows are replaced; digests keep what they cover.

Tables (besides the hub's own `cc_sessions`, `digests`, `hub_entries`, `briefs`, `asks`, `notes`,
`model_runs`, `loop_state`, `kv`): `cc_items`, `cc_steps`, `cc_files`, `cc_commands`, `cc_links`,
`cc_session_days`, `loops`, and the FTS5 index `cc_fts` (prompts, answers, step labels, commands, file
paths; `unicode61 remove_diacritics 2`, prefix 2 and 3). Search terms are quoted one by one
(`util.fts_query`), so any characters are safe.

## Tests

`hub/tests/secretary/` (pytest), run with the whole hub suite in the hub image:

```sh
docker build -t localhost/deskmate-hub:t-<you> hub
docker run --rm -v "$PWD/hub/tests:/app/tests:ro" localhost/deskmate-hub:t-<you> \
  sh -c 'pip install -q -r /app/requirements-test.txt && python -m pytest -q tests'
```

The fake world (`world.py`) is Alex's home with transcripts, a docs hub, memory notes and git repos, and
planted fake secrets that must never reach SQLite or a model call. `conftest.FakeLLM` stands in for llm.py in
the backend tests; `test_llm.py` tests the model layer with a stubbed SDK; `test_integration.py` runs the
scheduler with the real model layer and only the SDK client and the usage probe's HTTP client stubbed (a
digest, a brief, Ask with citations and refusals, every error kind, a pause on each usage window).
