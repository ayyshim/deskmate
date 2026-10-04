# Knowledge base

How things work across the repos: the facts a teammate, or a new Claude Code session, needs and cannot get quickly from the code. Docs for one repo live in `projects/<repo>/`. This folder is for what crosses repos.

## What goes here

| File | What is in it |
|---|---|
| `conventions.md` | Shared data conventions: identifiers, money, time, status values, naming |
| `permissions.md` | How permissions work end to end, and the recipe for adding one |
| `environments.md` | Local and test environments: URLs, ports, env var names, and where credentials live (by path only) |
| `feature-development.md` | How a feature is made end to end, and in what order |
| `known-issues.md` | Verified bugs, risks and drift, and what was checked and is intentional |
| `glossary.md` | Domain and codebase vocabulary |

Create a file when you first have something to put in it. Add a row here when you add a file.

## How to write

- Write what you verified, with code references (`<repo>/<path>:<line>`). Say what you assumed.
- One topic per section. Short sentences.
- When a fact changes, fix it here in the same session that changed it.
- Never write secrets. Name the file a credential lives in, never its contents.

## Hub or memory?

| It is... | Put it in |
|---|---|
| How the system works, a convention, a gotcha a teammate would hit | this hub |
| How you like Claude to work, a fact about your own machine, where your unfinished work stands | your Claude Code memory |
| A password, token or key | neither |
