# Team Notes API contract

The exact wire format and the rules a client can rely on.

## Conventions

- JSON in and out. IDs are UUID strings. Times are ISO 8601 UTC with a `Z` suffix, for example `2026-03-09T14:51:44.907257Z` (the fraction is omitted when it is zero).
- Every API route except `POST /auth/signup`, `POST /auth/login` and `GET /health` requires `Authorization: Bearer <token>`. The pages under "Other" below need none.
- A request body may contain only the keys listed for it. Any other key (a typo such as `titel`, or a field the server sets such as `author_id`) is 422 `validation_error`.
- Every error, including validation errors, has this shape:

  ```json
  {"error": {"code": "note_locked", "message": "Human-readable text", "lock": {...}}}
  ```

  `code` and `message` are always present. Extra keys depend on the code (listed below). Switch on `code`, not on `message`.

| Status | Codes |
|---|---|
| 400 | `bad_request` (the body could not be read at all; malformed JSON is 422 instead) |
| 401 | `unauthorized` (missing, bad or expired token), `invalid_credentials` (login) |
| 403 | `forbidden` (a team member who lacks the role or ownership) |
| 404 | `not_found` (also returned to non-members for anything under a team), `user_not_found` (add member: no account has that email) |
| 405 | `method_not_allowed` |
| 409 | `email_taken`, `already_member`, `team_full`, `team_notes_full`, `last_admin`, `note_locked` (+`lock`), `lock_required`, `lock_idle` |
| 412 | `precondition_failed` (+`note`: `If-Match` is malformed or names an older version) |
| 413 | `payload_too_large` (request body over 1,000,000 bytes) |
| 422 | `validation_error` (+`details`: a list of `{loc, message, type}`) |
| 428 | `precondition_required` (a save or delete without `If-Match`) |
| 429 | `too_many_polls` (the caller already holds 10 long polls) |
| 500 | `internal_error` (a server fault; logged) |
| 503 | `busy` (the database is busy; sent with `Retry-After: 1`, nothing was written, safe to retry) |

`note_locked` carries `lock`: who holds the lock and until when. It can be `null`: the lock changed hands while the server was answering. Treat that as "retry the request".

## Objects

```
User        {id, email, name, created_at}
Team        {id, name, created_at, role}            role is the caller's role: "admin" | "member"
Member      {user_id, email, name, role, joined_at}
Lock        {user_id, user_name, expires_at, renew_after_seconds}
Note        {id, team_id, author_id, title, body, version, lock, tag, created_at, updated_at}
NoteSummary Note without body
NotePage    {items: [NoteSummary], next_cursor}     next_cursor: opaque string, or null on the last page
Change      {tag, version, lock}
```

- `lock` is a Lock or `null`. An expired or idle lock (see Lock) is reported as `null`.
- `renew_after_seconds` (20) is how often the holder should renew; the lease is 60 seconds. `expires_at` is the lease's end; an idle holder's lock can end sooner.
- `tag` is opaque: compare it only for equality with an earlier one. It is currently `"<version>:<lock holder user id or empty>"`, for example `"7:"` or `"7:3f2c..."`, but do not parse it. It changes on save, lock and unlock, not on lock renewal, and an expired or idle lock counts as no holder. It describes the note's state, not a history: a lock taken and released between two polls leaves the tag as it was, so that poll does not report it.
- `version` starts at 1 and goes up by one on every save.

## Limits

- `email`: valid address, stored lower-cased. Surrounding spaces and a display name (`Name <addr>`) are dropped. Login normalises the address the same way; an invalid one is 401 `invalid_credentials`. `password`: 8 to 128 characters. `name`: 1 to 100.
- No stored text may contain the NUL character (U+0000): user and team `name` and note `body` with one are 422 (titles refuse every control character). Postgres cannot store it, and the rule is the same on either database.
- Team `name`: 1 to 100. At most 200 members per team (409 `team_full`).
- At most 10,000 notes per team (409 `team_notes_full`).
- Note `title`: surrounding whitespace is removed, then 1 to 200 characters, no control characters (so a title of only spaces is refused). `body`: at most 100,000 characters, default `""`; newlines and tabs are fine.
- Note list: `limit` 1 to 100, default 50. `cursor` at most 200 characters, and it must decode to a valid position (send back a `next_cursor` the server issued). `q` at most 200 characters, no control characters. Any other query parameter, `offset` included, is 422 `validation_error`.
- The version in `If-Match`: 1 to 2^63 - 1 (a 64-bit signed integer, the database's range); anything larger is 412.
- Lock lease: 60 seconds per take or renew. Lock idle limit: 10 minutes since the holder's last activity. Long-poll `wait`: 0 to 25 seconds, default 25.
- Session lifetime: 7 days.
- Request body: at most 1,000,000 bytes, on every route, checked before authentication. A larger declared `Content-Length` is refused at once; a body without one is refused once it passes the cap. The note limits above count characters, this one counts bytes. Send JSON as UTF-8 without `\u` escapes for non-ASCII: a 100,000-character body then takes at most 400 KB of printable text (600 KB if it is all control characters, which JSON must escape). Escaping characters outside the Basic Multilingual Plane as surrogate pairs takes 12 bytes each and can push a legal body over the cap (about 1.2 MB), which is refused with 413.
- Long polls: at most 10 held at once per user, across all notes and sessions. Another is 429 `too_many_polls` until one ends.

## Endpoints

### Auth

| Call | Body | Success | Errors |
|---|---|---|---|
| `POST /auth/signup` | `{email, password, name}` | 201 User | 409 `email_taken` |
| `POST /auth/login` | `{email, password}` | 200 `{token, expires_at, user}`, sent with `Cache-Control: no-store` | 401 `invalid_credentials` |
| `GET /me` | | 200 User | |

A wrong password, an unknown email and a malformed email get the same answer, in about the same time.

### Teams

| Call | Body | Success | Errors |
|---|---|---|---|
| `POST /teams` | `{name}` | 201 Team, caller is admin | |
| `GET /teams` | | 200 `[Team]`, only the caller's, oldest first | |
| `GET /teams/{team_id}` | | 200 Team | |
| `GET /teams/{team_id}/members` | | 200 `[Member]`, in joining order | |
| `POST /teams/{team_id}/members` | `{email, role}` (role defaults to `"member"`) | 201 Member | 403 not admin, 404 `user_not_found`, 409 `already_member`, 409 `team_full` |
| `DELETE /teams/{team_id}/members/{user_id}` | | 204 | 403 unless admin or removing yourself, 404 not a member, 409 `last_admin` |

`GET /teams` is not paged: it lists only the teams the caller belongs to. `GET .../members` is bounded by the 200-member cap.

Removing a member also releases every lock they hold on the team's notes, in the same step; viewers of those notes are told at once through the long poll.

### Notes

| Call | Body or query | Success | Errors |
|---|---|---|---|
| `POST /teams/{team_id}/notes` | `{title, body}` | 201 Note, with `ETag` | 409 `team_notes_full` |
| `GET /teams/{team_id}/notes` | `?q=&limit=&cursor=` | 200 NotePage, newest edit first | 422 `validation_error` (a cursor that does not decode to a valid position, or an unknown parameter) |
| `GET /teams/{team_id}/notes/{note_id}` | | 200 Note, with `ETag` | |
| `PATCH /teams/{team_id}/notes/{note_id}` | `{title?, body?}`, at least one; header `If-Match` | 200 Note, version + 1, with the new `ETag` | 409 `lock_required` (caller holds no live lock and nobody else does), 409 `note_locked` (someone else holds it), then 428 `precondition_required`, 412 `precondition_failed` |
| `DELETE /teams/{team_id}/notes/{note_id}` | header `If-Match` | 204 | 403 unless author or admin, 409 `note_locked` if someone else holds a live lock, then 428 `precondition_required`, 412 `precondition_failed` |

Preconditions:

- Every response that returns one Note (create, read, save) carries `ETag`: the note's `version` as a quoted string, for example `"3"`. The `version` stays in the body too.
- A save and a delete must send `If-Match` with that value. Without the header: 428 `precondition_required`. With a malformed value, or one naming a version other than the current one: 412 `precondition_failed`, carrying the current note as `note`.
- Only a single strong ETag in the server's form is accepted. `If-Match: *` is refused (412): it matches any version, so it would let a client overwrite or delete text it never read. A weak (`W/"3"`) or listed (`"3", "4"`) value is 412 too, and so is `If-Match` sent on more than one header line, which is the same list split across lines.
- A save body that contains `version` is refused like any unknown key (422 `validation_error`).
- Checks run in this order, and the first that fails answers: authentication (401), team access (404 to a non-member, so an outsider still cannot tell a missing note from a forbidden one), the lock rule and, for a delete, authorship (409, 403), then the precondition (428, 412). A caller without the lock gets the lock error whatever `If-Match` says.

- `q` is a case-insensitive substring match on title and body (`café` finds `CAFÉ`). `%` and `_` in it match literally.
- How case is ignored depends on the database the server runs on, and this is the one place the two differ. On SQLite (the default) both sides are compared after Unicode case folding, which can turn one character into two: `strasse` finds `Straße`. On Postgres both sides are lowercased one character at a time by the database, so `strasse` does not find `Straße` (nor final `ς` match `σ`); accented letters such as `É` still fold, given the UTF-8 locale the server expects. Accents are never removed on either: `cafe` does not find `café`.
- Order: newest `updated_at` first; notes saved in the same microsecond are ordered by `id`, descending. The order is total, so every note has one place in it.
- Paging: omit `cursor` for the first page, then pass each page's `next_cursor` back as `cursor`, with the same `q` and `limit`, until it is `null`. Search pages the same way.
- A cursor is opaque: do not build, parse or edit one. It marks the position just after the last note of its page, and the next page starts there. It grants nothing: the team check runs on every page, and a cursor used in another team, or with another `q`, is only a position in that list. A cursor never expires, and the note it was taken from may be deleted meanwhile.
- A cursor that decodes to a valid position is accepted and pages from that position, whether or not it is byte for byte what the server wrote (a harmless variant such as fewer fractional digits or base64 padding is accepted too). Anything else, such as a malformed or truncated cursor, is 422 `validation_error` with one detail, `loc` `["query", "cursor"]` and `type` `invalid_cursor`: start again from the first page. Either way a cursor grants nothing.
- While other notes are created, edited and deleted between your page requests, one walk from the first page to `null` never returns a note twice, and never skips a note that existed, unedited, for the whole walk. A note edited during the walk moves to the front, so the rest of the walk does not show it again: it is missed if the walk had not reached it yet. A note created during the walk is in front of the walk and is not shown. To see such changes, start again from the first page.
- A save returns exactly the note it wrote. Saving does not release the lock.
- `precondition_failed` carries the server's current note as `note`. A delete is fenced the same way: an author or admin can delete a note that nobody else holds the lock on, but only at the version they last read.
- Retrying a save whose response was lost: the retry gets `precondition_failed`. If `error.note.version` is the version you sent in `If-Match` + 1 and its `title` and `body` equal what you sent, the first attempt landed; treat it as success.

### Lock

| Call | Success | Errors |
|---|---|---|
| `PUT /teams/{team_id}/notes/{note_id}/lock` | 200 Lock. Takes a free, expired or idle lock, or renews the caller's own. Counts as activity; `active` is ignored. | 409 `note_locked` |
| `PUT /teams/{team_id}/notes/{note_id}/lock?renew=true&active=<true\|false>` | 200 Lock. Renews the caller's live lock only; never takes one. `active` is required. | 422 `validation_error` (`active` missing), 409 `lock_idle` (the caller's lock had gone idle while its lease is still live; it is now released), 409 `lock_required` (the caller's lock is gone or its lease has lapsed, whether or not the caller was also idle), 409 `note_locked` (someone else holds it) |
| `DELETE /teams/{team_id}/notes/{note_id}/lock` | 204. Holder or admin. Also 204 if the note is not locked. | 403 when someone else holds it and the caller is not an admin, 409 `note_locked` with `lock: null` (retry) |

- Locks belong to a user, not to a token: a second session of the same user renews, saves under and releases the same lock.
- A lease is judged against the server's clock when the request arrives.
- Idle rule: the lock records its holder's last activity, which is taking the lock, a successful save, or a renewal with `active=true` (the holder has typed since the previous renewal). Once that is 10 minutes old the lock is over, even if its lease is still running: from then on it reads as `null` and anyone can take it, as for an expired lease. The holder's next renewal is refused with 409 `lock_idle` and releases the lock in the same step; viewers are woken as for a release. Idleness is judged before the renewal's own `active` flag, so a renewal that arrives after the limit gets `lock_idle` even with `active=true`. If someone has taken the lock by then, the renewal gets `note_locked` instead. `lock_idle` is the answer only while the lease is still live: once the lease has lapsed, the renewal gets `lock_required`, whether or not the holder was also idle.
- The server trusts `active`. A client that always sends `true` holds the note for as long as it renews, until an admin breaks the lock.

### Changes (long poll)

`GET /teams/{team_id}/notes/{note_id}/changes?since=<tag>&wait=<seconds>`

- If `since` is missing or differs from the note's current tag: 200 Change at once.
- Otherwise the request is held until the note is saved, locked or unlocked, until its lock expires, or until `wait` seconds pass.
- A lock passing the idle limit writes nothing, so a held poll is not ended at that instant: it reports the change when the holder's next renewal releases the lock, when the lease runs out, or at the latest when `wait` ends (it then answers 200 with the change, not 204).
- 200 Change when the tag changed, 204 with no body when it did not.
- 404 if the note is deleted or the caller is not a member. Membership is re-checked on every wake, so a member removed during a poll gets 404 on its next wake (at once, if they held a lock on the note).
- 429 `too_many_polls` if the caller already has 10 polls held.
- When the server begins to shut down, held polls end at once with 204; poll again.

### Other

- `GET /health` returns 200 `{"status": "ok"}`. It says the process is up, nothing more.
- `GET /` serves `static/index.html`, the manual test page; `GET /static/...` serves its stylesheet and scripts.
- `GET /docs` and `GET /redoc` are FastAPI's generated API explorers, from `GET /openapi.json`. Both load their scripts from a CDN.
- `GET /site/...` serves the docs site built by `mkdocs build` (unauthenticated, like `/static/...`). Before it is built: 404 `not_found` with a message saying how to build it.

## Client protocol

How an editor uses the calls above, one line per event. The test page follows it, except that it leaves every re-take of the lock to the user's Edit button.

| State | Event | Do |
|---|---|---|
| viewing | open a note | `GET` the note, then long-poll `changes` with its `tag`; on 200 re-`GET`, on 204 poll again |
| viewing | press Edit | `PUT .../lock` and go to acquiring |
| acquiring | 200 Lock | `GET` the note (newest text), keep its `ETag` (its `version`), go to editing |
| acquiring | 409 `note_locked` | stay viewing; the error names the holder |
| editing | every `renew_after_seconds` | `PUT .../lock?renew=true&active=<typed since the last renewal>`; any failure means the lock is lost |
| editing | 409 `lock_idle` on a renewal | the lock was released for inactivity: go to lock-lost and say so |
| editing | Save | `PATCH` with `If-Match` set to the kept ETag; on 200 keep the new `ETag` |
| editing | 409 `lock_required` on Save | the lease ran out but nobody took it: `PUT .../lock` again, then retry the save with the same `If-Match` |
| editing | 409 `note_locked` on Save, a poll showing another holder or none, or a failed renewal | go to lock-lost |
| editing | 412 `precondition_failed` | apply the retry rule above; otherwise show `error.note` and let the user merge |
| editing | Done | `DELETE .../lock`, go to viewing |
| lock-lost | (any) | keep the user's text, stop renewing, never `DELETE` the lock (it may be someone else's now); offer Edit to re-take it, after which Save may get `precondition_failed` |
| any | tab closed | send nothing; the lease runs out within 60 seconds |

An admin's lock break reaches the editor through the long poll (the tag changes) or, at the latest, the next renewal (`lock_required`).
