# Design docs

How a feature or module is (or should be) built lives here, before and while it is built. One folder per feature. The docs inside are numbered so their order is obvious.

```
design/
  README.md             ← this file
  _template.md          ← copy this to start a design doc
  <feature>/
    00-overview.md      ← problem, goals, non-goals, glossary
    01-requirements.md  ← functional and non-functional requirements, acceptance criteria
    02-architecture.md  ← components per repo, data model, API contracts, impact on other repos
    03-permissions.md   ← new or changed permissions and roles, and where clients check them
    04-ui.md            ← screens, states, flows
    05-test-plan.md     ← test scenarios, fixtures, edge cases
    99-decisions.md     ← dated log of decisions and trade-offs
```

Not every feature needs every file. Start with `00-overview.md` and add files as the design grows. A small change can live in a single `<feature>.md`.

## Naming

- Folder name: the feature or module in kebab-case, e.g. `group-booking`, `invoice-export`.
- A feature that spans repos gets one folder, with a section per repo inside the docs, not a folder per repo.

## Conventions

- Reference code with the repo name first: `<repo>/<path>:<line>`.
- Link the knowledge base (`knowledge-base/...`, `projects/<repo>/...`) instead of repeating it.
- State assumptions. Mark open questions with `> ❓`.
- Link prototypes and tickets. Do not paste generated HTML or long logs here.
- When a design is implemented, put this line at the top of `00-overview.md`: `**Status:** implemented on YYYY-MM-DD — see changelog/<repo>/YYYY-MM-DD.md`, and update the index below.
- Never paste secrets or `.env` values.

## Index

| Feature / module | Status | Repos touched |
|---|---|---|
