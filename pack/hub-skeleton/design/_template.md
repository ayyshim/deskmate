# <Feature or module name>

**Status:** draft | in review | approved | in progress | implemented (YYYY-MM-DD)
**Owner:** <name>
**Repos touched:** <repo>, <repo>
**Related:** changelog entries, tickets, other design docs

## 1. Problem and goals

- What problem are we solving, and for whom (which kind of user or operator)?
- Goals. Non-goals.

## 2. Requirements and acceptance criteria

| # | Requirement | Type (functional / non-functional) | Acceptance criterion |
|---|---|---|---|
| R1 | | | |

## 3. Current behaviour

What exists today, with code references (`<repo>/<path>:<line>`).

## 4. Proposed design

### 4.1 Data model
Per repo: new or changed models, tables or entities, migrations, and anything that syncs between repos or services.

### 4.2 API contracts
Method, path, request and response shape, the permission required, and which client consumes it.

### 4.3 Permissions
New or changed permissions (follow each repo's convention, see `knowledge-base/permissions.md`), the roles that get them, where clients check them, and the commands to run after the change.

### 4.4 Clients
Pages, screens, menu entries, components, stores and state that change in each client.

### 4.5 Background jobs and realtime
Scheduled jobs, queues, events, sockets.

## 5. Impact on other repos

Who consumes what changes, and the order to deploy in. Say "none" when there is none.

## 6. Test plan

Scenarios: happy path, edge cases, permission denied, offline or failure cases. Fixtures and seed data. Environments (see `knowledge-base/environments.md`).

## 7. Rollout

Migration order across services, feature flags, backfills, how to roll back.

## 8. Decisions and open questions

| Date | Decision / question | Outcome |
|---|---|---|
| | | > ❓ |
