# The working-habits pack

What `./deskmate setup` (the Working habits step) and `./deskmate habits apply` install, when the user turns
habits on. The code that installs it is `setup/deskmate_setup/habits.py`; the Claude Code side is the
`habits` plugin in `plugin/habits/` (skills `habits:changelog-entry`, `habits:design-doc`, `habits:wrap-up`
and the end-of-turn changelog check, `bin/habits-stop`).

| File | What it is |
|---|---|
| `rules.md.tmpl` | The "Working habits" block written into a folder's `CLAUDE.md` (or `CLAUDE.local.md` when the folder is inside a git repo), between `<!-- deskmate:habits:begin … -->` and `<!-- deskmate:habits:end -->`. Text outside the markers is never touched. |
| `hub-skeleton/` | A new docs hub: `README.md`, `changelog/`, `design/` (with `_template.md`), `knowledge-base/`, `projects/`. Files ending in `.tmpl` are rendered; nothing in an existing hub is overwritten. |
| `habits.example.json` | The end-of-turn check's config, as setup writes it to `${XDG_CONFIG_HOME:-~/.config}/deskmate/habits.json`. |
| `examples/deskmate-team.json` | An example team manifest (below). |
| `examples/agents/` | An example team subagent, the shape the agents import expects. |

## Template syntax

`{name}` is replaced by a value. A line that is exactly `{?flag}` opens a section kept only when the flag is
on, and `{/flag}` closes it. An unknown name or flag is an error, so a rendered block never has a leftover
placeholder. Flags of the rules block: `multi`, `single`, `hub`, `nohub`, `notify`, `check`, `codegraph`,
`agents`.

## The team manifest

A docs hub may hold `deskmate-team.json` at its root. Setup reads only these keys; all are optional.

| Key | Meaning |
|---|---|
| `changelog_dir` | The changelog folder inside the hub (default `changelog`). |
| `repos` | `[{"name", "folder"}]`: a repo whose folder name differs from the name it is logged under. |
| `skip_repos` | Repo folders the rules leave out and the end-of-turn check ignores. |
| `check_mode` | The default end-of-turn check (`off`, `remind`, `require`) when the user has not picked one. |
| `agents` | Where the team's subagents come from: `{"git": "<url>", "path": "agents"}` or `{"folder": "<path in the hub>"}`. They are checked first (home paths, local network host names, anything that looks like a secret) and copied into `<folder>/.claude/agents/` only when the check is clean or the user accepts what it found. |
| `lint_deny` | Extra words the agents check flags (an internal host name, say). |

## What never ships here

Nothing personal: no memory notes, no machine or infrastructure details, no webhook URLs, no tokens, no
paths from someone's home folder. No rule lets a session commit, push, merge or deploy without being asked;
a standing permission is the user's own and belongs in their memory. `setup/tests/test_habits_pack.py`
checks this.
