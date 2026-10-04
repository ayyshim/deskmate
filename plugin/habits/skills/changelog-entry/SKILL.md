---
name: changelog-entry
description: Write or extend today's changelog entry in the docs hub for a repo you changed. Use when you finish a change in a repo (feature, fix, refactor, test or docs), after a commit or merge, when a "Deskmate habits" note says an entry is missing, and when the user asks to log, record or write up a change ("add a changelog entry", "update the changelog", "log this").
---

# Changelog entry

Every change in a repo gets an entry in the docs hub. One file per repo per day.

## 1. Find the file

1. The hub and its changelog folder are named in the workspace rules (the "Working habits" section of `CLAUDE.md` or `CLAUDE.local.md`, or the folder's own rules). If no hub is named, ask the user where changelogs go and stop.
2. Read the changelog folder's `README.md`. Its format and rules win over this skill.
3. The repo name is the repo's folder name. A git worktree logs under its main repo's name.
4. Run `date +%F` and `date +%H:%M` for the local date and time.
5. The file is `<hub>/<changelog folder>/<repo>/<date>.md`, usually `<hub>/changelog/<repo>/<date>.md`.

## 2. Gather the facts

- What changed: `git -C <repo> status --short`, `git -C <repo> diff --stat`, and today's commits (`git -C <repo> log --since=midnight --oneline`).
- How it was verified: the tests, builds and checks you ran, and their results. Never claim a check you did not run.
- The design doc for this work, if there is one.
- The impact on other repos: a client of a changed API, a counterpart that syncs the data, a shared package, a migration or command to run on deploy.

## 3. Write

- No file yet: create it. The first line is `# <date>`.
- The file exists: append. Never rewrite, reorder or delete earlier entries. If the newest entry is yours, from this session and about the same change, extend it instead of adding another.
- One entry per logical change, not per file. A change that spans repos gets an entry in each repo's file, and each one says so.
- Use the format in the README. Unless it says otherwise:

  ```markdown
  ## HH:MM — <short title>

  **Scope:** <module or area>
  **Type:** feature | fix | refactor | chore | docs | test
  **Design doc:** design/<feature>/... (if any)

  ### What changed
  - <one bullet per meaningful change, with file paths>

  ### Why
  - <motivation or ticket>

  ### How to verify
  - <commands run, tests, manual steps>

  ### Follow-ups / risks
  - <deploy steps, migrations, impact on other repos, or "none">
  ```

- Reference code as `<repo>/<path>:<line>`.
- Keep it short. Link the design doc for detail.
- Never write secrets, tokens or `.env` values. Name credential files by path only.

## 4. After

- If this change implements a design, update the design doc's status and link this entry (the design-doc skill).
- Tell the user in one line which file you wrote.
