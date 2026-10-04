---
name: design-doc
description: Start, extend or close a design doc in the docs hub and keep the design index current. Use before building a non-trivial feature or module (new endpoints or data model, work across repos, a new page or flow, a migration), when the user says "design this", "write a design doc" or "plan this feature", when a decision or open question about a designed feature is settled, and when a designed feature ships.
---

# Design doc

Design docs live in `<hub>/design/<feature>/`. The hub is named in the workspace rules (the "Working habits" section of `CLAUDE.md` or `CLAUDE.local.md`, or the folder's own rules). If no hub is named, ask the user and stop.

## Start or extend

1. Read `<hub>/design/README.md` (layout, naming, index) and `<hub>/design/_template.md`.
2. Look for the feature in the index. If a folder exists, extend it. Do not start a second one.
3. New feature: create `<hub>/design/<feature>/00-overview.md` from the template. The folder name is the feature in kebab-case. A small change can be a single `<hub>/design/<feature>/<feature>.md`.
4. Fill in what you know: problem and goals, requirements with acceptance criteria, current behaviour with code references, the design per repo, the impact on other repos, the test plan and the rollout. Read the code before you describe it.
5. Mark what you do not know as an open question (`> ❓`). Do not invent requirements. Ask.
6. Add a row to the index table in `<hub>/design/README.md`: link, status, repos touched.
7. If the user asked for a design first, show them the summary and the open questions before you build.

## While building

- Record each decision in the decisions table: date, decision, why.
- When a requirement changes, change it in the doc in the same session.
- Keep the status line current: draft, in review, approved, in progress, implemented. Update the index row to match.

## When it ships

- Put `**Status:** implemented on YYYY-MM-DD — see changelog/<repo>/YYYY-MM-DD.md` at the top of `00-overview.md`.
- Update the index row. Close the open questions that are answered.

## Rules

- Reference code as `<repo>/<path>:<line>`. Link the knowledge base instead of repeating it.
- Link prototypes and tickets. Do not paste generated HTML or long logs into the hub.
- Never write secrets or `.env` values.
