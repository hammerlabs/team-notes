"""The demo data in `docs/SEED_DATA.md`: what it creates, and that re-running it is safe.

Also the `--if-empty` start-up path that `scripts/run.sh` and the Docker entrypoint run on every start. Runs on both
databases, so the accented search the seed demonstrates is checked on each.
"""

from typing import Any

import pytest
from fastapi import FastAPI

from app import clock, seed
from helpers import Api


def login(api: Api, person: seed.Person) -> dict[str, str]:
    """Log in as a seeded person and return their auth headers."""
    response = api.client.post("/auth/login", json={"email": person.email, "password": seed.PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def teams(api: Api, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The caller's teams, keyed by name."""
    return {team["name"]: team for team in api.client.get("/teams", headers=headers).json()}


def titles(api: Api, headers: dict[str, str], team_id: str, q: str | None = None) -> list[str]:
    """Titles of a team's notes in list order, optionally searched with `q`."""
    params = {"q": q} if q else {}
    page = api.client.get(f"/teams/{team_id}/notes", params=params, headers=headers).json()
    return [note["title"] for note in page["items"]]


def test_seed_builds_the_documented_world(app: FastAPI, api: Api) -> None:
    """The seed creates the users, teams, roles and note counts the seed document describes.

    Also the edited handover note at version 3 and unlocked, the isolation between the two teams, and an accented
    search. If the document and the seed drift apart, this fails.
    """
    assert seed.seed(app.state.store).startswith("Seeded 4 users, 2 teams and 15 notes")

    ada, ben, chloe = login(api, seed.ADA), login(api, seed.BEN), login(api, seed.CHLOE)
    ada_teams, ben_teams = teams(api, ada), teams(api, ben)
    assert {name: t["role"] for name, t in ada_teams.items()} == {"Platform": "admin"}
    assert {name: t["role"] for name, t in ben_teams.items()} == {"Platform": "member", "Spring Launch": "admin"}
    assert set(teams(api, chloe)) == {"Spring Launch"}

    platform, launch = ben_teams["Platform"]["id"], ben_teams["Spring Launch"]["id"]
    platform_titles = titles(api, ben, platform)
    assert len(platform_titles) == 7 and len(titles(api, ben, launch)) == 8
    # The edited note is the newest, at version 3.
    assert platform_titles[0] == "On-call handover (updated)"
    handover = api.client.get(f"/teams/{platform}/notes", headers=ben).json()["items"][0]
    assert handover["version"] == 3 and handover["lock"] is None
    # Isolation: Ada is not in Spring Launch.
    assert api.client.get(f"/teams/{launch}/notes", headers=ada).status_code == 404
    # Search folds the accent's case on both databases.
    assert "Offsite at CAFÉ MÖSL" in titles(api, ada, platform, q="café")


def test_seed_twice_changes_nothing(app: FastAPI, api: Api) -> None:
    """Running the seed again reports "Already seeded" and adds nothing."""
    seed.seed(app.state.store)
    assert seed.seed(app.state.store).startswith("Already seeded")
    assert len(titles(api, login(api, seed.ADA), teams(api, login(api, seed.ADA))["Platform"]["id"])) == 7


def test_seed_refuses_a_database_with_only_some_demo_accounts(app: FastAPI, api: Api) -> None:
    """A database with some but not all demo accounts makes the seed exit with an error.

    It does not add the missing ones on top.
    """
    app.state.store.create_user(seed.ADA.email, seed.ADA.name, "-", clock.now())
    with pytest.raises(SystemExit, match="Refusing to seed"):
        seed.seed(app.state.store)


def test_seed_if_empty_seeds_an_empty_database_once(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`scripts/run.sh` and the Docker image run `--if-empty` on every start.

    It seeds an empty database on the first start and does nothing on the next.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    seed.main(["--if-empty"])
    assert capsys.readouterr().out.startswith("Seeded 4 users")
    seed.main(["--if-empty"])
    assert capsys.readouterr().out.startswith("Not seeding")


def test_seed_if_empty_leaves_a_database_with_accounts_alone(
    app: FastAPI, api: Api, database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Demo data never lands among real sign-ups, and a start never fails on a half-seeded database."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    api.signup("alice")
    seed.main(["--if-empty"])
    assert capsys.readouterr().out.startswith("Not seeding")
    assert app.state.store.user_by_email(seed.ADA.email) is None
