"""Demo data: four people, two teams that share a member, and notes worth searching.

    uv run python -m app.seed                                   # ./notes.db
    DATABASE_URL=postgresql://... uv run python -m app.seed     # Postgres
    uv run python -m app.seed --if-empty                        # only into a database with no accounts

Everything goes through the store, the same calls the API makes, so it works
on either database and every rule (caps, locks, versions) applies to it.

Safe to re-run: if every demo account already exists it does nothing; if
only some do, it refuses rather than guess what a half-seeded database
should look like. `--if-empty` (used by scripts/run.sh and the Docker
image on every start) skips seeding when any account exists, so demo
data never lands among real sign-ups and a start never fails here. docs/SEED_DATA.md lists what it creates and what to try.
"""

import sys
from dataclasses import dataclass
from datetime import timedelta

from app import clock, config
from app.models import User
from app.security import hash_password
from app.store import make_store
from app.store.interface import Store

PASSWORD = "demo-password"


@dataclass(frozen=True)
class Person:
    key: str
    email: str
    name: str


ADA = Person("ada", "ada@example.com", "Ada Lindqvist")
BEN = Person("ben", "ben@example.com", "Ben Okafor")
CHLOE = Person("chloe", "chloe@example.com", "Chloé Durand")
DEV = Person("dev", "dev@example.com", "Dev Raman")
PEOPLE = (ADA, BEN, CHLOE, DEV)

# Team name -> (creator and first admin, [(member, role)]).
# Ben is in both teams: a plain member of Platform and an admin of Spring Launch.
# Ada is only in Platform; Chloé and Dev only in Spring Launch.
TEAMS: dict[str, tuple[Person, list[tuple[Person, str]]]] = {
    "Platform": (ADA, [(BEN, "member")]),
    "Spring Launch": (BEN, [(CHLOE, "member"), (DEV, "member")]),
}

# Notes per team, oldest first: (author, title, body).
NOTES: dict[str, list[tuple[Person, str, str]]] = {
    "Platform": [
        (
            ADA,
            "Postgres migration plan",
            """Goal: move Team Notes from a single SQLite file to Postgres without changing the API.

Why now
The service runs as one process with one SQLite file. That is fine for a team of teams, but backups are
a file copy, a second app server is impossible, and every write waits for the single write lock.

What changes
1. Storage sits behind one interface (app/store). Routers never see SQL.
2. The SQLite and Postgres stores share every statement except four hooks: engine setup, case folding
   in search, the lock that makes count-based guards atomic, and which errors mean "busy".
3. The member cap, note cap and last-admin rule count other rows. SQLite is safe because it has one
   writer. Postgres at READ COMMITTED is not: two transactions can both pass the count. The Postgres
   store locks the team row first, so guarded writes to one team take turns.

What does not change
Wire format, status codes, lock leases, the long poll. One documented difference: search folds case
with Unicode case folding on SQLite and with lowercasing on Postgres, so the German sharp s matches
a double s only on SQLite.

Rollout
Run the whole test suite against both. Export with the seed-style script, import, flip DATABASE_URL.
Keep the SQLite file for a week as the rollback.""",
        ),
        (
            BEN,
            "Incident 2026-11-03: slow logins",
            """Summary
Between 09:12 and 09:40 logins took 2 to 6 seconds. No data was lost.

Timeline
09:12 Alerts: p95 login latency above 1 s.
09:18 Found 40 worker threads busy hashing passwords; a script was retrying logins in a tight loop.
09:25 Blocked the script's address at the proxy.
09:40 Latency back to normal.

Root cause
Password hashing is deliberately slow (about 50 ms of CPU and 64 MiB per attempt). Unlimited login
attempts from one client can occupy every worker thread.

Follow-ups
- Rate-limit login attempts per address (owner: Ben).
- Alert on worker thread saturation, not only on latency (owner: Ada).
- Document that an unknown email costs the same hashing work as a wrong password, on purpose.""",
        ),
        (ADA, "On-call handover", "This week: Ada. Next week: Ben.\nPager rota is in the shared calendar."),
        (
            BEN,
            "Offsite at CAFÉ MÖSL",
            "Booked the back room at CAFÉ MÖSL for Thursday 14:00.\nWifi password is on the receipt. Bring adapters.",
        ),
        (
            ADA,
            "Glossary",
            (
                "SLO: 99.9% of requests under 300 ms, monthly.\n100% means every request, which no one promises.\n"
                "tag: a note's version and lock holder, used by the long poll.\nsnake_case: how our JSON keys look."
            ),
        ),
        (
            BEN,
            "Reading list",
            "Designing Data-Intensive Applications, chapter 7 (transactions).\nThe Postgres docs on MVCC.",
        ),
        (
            ADA,
            "Retro draft (edit together)",
            "Went well:\n- \n\nCould be better:\n- \n\nTry next sprint:\n- ",
        ),
    ],
    "Spring Launch": [
        (
            BEN,
            "Launch timeline",
            "Week 1: menu final, photos.\nWeek 2: supplier orders, staff tasting.\nWeek 3: soft open.\nWeek 4: launch.",
        ),
        (
            CHLOE,
            "Café noir tasting notes",
            """Roast A (Ethiopia, washed): bright, citrus, a little thin as an espresso, lovely as filter.
Roast B (Brazil, natural): chocolate and nuts, heavy body, the safe choice for milk drinks.
Roast C (blend 60/40): best of both in the café noir. Slightly bitter at 30 s; pull at 27 s.

Decision: blend for espresso, Roast A as the guest filter. Revisit after two weeks of sales.""",
        ),
        (
            DEV,
            "Supplier contacts",
            "Beans: orders@roastery.example.com\nMilk: dairy@farm.example.com\nCups and lids: sales@packaging.example.com",
        ),
        (
            CHLOE,
            "Menu draft (edit together)",
            "Espresso 2.80\nCafé crème 3.60\nFlat white 3.90\nFilter (guest roast) 3.40\nHot chocolate 3.50",
        ),
        (
            BEN,
            "Press release draft",
            """FOR RELEASE ON LAUNCH DAY

A new neighbourhood café opens its doors this spring with a short menu and long opening hours.

The team spent a month tasting roasts with local suppliers before settling on a house blend for
espresso and a rotating single origin for filter. "We wanted a menu you can read in ten seconds,"
says the head barista. "Every drink on it is one we would order ourselves."

The café opens at 07:00 every day, with seating for thirty and a quiet room upstairs that teams can
book for meetings. Opening week brings a free filter coffee with any pastry.

Contact: press@cafe.example.com""",
        ),
        (
            DEV,
            "Pop-up at Hauptstraße 12",
            "Weekend pop-up in Berlin to test the menu.\nBring the small grinder and two kilos of the house blend.",
        ),
        (DEV, "Budget", "Fit-out 60%, equipment 25%, first stock 10%, contingency 5%.\nTotal: 100% of what we have."),
        (CHLOE, "Open questions", "Oat milk at no extra charge?\nDo we take cash?\nWho closes on Sundays?"),
    ],
}


def seed(store: Store) -> str:
    """Create the demo world, or explain why not. Returns a one-line outcome."""
    store.create_schema()
    existing = [person for person in PEOPLE if store.user_by_email(person.email) is not None]
    if len(existing) == len(PEOPLE):
        return "Already seeded: every demo account exists. Nothing changed."
    if existing:
        raise SystemExit(
            f"Refusing to seed: {', '.join(p.email for p in existing)} already exist(s) but not every demo account does. "
            "Seed an empty database."
        )

    now = clock.now()
    users: dict[str, User] = {}
    for person in PEOPLE:
        users[person.key] = store.create_user(person.email, person.name, hash_password(PASSWORD), now)

    team_ids: dict[str, str] = {}
    for team_name, (creator, members) in TEAMS.items():
        team = store.create_team(team_name, users[creator.key].id, now)
        team_ids[team_name] = team.id
        for member, role in members:
            store.add_member_if_room(team.id, member.email, role, now)

    # Spread the notes over the last few days, so the list (newest edit first) has an order to show.
    note_ids: dict[tuple[str, str], str] = {}
    for team_name, notes in NOTES.items():
        for age, (author, title, body) in enumerate(reversed(notes)):
            created = now - timedelta(days=3, hours=age * 7)
            note = store.create_note_if_room(team_ids[team_name], users[author.key].id, title, body, created)
            note_ids[(team_name, title)] = note.id

    # Ada edits the handover twice the way the editor does (lock, save, save, release),
    # so it is the newest note and shows version 3.
    team_id, note_id, ada = team_ids["Platform"], note_ids[("Platform", "On-call handover")], users[ADA.key].id
    edited = clock.now()
    store.take_lock(team_id, note_id, ada, edited, edited + config.LOCK_LEASE, renew_only=False, active=True)
    body = store.note(team_id, note_id).body + "\nBen swaps with Ada on 1 December."
    store.save_note(team_id, note_id, ada, 1, None, body, edited)
    store.save_note(team_id, note_id, ada, 2, "On-call handover (updated)", None, edited)
    store.release_lock(team_id, note_id, ada, is_admin=False, now=edited)

    note_count = sum(len(notes) for notes in NOTES.values())
    return f"Seeded {len(PEOPLE)} users, {len(TEAMS)} teams and {note_count} notes. Password for everyone: {PASSWORD}"


def main(argv: list[str]) -> None:
    if argv not in ([], ["--if-empty"]):
        raise SystemExit("usage: python -m app.seed [--if-empty]")
    store = make_store(config.database_url(), pool_size=1)
    try:
        store.create_schema()
        if "--if-empty" in argv and store.has_users():
            print("Not seeding: the database already has accounts.")
            return
        print(seed(store))
    finally:
        store.close()


if __name__ == "__main__":
    main(sys.argv[1:])
