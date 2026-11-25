"""Create a throwaway database for the measurements: 2 teams of 10,000 notes of 1 to 3 KB, 50 users.

Every user is in both teams (user 0 is admin) and has a session whose token is `common.token(i)`, so the
other scripts can authenticate without logging in. About one body in five has a non-ASCII word, like real
text. Bodies are random words from a fixed seed, so every run builds the same data.

    uv run python scripts/measure/seed.py                      # SQLite: .measure/measure.db (about 70 MB)
    uv run python scripts/measure/seed.py postgresql://...     # Postgres: its tables are dropped and recreated

Writes `.measure/meta.json` (the URL, team ids, sample note ids) for the other scripts.
"""

import json
import random
import string
import sys
import uuid
from datetime import UTC, datetime, timedelta

from common import DATA_DIR, DEFAULT_URL, META_PATH, token
from sqlalchemy import insert
from sqlalchemy.engine import make_url

from app.models import AuthSession, Base, Membership, Note, Team, User
from app.security import hash_token
from app.store import make_store

USERS = 50
TEAMS = 2
NOTES_PER_TEAM = 10_000
SAMPLE_EVERY = 100  # every 100th note's id goes into meta.json for scripts that need one note per saver

rng = random.Random(7)
WORDS = ["".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 10))) for _ in range(20_000)]
NON_ASCII = ["café", "Straße", "naïve", "über", "résumé"]


def paragraph(non_ascii: bool) -> str:
    words = rng.choices(WORDS, k=80)
    if non_ascii:
        words[rng.randrange(80)] = rng.choice(NON_ASCII)
    return " ".join(words).capitalize() + ". "


PLAIN = [paragraph(False) for _ in range(600)]
ACCENTED = [paragraph(True) for _ in range(600)]


def body(size: int) -> str:
    pool = ACCENTED if rng.random() < 0.2 else PLAIN
    parts: list[str] = []
    while sum(map(len, parts)) < size:
        parts.append(rng.choice(pool))
    return "".join(parts)[:size]


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    DATA_DIR.mkdir(exist_ok=True)
    if make_url(url).get_backend_name() == "sqlite":
        path = make_url(url).database or ""
        for suffix in ("", "-wal", "-shm"):
            (DATA_DIR / (path.rsplit("/", 1)[-1] + suffix)).unlink(missing_ok=True)
    store = make_store(url, pool_size=2)
    Base.metadata.drop_all(store.engine)
    store.create_schema()

    start = datetime(2026, 1, 1, tzinfo=UTC)
    users = [str(uuid.uuid4()) for _ in range(USERS)]
    meta: dict = {"url": url, "users": USERS, "teams": []}
    with store.engine.begin() as db:
        db.execute(
            insert(User),
            [
                {
                    "id": u,
                    "email": f"user{i}@example.com",
                    "name": f"User {i}",
                    "password_hash": "x",
                    "created_at": start,
                }
                for i, u in enumerate(users)
            ],
        )
        db.execute(
            insert(AuthSession),
            [
                {"token_hash": hash_token(token(i)), "user_id": u, "expires_at": start + timedelta(days=36500)}
                for i, u in enumerate(users)
            ],
        )
        sequence = 0
        for t in range(TEAMS):
            team_id = str(uuid.uuid4())
            db.execute(insert(Team), [{"id": team_id, "name": f"Team {t}", "created_at": start}])
            db.execute(
                insert(Membership),
                [
                    {"team_id": team_id, "user_id": u, "role": "admin" if i == 0 else "member", "created_at": start}
                    for i, u in enumerate(users)
                ],
            )
            rows, samples = [], []
            for k in range(NOTES_PER_TEAM):
                sequence += 1
                at = start + timedelta(seconds=sequence * 7, microseconds=rng.randrange(10**6))
                note_id = str(uuid.uuid4())
                rows.append(
                    {
                        "id": note_id,
                        "team_id": team_id,
                        "author_id": rng.choice(users),
                        "title": f"Note {k} {rng.choice(WORDS)}",
                        "body": body(rng.randint(1000, 3000)),
                        "version": 1,
                        "created_at": at,
                        "updated_at": at,
                    }
                )
                if k % SAMPLE_EVERY == 0:
                    samples.append(note_id)
            db.execute(insert(Note), rows)
            meta["teams"].append({"id": team_id, "notes": NOTES_PER_TEAM, "sample_note_ids": samples})
    with store.engine.connect() as connection:
        if store.engine.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            # Planner statistics now, as autovacuum would give a live database; otherwise the first runs are
            # planned without them and the figures drift as autovacuum catches up.
            connection.exec_driver_sql("ANALYZE")
            connection.commit()
    store.close()
    META_PATH.write_text(json.dumps(meta))
    print(f"seeded {TEAMS} teams x {NOTES_PER_TEAM} notes, {USERS} users into {make_url(url).render_as_string()}")


if __name__ == "__main__":
    main()
