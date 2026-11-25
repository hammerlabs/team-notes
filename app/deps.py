"""FastAPI dependencies: who is calling, and are they allowed in this team.

Team isolation rests on `team_router()`. Every router for paths under
`/teams/{team_id}` is built by it, so `require_member` runs on every such
endpoint. A route added any other way that names a team or a note fails
`tests/test_isolation.py::test_every_team_route_is_guarded`.

Storage is reached only through `StoreDep` (app/store). Each store call is
its own short unit of work, so the User and Membership these dependencies
return are plain loaded values: nothing on them touches the database again.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import clock
from app.errors import ApiError, error_responses, forbidden, not_found
from app.models import Membership, User
from app.notify import Notifier
from app.security import hash_token
from app.store.interface import Store


async def get_store(request: Request) -> Store:
    """The app's store. `async` so FastAPI calls it on the event loop instead of spending a thread hop on it."""
    return request.app.state.store


StoreDep = Annotated[Store, Depends(get_store)]

# auto_error=False so a missing token gets our error shape, not FastAPI's.
_bearer = HTTPBearer(auto_error=False)


def current_user(
    store: StoreDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """The user behind the bearer token. Identity comes only from here."""
    if credentials is None:
        raise _unauthorized("Missing bearer token.")
    user = store.user_for_token(hash_token(credentials.credentials), clock.now())
    if user is None:
        raise _unauthorized("Invalid or expired token.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_member(team_id: str, user: CurrentUser, store: StoreDep) -> Membership:
    """The caller's membership in `team_id`, or 404 for anyone outside it.

    404 rather than 403 so outsiders cannot tell whether a team exists. Read
    from the database on every request and never cached, so a removed member
    is refused on their very next request.
    """
    membership = store.membership(team_id, user.id)
    if membership is None:
        raise not_found("Team")
    return membership


Member = Annotated[Membership, Depends(require_member)]


def require_admin(membership: Member) -> Membership:
    """The caller's membership, if they are an admin of the team; else 403."""
    if not membership.is_admin:
        raise forbidden("Only a team admin can do this.")
    return membership


Admin = Annotated[Membership, Depends(require_admin)]


def team_router(tag: str) -> APIRouter:
    """A router for paths under /teams/{team_id}, guarded by `require_member`.

    Every route on it can answer 401 (no or bad token), 404 (not a member,
    or no such team), 422 (bad parameters) and 503 (database busy).
    """
    return APIRouter(
        prefix="/teams/{team_id}",
        tags=[tag],
        dependencies=[Depends(require_member)],
        responses=error_responses(401, 404, 422, 503),
    )


async def get_notifier(request: Request) -> Notifier:
    """The app's in-memory notifier for long polls. `async`: no thread hop for a lookup."""
    return request.app.state.notifier


NotifierDep = Annotated[Notifier, Depends(get_notifier)]


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})
