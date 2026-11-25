# Tests: which claim each file backs

Each test module's docstring says what it covers, what it leaves to other files, and how far it can be
trusted. Each test's docstring says what it pins and why it exists. The docs site renders both under
"Tests".

| Design claim | Tested in | Run just that part |
|---|---|---|
| A non-member cannot see or change anything in a team, and cannot tell whether it exists (404, never 403) | `test_isolation.py` (matrix, plus structural checks that every team route is guarded and has a matrix row) | `uv run pytest tests/test_isolation.py` |
| One editor at a time: the lock is a 60 s lease, renewed every 20 s, breakable by an admin, and over after 10 minutes without activity (`lock_idle`) | `test_locks.py` (idle rule: the tests after `keep_renewing`), `test_changes.py::test_poll_returns_soon_after_an_idle_release`, `test_races.py::test_idle_release_and_another_take_never_lose_the_new_holders_lock` | `uv run pytest tests/test_locks.py` |
| The version check backs up the lock, so a stale save never overwrites silently | `test_locks.py::test_stale_save_after_lease_lapse_is_a_version_conflict`, `test_races.py` (same-version saves) | `uv run pytest -k "stale_save or same_version"` |
| Every guarded write is one conditional statement (the lock: a renewal attempt, then the take), so concurrent requests cannot both pass a check | `test_races.py` (thread races mutation-checked on both databases, with the result in each docstring; the idle-release race says it is not) | `uv run pytest tests/test_races.py` |
| Viewers hear of a save, lock or unlock within a second, a waiting poll holds no thread or connection, and SIGTERM ends held polls at once | `test_changes.py` | `uv run pytest tests/test_changes.py` |
| No request holds a connection while waiting for a thread, so pool = threads never deadlocks | `test_burst.py` | `uv run pytest tests/test_burst.py` |
| The wire format is exactly the contract's | `test_contract.py`, `test_http.py` (OpenAPI error docs) | `uv run pytest tests/test_contract.py` |
| Limits and HTTP guards: 1 MB body cap, 503 `busy` with `Retry-After`, security headers | `test_http.py` | `uv run pytest tests/test_http.py` |
| Only "gave up waiting" errors are `busy` (SQLite locked; Postgres lock timeout, deadlock, serialization failure; pool timeout) | `test_store.py` (the mapping, without forcing each error) | `uv run pytest tests/test_store.py` |
| Same behaviour on SQLite and Postgres, except one documented search difference | the whole suite on each database; `test_store.py` for the difference and the settings | see below |
| Every error is in the contract's shape, including 400 `bad_request` and 500 `internal_error` | `test_auth.py`, and `assert_error` in the refusal checks | `uv run pytest tests/test_auth.py` |
| Accounts, teams and notes behave as the contract says, one request at a time | `test_auth.py`, `test_teams.py`, `test_notes.py`, `test_locks.py` | `uv run pytest tests/test_auth.py tests/test_teams.py tests/test_notes.py` |
| Paging by cursor never repeats a note or skips an unchanged one while others are created, edited and deleted; equal edit times split cleanly; a bad cursor is 422, never 500; a cursor page is one index range on both databases | `test_notes.py` (the `cursor`, `walk` and `edit_times` tests), `test_store.py::test_a_cursor_page_is_one_range_of_the_list_index` | `uv run pytest -k "cursor or walk or edit_times"` |
| The demo data matches `docs/SEED_DATA.md` and seeding is safe to repeat | `test_seed.py` | `uv run pytest tests/test_seed.py` |

Shared fixtures are in `conftest.py` (fresh app and database per test, fake clock, cheap password
hashing) and helpers in `helpers.py` (`race`, `assert_error`, API shortcuts).

## Running the whole suite

On SQLite (the default), a new database file per test:

```sh
uv run pytest
```

On Postgres, a new schema per test on the server named by `TEST_DATABASE_URL`:

```sh
docker run -d --name team-notes-pgtest -e POSTGRES_PASSWORD=pgtest -p 127.0.0.1:55439:5432 \
  postgres:17 -c fsync=off -c synchronous_commit=off -c full_page_writes=off
TEST_DATABASE_URL=postgresql://postgres:pgtest@127.0.0.1:55439/postgres uv run pytest
docker rm -fv team-notes-pgtest
```

`postgres:17` is the server version the Docker path runs. This container keeps the image's default locale, while
compose creates its database with the builtin `C.UTF-8` locale; search folding is the behaviour that depends on it. `-v` removes the
container's anonymous data volume with it. If the server is still starting, the first tests fail to
connect; wait for `docker exec team-notes-pgtest pg_isready` first.

Either way one test is skipped: the one that inspects the other database's internals
(`sqlite_only` or `postgres_only`). The report header names the database used.

Lease and token expiry are tested by moving a fake clock, never by sleeping. Race and long-poll tests
use real concurrent requests from threads.
