"""Teams and membership.

`POST /teams` and `GET /teams` are not about one team, so they sit on a plain
router. Everything under /teams/{team_id} uses `team_router()`, which applies
the membership check.
"""

from fastapi import APIRouter, Response

from app import clock
from app.deps import Admin, CurrentUser, Member, NotifierDep, StoreDep, team_router
from app.errors import error_responses, forbidden, not_found
from app.schemas import MemberIn, MemberOut, TeamIn, TeamOut

router = APIRouter(prefix="/teams", tags=["teams"], responses=error_responses(401, 503))
one_team_router = team_router("teams")


@router.post("", status_code=201, responses=error_responses(413, 422))
def create_team(payload: TeamIn, user: CurrentUser, store: StoreDep) -> TeamOut:
    """Create a team with the caller as its admin."""
    team = store.create_team(payload.name, user.id, clock.now())
    return TeamOut(id=team.id, name=team.name, created_at=team.created_at, role="admin")


@router.get("")
def list_teams(user: CurrentUser, store: StoreDep) -> list[TeamOut]:
    """The teams the caller belongs to, oldest first, with the caller's role."""
    return [
        TeamOut(id=team.id, name=team.name, created_at=team.created_at, role=role)
        for team, role in store.teams_of(user.id)
    ]


@one_team_router.get("")
def get_team(team_id: str, membership: Member, store: StoreDep) -> TeamOut:
    """One team the caller belongs to, with the caller's role."""
    team = store.team(team_id)
    if team is None:  # cannot happen while a membership row points at it; kept for the type
        raise not_found("Team")
    return TeamOut(id=team.id, name=team.name, created_at=team.created_at, role=membership.role)


@one_team_router.get("/members")
def list_members(team_id: str, store: StoreDep) -> list[MemberOut]:
    """Everyone in the team, in the order they joined."""
    return [
        MemberOut(
            user_id=user.id,
            email=user.email,
            name=user.name,
            role=membership.role,
            joined_at=membership.created_at,
        )
        for membership, user in store.members(team_id)
    ]


@one_team_router.post("/members", status_code=201, responses=error_responses(403, 409, 413))
def add_member(team_id: str, payload: MemberIn, _admin: Admin, store: StoreDep) -> MemberOut:
    """Add an existing user, found by email. Admins only.

    404 `user_not_found`, 409 `already_member`, or 409 `team_full`; two admins
    adding at once cannot both take the last seat (see the store).
    """
    now = clock.now()
    user = store.add_member_if_room(team_id, payload.email, payload.role, now)
    return MemberOut(user_id=user.id, email=user.email, name=user.name, role=payload.role, joined_at=now)


@one_team_router.delete("/members/{user_id}", status_code=204, responses=error_responses(403, 409))
def remove_member(team_id: str, user_id: str, membership: Member, store: StoreDep, notifier: NotifierDep) -> Response:
    """Remove a member. Admins can remove anyone; anyone can remove themselves.

    The last admin cannot leave (409 `last_admin`), or the team could never
    be managed again. Any lock the member holds on the team's notes is
    released with them.
    """
    if not membership.is_admin and user_id != membership.user_id:
        raise forbidden("Only a team admin can remove other members.")
    unlocked = store.remove_member_keeping_an_admin(team_id, user_id)
    # After the commit: viewers of those notes see the lock go. Every poll
    # re-checks membership before it answers, so the removed member's polls
    # on those notes end with 404 on this wake, and their polls on other
    # notes end with 404 when the wait runs out.
    for note_id in unlocked:
        notifier.wake(note_id)
    return Response(status_code=204)
