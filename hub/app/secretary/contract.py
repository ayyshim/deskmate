"""The Secretary API contract as pydantic models (FastAPI response_model=...), plus request bodies.

Every response of /api/sec/... validates against one model here. extra='forbid' makes a stray or
misspelt field fail in tests instead of silently reaching the UI.

Taken from the research contract (contract.json, sec_contract.py) with these additions for the
configurable install (2026-10-04), each marked "added":
- SecretaryStatus.status "off" (SECRETARY=off: nothing is read and no model is called).
- StateResp.sources: what the secretary may read (roots, docs folder and its parts, memory, git), so a
  page that needs the docs folder can show its empty state when there is none.
- Usage.error "off", AsksResp.disabled_reason "off"; LinkKind "notify" (a post through Deskmate's own
  notify tool, any channel).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


Day = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Repo = str  # a git repo under SESSIONS_ROOTS, keyed by its folder name; the docs folder is a repo key too

SourceKind = Literal["changelog", "followup", "design", "decision", "memory", "commit", "branch", "session", "note", "brief", "url", "file"]
StepStatus = Literal["ok", "error", "denied", "interrupted", "running", "background", "unknown"]
Outcome = Literal["shipped", "pushed", "in_progress", "explored", "blocked"]
DigestState = Literal["none", "queued", "running", "ready", "skipped", "paused", "capped", "no_token", "failed"]
LinkKind = Literal["artifact", "pr", "discord", "commit", "push", "url", "notify"]  # notify: added
LoopKind = Literal["followup", "design_status", "memory", "branch", "digest", "note"]


class Source(M):
    kind: SourceKind
    label: str                      # what the .src button shows: "shop/2026-10-04 · 11:45"
    href: str | None                # "#s-<id>[-t<turn>]" | "vscode://file/abs/path:line" | "https://..." | None
    path: str | None = None         # host path
    line: int | None = None
    session: str | None = None
    turn: int | None = None


# ---------------------------------------------------------------- state, usage


class Window(M):
    utilization: float              # 0.0–1.0 (header anthropic-ratelimit-unified-<w>-utilization)
    resets_at: int | None           # epoch seconds
    status: str                     # header ...-status, "" when absent


class Usage(M):
    ok: bool
    ts: float | None                # when the probe last ran
    stale: bool                     # ts older than 5 min
    five_hour: Window | None
    seven_day: Window | None
    pause_at: float                 # config.PAUSE_AT (5-hour window)
    pause_at_week: float            # added: config.PAUSE_AT_WEEK (7-day window)
    over_pause: bool                # five_hour >= pause_at, or seven_day >= pause_at_week, or the model layer says paused
    reason: str | None              # added: why digests and the brief wait (usage.pause_reason), e.g.
    #                                 "The 7-day window is at 86% (the secretary pauses at 85%); it resets on Tue 6 Oct 09:00."
    error: Literal["no_token", "probe_failed", "not_yet", "off"] | None  # off: added


class SecretaryStatus(M):
    status: Literal["on", "paused_user", "paused_usage", "capped", "no_token", "off"]  # off: added
    label: str                      # "On" | "Paused" | "Paused at 61% of 5 h" | "Daily cap reached" | "No Claude login" | "Off"
    paused_by_user: bool
    token: bool
    queued: int                     # sessions waiting for a digest
    running: Literal["digest", "brief", "ask", "sweep"] | None
    last_sweep_ts: float | None
    last_digest_ts: float | None
    last_error: str | None


class Caps(M):
    digests_today: int
    digests_max: int
    briefs_today: int
    briefs_max: int
    pause_at: float


class Models(M):
    digest: str
    digest_label: str
    brief: str
    brief_label: str
    ask: str
    ask_label: str


class Schedule(M):
    brief_at: str                   # "18:30"
    discord_at: str                 # "09:00"


class Counts(M):
    sessions: int
    running_sessions: int
    loops_open: int
    loops_you: int
    hygiene_warn: int


class RepoRef(M):
    key: Repo
    label: str


class DocsSource(M):
    """Added. The optional docs folder (DOCS_DIR) and which parts of a docs hub it has."""
    set: bool                       # DOCS_DIR is configured
    found: bool                     # ... and the hub can read it
    label: str | None               # "~/work/notes"
    kind: Literal["hub", "docs", "none"]  # hub: has changelog/ or design/; docs: Markdown only
    changelog: bool                 # <docs>/changelog/<repo>/<day>.md (Shipped, follow-ups, hygiene)
    design: bool                    # <docs>/design/ (Board, decisions, design status loops)


class Sources(M):
    """Added. What the secretary reads, so pages can explain an empty state."""
    roots: list[str]                # SESSIONS_ROOTS as ~ labels
    config_dirs: list[str]          # CLAUDE_CONFIG_DIRS as ~ labels
    docs: DocsSource
    memory: bool                    # READ_MEMORY
    git: bool                       # READ_GIT
    repos: int                      # git repos found under the roots


class StateResp(M):
    today: Day
    tz: str
    first_sweep_done: bool
    secretary: SecretaryStatus
    caps: Caps
    models: Models
    schedule: Schedule
    usage: Usage
    counts: Counts
    repos: list[RepoRef]
    sources: Sources                # added


# ---------------------------------------------------------------- sessions


class SessionRow(M):
    id: str
    title: str
    title_source: Literal["custom", "digest", "ai", "prompt", "none"]
    day: Day                        # start day
    day_label: str
    first_ts: float
    last_ts: float
    time_label: str                 # "09:55–10:20" | "12:20–now"
    state: Literal["running", "open", "ended"]
    outcome: Outcome | None
    summary: str | None
    summary_source: Literal["digest", "prompt"] | None
    repos: list[Repo]
    cwd: str | None
    cwd_label: str | None           # "~/work/shop"
    model: str | None
    model_label: str | None         # "Opus 5.5"
    prompts: int
    turns: int
    steps: int
    errors: int
    digest_state: DigestState
    digest_label: str               # "digest ready" | "digest queued" | "skipped: too small" | ...
    digest_stale: bool              # the transcript grew after the newest digest


class OpenLoopItem(M):
    text: str
    owner: Literal["you", "claude"]


class Digest(M):
    session: str
    ts: float
    day: Day
    model: str | None
    title: str
    repos: list[Repo]
    outcome: Outcome | None
    summary: str
    shipped: list[str]
    decisions: list[str]
    open_loops: list[OpenLoopItem]
    blockers: list[str]
    to_offset: int                  # transcript bytes this digest covers
    stale: bool


class SessionsResp(M):
    total: int
    matched: int
    sessions: list[SessionRow]
    next_cursor: str | None


class SearchHit(M):
    item: int
    turn: int
    step: int | None
    kind: Literal["prompt", "assistant", "step", "command", "file"]
    ts: float | None
    snippet: str                    # U+0002 / U+0003 around matches


class SearchResult(M):
    session: SessionRow
    hits: list[SearchHit]           # at most 4, conversation order
    more: int


class SearchResp(M):
    q: str
    total_hits: int
    total_sessions: int
    results: list[SearchResult]
    next_cursor: str | None


class FileTouched(M):
    path: str                       # host path
    label: str                      # relative to the root that holds it
    repo: Repo | None
    action: Literal["create", "write", "edit", "read"]
    count: int
    last_item: int | None


class CommandRun(M):
    item: int
    n: int
    turn: int
    command: str                    # first line, <= 300 chars, redacted
    description: str | None
    status: StepStatus
    status_label: str
    exit_code: int | None
    ts: float | None


class Published(M):
    kind: LinkKind
    title: str
    url: str | None
    detail: str | None              # "204", "7f12159 on main", "#42 alex/shop"
    ts: float | None
    item: int | None
    turn: int | None


class SessionSide(M):
    files: list[FileTouched]        # at most 12, most touched first
    files_total: int
    commands: list[CommandRun]      # at most 20: failures first, then newest
    commands_total: int
    commands_failed: int
    published: list[Published]


class SessionMeta(SessionRow):
    transcript: str | None          # host path of the .jsonl
    transcript_exists: bool
    branch: str | None
    resume_command: str             # "cd ~/work && claude --resume <id>"
    compactions: int
    subagents: int
    items: int


class DigestQueued(M):  # added: POST /sessions/{id}/digest, the "Write a digest" button
    ok: bool                        # false: nothing new to digest (digest_state says what it is)
    session: str
    digest_state: DigestState


class SessionDetailResp(M):
    session: SessionMeta
    digest: Digest | None
    side: SessionSide
    sources: list[Source]           # changelog entries and memory notes this session wrote


class Step(M):
    n: int
    tool: str
    label: str
    arg: str
    status: StepStatus
    status_label: str
    exit_code: int | None
    ms: int | None
    ts: float | None
    path: str | None


class _ItemBase(M):
    i: int
    turn: int
    ts: float | None
    time: str | None                # "12:20", hub local time


class PromptItem(_ItemBase):
    kind: Literal["prompt"]
    text: str
    chars: int
    truncated: bool
    mid_turn: bool
    slash: str | None               # "/compact" when the prompt was a slash command
    images: int
    in_digest: bool


class AssistantItem(_ItemBase):
    kind: Literal["assistant"]
    text: str
    chars: int
    truncated: bool
    in_digest: bool
    live: bool
    model: str | None
    error: bool                     # an API error message


class StepsItem(_ItemBase):
    kind: Literal["steps"]
    summary: str                    # "14 steps · read 7 files · ran 4 commands"
    n: int
    ts_end: float | None
    live: bool
    steps: list[Step]


class CompactItem(_ItemBase):
    kind: Literal["compact"]
    text: str                       # "Conversation compacted · auto · 148k → 12k tokens"
    trigger: Literal["auto", "manual"] | None


class InterruptItem(_ItemBase):
    kind: Literal["interrupt"]
    text: str                       # "You interrupted"


Item = Annotated[Union[PromptItem, AssistantItem, StepsItem, CompactItem, InterruptItem], Field(discriminator="kind")]


class ItemsResp(M):
    session: str
    total: int
    turns: int
    cursor: int
    next_cursor: int | None
    live: bool
    items: list[Item]


class FindHit(M):
    item: int
    turn: int
    step: int | None
    kind: Literal["prompt", "assistant", "step", "command", "file"]
    snippet: str


class FindResp(M):
    q: str
    total: int
    hits: list[FindHit]


# ---------------------------------------------------------------- today, timeline


class Brief(M):
    day: Day
    ts: float | None
    status: Literal["running", "ready", "failed"]
    model: str | None
    trigger: Literal["schedule", "manual"] | None
    headline: str | None
    lede: str | None
    error: str | None
    posted_ts: float | None


class Loop(M):
    id: str                         # lp_<16 hex>
    group: Literal["you", "followup"]
    kind: LoopKind
    text: str
    where: str | None               # chip: design folder or repo
    repo: Repo | None
    origin_day: Day | None
    age_days: int | None
    age_label: str                  # "today" | "3 d" | "—"
    state: Literal["open", "done", "snoozed"]
    until: Day | None               # snoozed until this local day
    state_ts: float | None
    gone: bool                      # the source no longer produces it
    src: Source


class ShipItem(M):
    id: str
    time: str | None
    ts: float | None
    text: str
    repo: Repo | None
    state: Literal["deployed", "pushed", "merged", "committed", "built", "shipped"]
    src: Source


class ProgressItem(M):
    id: str
    text: str
    where: str | None
    src: Source


class DecisionRow(M):
    id: str                         # dc_<16 hex>
    day: Day | None
    text: str
    ref: str | None                 # "D4", "Q3"
    outcome: str | None
    where: str | None
    repos: list[Repo]
    kind: Literal["design", "digest", "note"]
    is_question: bool
    src: Source


class RepoCount(M):
    repo: Repo
    label: str
    count: int


class TodayStats(M):
    sessions: int
    shipped: int
    open_loops: int
    decisions: int


class TodayResp(M):
    day: Day
    day_label: str
    is_today: bool
    prev_day: Day | None
    next_day: Day | None
    updated_ts: float | None
    brief: Brief | None
    lede: str
    lede_source: Literal["brief", "digests", "empty"]
    stats: TodayStats
    needs_you: list[Loop]           # at most 8
    needs_you_total: int
    shipped: list[ShipItem]
    in_progress: list[ProgressItem]
    decided: list[DecisionRow]
    by_repo: list[RepoCount]


class TimelineSession(SessionRow):
    digest: Digest | None
    sources: list[Source]


class TimelineDay(M):
    day: Day
    day_label: str
    headline: str
    headline_source: Literal["brief", "computed"]
    sessions: list[TimelineSession]


class TimelineResp(M):
    repo: str                       # "all" or a repo key
    repos: list[RepoRef]
    days: list[TimelineDay]
    next_before: Day | None


# ---------------------------------------------------------------- board, loops, decisions


class Card(M):
    folder: str
    title: str
    status: str                     # full status text, markdown stripped
    status_short: str               # "Built 1 Oct, not pushed"
    status_day: Day | None
    lane: Literal["building", "built", "shipped"]
    projects: list[str]
    last_activity_day: Day | None
    last_activity_label: str
    open_loops: int
    prototype_url: str | None
    doc: Source


class Lane(M):
    key: Literal["building", "built", "shipped"]
    title: str
    hint: str
    cards: list[Card]


class BoardResp(M):
    lanes: list[Lane]
    unindexed: list[str]            # folders in claude/design with no index row


class LoopCounts(M):
    open: int
    you: int
    followup: int
    snoozed: int
    done: int


class LoopsResp(M):
    state: Literal["open", "snoozed", "done", "all"]
    counts: LoopCounts
    you: list[Loop]
    followup: list[Loop]


class LoopActionResp(M):
    ok: bool
    loop: Loop
    counts: LoopCounts


class SnoozeReq(M):
    days: int | None = Field(default=None, ge=1, le=90)
    until: Day | None = None        # either; neither means 3 days


class DecisionsResp(M):
    total: int
    rows: list[DecisionRow]
    next_cursor: str | None


# ---------------------------------------------------------------- ask, notes, brief, pause, hygiene


class Ask(M):
    id: int
    ts: float
    origin: Literal["ui", "mcp"]
    origin_label: str               # "you" | "shop · board after deploy"
    question: str
    status: Literal["running", "done", "failed"]
    answer_md: str | None
    citations: list[Source]
    ms: int | None
    model: str | None
    error: str | None
    warning: str | None


class AsksResp(M):
    asks: list[Ask]                 # newest first
    next_cursor: str | None
    suggestions: list[str]
    enabled: bool
    disabled_reason: Literal["no_token", "off"] | None  # off: added
    warning: str | None


class AskReq(M):
    question: str = Field(min_length=1, max_length=2000)


class AskCreated(M):
    ask: Ask


class NoteReq(M):
    kind: Literal["followup", "decision", "note"]
    text: str = Field(min_length=1, max_length=1000)
    repo: Repo | None = None
    where: str | None = None
    session: str | None = None


class Note(M):
    id: int
    ts: float
    origin: Literal["ui", "mcp"]
    kind: Literal["followup", "decision", "note"]
    text: str
    repo: Repo | None
    where: str | None
    session: str | None
    consumed: bool


class NoteCreated(M):
    note: Note
    loop: Loop | None
    decision: DecisionRow | None


class NotesResp(M):
    notes: list[Note]
    next_cursor: str | None


class BriefRunReq(M):
    day: Day | None = None


class BriefResp(M):
    brief: Brief


class PauseReq(M):
    paused: bool


class PauseResp(M):
    ok: bool
    secretary: SecretaryStatus


class Check(M):
    id: str                         # hy_<16 hex>
    rule: Literal["commits_without_changelog", "design_status_stale", "memory_not_pushed", "index_missing",
                  "index_status_mismatch", "changelog_folder_missing", "memory_index_missing"]
    level: Literal["warn", "ok", "info"]
    text: str
    repo: Repo | None
    where: str | None
    src: Source
    hand_off: str | None            # text to paste into a Claude session (warn only)


class HygieneCounts(M):
    warn: int
    ok: int
    info: int


class HygieneResp(M):
    ts: float | None
    counts: HygieneCounts
    checks: list[Check]             # warn, then info, then ok


class SweepResp(M):
    ok: bool
    started: bool                   # false when a sweep was already running


class ErrorResp(M):
    detail: str
