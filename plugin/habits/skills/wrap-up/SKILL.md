---
name: wrap-up
description: Close out a piece of work so nothing is lost. Covers changelog entries for every repo changed, design doc status, the knowledge base, a status memory for unfinished work, loose ends, and a milestone notification. Use when a task is finished, before a long pause or /compact, at the end of a session, and when the user says "wrap up", "that's it for today", "we're done" or "save where we are".
---

# Wrap up

Go through this list. Do each item that applies and skip the rest. Then report in a few lines what you did and what is left.

1. **What changed.** For each repo you touched this session, run `git -C <repo> status --short` and `git -C <repo> log --since=midnight --oneline`.
2. **Changelog.** If the workspace rules name a docs hub, each changed repo has today's entry that covers this work. Write or extend it with the changelog-entry skill.
3. **Design doc.** If the work has one, its status line, decisions and open questions are current. If it shipped, mark it implemented and link the changelog entry (the design-doc skill).
4. **Knowledge base.** Anything you learned that a teammate would need goes into the hub (`projects/<repo>/` or `knowledge-base/`).
5. **Memory.** If the work is unfinished, save or update one short status memory: what is done, what is next, which branches, what is not committed, pushed or deployed. If it is finished, update or delete that memory so it does not go stale. Save any correction the user gave you about how to work. Never save secrets.
6. **Loose ends.** List uncommitted changes, unpushed branches, and migrations or deploy steps still to run. Do not commit, push, merge or deploy unless the user asked.
7. **Milestone.** If the deskmate MCP server's `notify` tool (`mcp__deskmate__notify`) is available and this was a milestone (a feature done, something shipped, a failure the user must know about), post one or two plain lines: what happened, where, what is next. No secrets, no file contents.
