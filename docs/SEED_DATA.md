# Seed data

A small world to explore from the test page: four people, two teams that share one member, and fifteen notes.

## Running it

```sh
uv run python -m app.seed                     # into ./notes.db, the default database
uv run uvicorn app.main:app --port 8765       # then open http://localhost:8765/
```

For Postgres, set the same `DATABASE_URL` for both commands:

```sh
export DATABASE_URL=postgresql://user:password@localhost:5432/notes
uv run python -m app.seed
uv run uvicorn app.main:app --port 8765
```

`scripts/run.sh` and the Docker image run the seed on every start with `--if-empty`: it seeds only a database with no accounts, so demo data never lands among real sign-ups.

The seed goes through the same storage calls as the API, so every rule applies to it. It creates the tables if they are missing.

Running it again is safe. If every demo account already exists it prints `Already seeded` and changes nothing. If only some exist it refuses and changes nothing; start from an empty database (delete `notes.db`, or use a fresh Postgres database).

## Logins

Everyone's password is `demo-password`.

| Email | Name | Teams |
|---|---|---|
| `ada@example.com` | Ada Lindqvist | Platform (admin) |
| `ben@example.com` | Ben Okafor | Platform (member), Spring Launch (admin) |
| `chloe@example.com` | Chloé Durand | Spring Launch (member) |
| `dev@example.com` | Dev Raman | Spring Launch (member) |

## Teams

| Team | Member | Role |
|---|---|---|
| Platform | Ada Lindqvist | admin (creator, the only admin) |
| Platform | Ben Okafor | member |
| Spring Launch | Ben Okafor | admin (creator, the only admin) |
| Spring Launch | Chloé Durand | member |
| Spring Launch | Dev Raman | member |

Ben is the overlap: a plain member in one team and the admin of the other. Ada sees only Platform; Chloé and Dev see only Spring Launch.

## Notes

Newest edit first, as the list shows them. All are at version 1 and unlocked, except the handover note, which Ada edited twice (version 3).

Platform (7):

| Title | Author | Why it is there |
|---|---|---|
| On-call handover (updated) | Ada | Version 3: locked, saved twice, released by the seed |
| Retro draft (edit together) | Ada | Empty headings, for the two-person edit demo |
| Reading list | Ben | Short |
| Glossary | Ada | Contains `99.9%`, `100%` and `snake_case`, for literal `%` and `_` in search |
| Offsite at CAFÉ MÖSL | Ben | Capital accented letters in the title, for case folding |
| Incident 2026-11-03: slow logins | Ben | Long, structured |
| Postgres migration plan | Ada | Long, several sections |

Spring Launch (8):

| Title | Author | Why it is there |
|---|---|---|
| Open questions | Chloé | Short |
| Budget | Dev | Contains `100%` |
| Pop-up at Hauptstraße 12 | Dev | `ß` in the title: the one search difference between SQLite and Postgres |
| Press release draft | Ben | Long; lower-case `café` in the body |
| Menu draft (edit together) | Chloé | For the two-person edit demo (Chloé and Dev, or Ben) |
| Supplier contacts | Dev | Fake `example.com` addresses |
| Café noir tasting notes | Chloé | Accented title |
| Launch timeline | Ben | Short |

## Things to try

To have two people signed in at once, log in on the test page, tick **Two-user demo** and log the second person into the right-hand pane; it follows the note open in the main pane. Two browser windows (not a duplicated tab, which copies the session) also work.

1. **Isolation.** Sign in as Ada: only Platform is listed. Sign in as Chloé: only Spring Launch. Ben sees both. With Ada's token, ask the API for a Spring Launch note directly (for example `curl -H "Authorization: Bearer $ADA" localhost:8765/teams/<launch id>/notes`): the answer is 404 `not_found`, the same as for a team that does not exist, so Ada cannot even tell it is there.
2. **Search.** In Platform, search `café`: it finds "Offsite at CAFÉ MÖSL" (case folded across accents). In Spring Launch, `café` finds three notes, but `cafe` finds only the press release (its `cafe.example.com` address): accents are not stripped. `100%` finds Glossary or Budget only, and `_case` finds Glossary: `%` and `_` are literal. `hauptstrasse` finds the pop-up note on SQLite but not on Postgres (the documented folding difference); `Hauptstraße` finds it on both.
3. **Lock.** In Spring Launch, Chloé opens "Menu draft (edit together)" and presses Edit. Dev opens the same note: Edit is refused with `note_locked`, naming Chloé and when her lease ends. Chloé saves (version 2) and presses Done; now Dev can take the lock. A holder who types nothing for 10 minutes loses the lock even while it keeps renewing: the next renewal is refused with `lock_idle` and anyone can take it.
4. **Long poll.** Leave Dev viewing the menu draft while Chloé edits and saves. Dev's view updates within a moment of each lock, save and release, without reloading: the page holds a `GET .../changes?since=<tag>` open and the save wakes it.
5. **Admin break.** While Chloé holds the lock on the menu draft, Ben (admin of Spring Launch) releases it: allowed, Chloé's page shows the lock as lost and keeps her text. Then in Platform, Ada locks "Retro draft (edit together)": Ben's Break lock button is disabled there, because in Platform he is only a member, and the same `DELETE .../lock` sent with his token is 403 `forbidden`. Ada leaving Platform is refused too (409 `last_admin`): it would leave the team with no admin.
