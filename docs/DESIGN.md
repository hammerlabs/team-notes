# Design

The design in order: requirements, entities, API, high-level design, then the deep dives. The [README](../README.md) gives the reasons for each decision under the key shown here (T, E, D1 to D9, L, S, G); the [diagram](architecture.md) carries the same keys.

## 1. Requirements

### Functional

Users should be able to:

1. sign up and log in.
2. create a team, and team admins should be able to add and remove its members.
3. create notes in their teams, and read and delete any note of their teams.
4. edit any note of their teams, one editor at a time, while teammates keep reading it.
5. Users viewing a note should be able to see that it is being edited and be told when to reload it.
6. list a team's notes, most recently edited first, and search them by text.
7. Team admins should be able to release a note that another member is holding.

Below the line: edit the same note at the same time; share a note outside its team, or keep one private; restore a deleted note or an earlier version; add attachments or rich text; invite by email, change roles, rename or delete a team; log out, reset a password or use single sign-on; a service administrator who can see or manage every team (an exception to team isolation, so it needs its own access path and an audit log).

### Non-functional (T)

The system should:

1. be secure across teams: no request reads or changes data outside the caller's teams, and an outsider cannot tell whether a team or note exists (D1, D9).
2. prioritise consistency over availability for edits and membership: an accepted save is never silently overwritten, and a removed member loses access on their next request (D1, D2, D3).
3. recover a note from an editor who has gone: within 60 s if they disappeared, and within 10 minutes if their tab is open but untouched (D2).
4. have low-latency change notification: a viewer is told within 1 s that a note was saved, locked or unlocked (D4).
5. have low-latency list and search: under 200 ms at 10,000 notes per team (D7).
6. be able to serve about 20 teams of up to 50 people from one process, including every member waiting on a note at once (D4, D6, D8).
7. run from one command with nothing installed but uv, and run the same on Postgres in a container (D5).

Below the line: high availability and fault tolerance; compliance, audit logging and encryption at rest; rate limiting and login throttling; more than one region or instance.

### Assumptions

The brief leaves these open; each is a guess, labelled as one.

| Question | Assumption | Why |
|---|---|---|
| Private notes, or sharing with one person? | No. A note belongs to one team; the whole team sees it. | One access rule is easy to get right and to test. |
| A user in several teams? | Yes. | The membership table makes it free. |
| Roles? | `admin` and `member`. Admins manage members and can break locks; both write notes. | Enough to show role checks without a permission system. |
| Who deletes a note? | Its author or a team admin. | Stops one member wiping another's work. |
| How does someone join? | An admin adds an existing user by email. | No email sending, no invite tokens. |
| What is a note? | A title and a plain-text body. | No rendering, no attachments. |
| Two people want to edit? | The first takes a lock; the second gets 409 naming the holder. | D2 |
| The editor's laptop closes, or the tab is forgotten? | The lock is a 60 s lease renewed every 20 s, and ends after 10 minutes without activity; an admin can break it. | D2 |
| Lock per user or per tab? | Per user. | Two tabs of one user are caught by the version check. |
| How do viewers hear of changes? | A long poll, held until the note changes or 25 s pass. | D4 |
| Is delete permanent? | Yes. | Soft delete adds a filter to every query. |

## 2. Entities (E)

User, Session (a login token), Team, Membership (user, team, role), Note (belongs to one team), Edit lock (on a note: who holds it, until when, and when the holder was last active). The lock is three columns on the note, so one statement can check the lock and the version together (D3). The tables are in the README's [Entities](../README.md#entities-e); [app/models.py](../app/models.py) is the source. Constraints not shown there:

- Foreign keys from every `team_id`, `user_id`, `author_id` and `lock_user_id` to their tables.
- `memberships.role` is checked to be `admin` or `member`; `memberships` has an index on `user_id` (for `GET /teams`).
- `notes.version` is a 64-bit integer, checked to be at least 1.
- `notes.lock_user_id`, `lock_expires_at` and `lock_active_at` are all null or all set.

## 3. API

REST and JSON. Every route except signup, login, `/health` and the static pages needs `Authorization: Bearer <token>`. Everything a team owns is nested under `/teams/{team_id}`, so one membership check on that path parameter guards it all, and every note query filters on the same `team_id`. The exact contract, error codes and client protocol: [the API contract](API_CONTRACT.md).

```
POST   /auth/signup   POST /auth/login   GET /me   GET /health
POST   /teams         GET  /teams        GET /teams/{team_id}
GET    /teams/{team_id}/members          POST   /teams/{team_id}/members   (admin)
DELETE /teams/{team_id}/members/{user_id}                                  (admin, or yourself)
POST   /teams/{team_id}/notes            GET    /teams/{team_id}/notes?q=&limit=&cursor=
GET    /teams/{team_id}/notes/{note_id}  PATCH  .../notes/{note_id}  {title?, body?}  If-Match
DELETE /teams/{team_id}/notes/{note_id}  If-Match                          (author or admin)
PUT    .../notes/{note_id}/lock[?renew=true&active=true|false]   DELETE .../notes/{note_id}/lock
GET    .../notes/{note_id}/changes?since=<tag>&wait=<seconds>              (long poll)
```

Every single-note response carries `ETag: "<version>"`. A save or delete without `If-Match` is 428 `precondition_required`; a malformed or stale one is 412 `precondition_failed` with the current note. Checks answer in this order: authentication, team access, the lock rule (and authorship for a delete), then the precondition.

The lock is a sub-resource with `PUT` and `DELETE`; taking a lock you hold renews it, which is why it is a `PUT`. A renewal must say whether the holder was active; an idle holder's renewal is 409 `lock_idle`. Retrying a write: signup and add-member get 409 (a unique constraint stops the duplicate); `PUT` the lock gives the same result; a retried save gets 412 carrying the note that already holds the edit (the contract's retry rule); deletes end in the same state; creating a team or a note twice makes two (no idempotency keys).

## 4. High-level design

One FastAPI process, one worker. A request authenticates by token, passes the membership check if it names a team, and runs a handler. Handlers call the store (`app/store`), on SQLite by default or Postgres when `DATABASE_URL` says so. The only state outside the database is the long-poll notifier in the process's memory. Under Docker, `db` (Postgres 17, not published to the host) and `app` (the service alone) are two containers started by one `docker compose up --build`.

From each endpoint to its query and the index that serves it:

| Endpoint | Query | Served by |
|---|---|---|
| every authenticated call | session by `token_hash`, not expired | `sessions` primary key |
| every `/teams/{team_id}/...` call | membership by `(team_id, user_id)` | `memberships` primary key |
| `GET /teams` | memberships by `user_id` | `ix_memberships_user_id` |
| `POST /auth/login` | user by `email` | the unique index |
| `GET .../notes` | `team_id = ?`, `(updated_at, id) < cursor`, order by `updated_at DESC, id DESC`, limit + 1 | `ix_notes_team_recent`, one range |
| one note, lock, save, delete, changes | one row by `id` and `team_id` | `notes` primary key |
| `GET .../notes?q=` | substring match on title and body within one team | the same range, filtered: a scan of one team (D7) |

Arithmetic changed the design in three places: the list index with keyset paging (a page reads one range of rows already in order, wherever it starts), the long poll over plain polling (D4), and the connection pool sized to the thread pool (D6). Figures: [measurements](MEASUREMENTS.md).

Problems the drawing raises, each answered by a decision: a forgotten membership check leaks a team (D1); a vanished or idle editor leaves a note locked, and a lapsed lease followed by a stale save loses work (D2); several endpoints check and then act (D3); a held request could tie up a thread and a connection for 25 s, and an expiring lease writes nothing to wake anyone (D4, D6); one store must behave the same on two databases (D5); a substring search cannot use an index, and pages counted by position shift under concurrent edits (D7); sync handlers hop between threads (D8); tokens must be revocable and passwords safe at rest (D9).

## 5. Deep dives and growth

These continue in the README: the [decisions](../README.md#decisions) D1 to D9, the [limits](../README.md#limits-l), [what I would add with more time](../README.md#what-id-change-add-or-stop-doing-with-more-time) and the [growth path](../README.md#growth-path-g).

## Behaviours to know

Consequences of the decisions and cuts that a client or operator may trip over:

- Admins are peers: any admin can remove another, the creator included, while one admin remains.
- Notes outlive their author's membership and still name their author; an admin can delete them. Locks do not: removing a member releases their locks in that team and wakes those notes' viewers.
- Adding a member needs no consent and tells the admin the user's name and id. `user_not_found` tells an admin whether an email has an account, and signup's `email_taken` tells anyone.
- A lease and the idle limit are judged at request arrival by the app's clock, so a write that waited out the busy timeout is judged up to 5 s late.
- A vanished editor blocks a note for up to 60 s, and an open but untouched tab for up to 10 minutes, unless an admin breaks the lock.
- The server trusts the renewal's `active` flag; a client that always sends `true` holds the lock until an admin breaks it.
- A lock passing the idle limit writes nothing, so a held poll reports it at the holder's next renewal, the lease's end, or the end of the poll's wait.
- A delete is fenced like a save: `If-Match` with the current version, and not while someone else holds the lock.
- A paging walk never repeats a note; a note edited during the walk moves to the front and is not shown again by that walk, and a note created during it is not shown.
- A busy database answers 503 `busy` with `Retry-After: 1`; an unreachable one answers 500.
- `/health` reports the process only, not the database.
- There is no CORS: the API takes a bearer header, never a cookie, so there is no cross-site request surface to open.
