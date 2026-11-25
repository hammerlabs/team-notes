"""Sign up, log in, and who am I.

Deliberately minimal: login exists so the service knows who is asking. There
is no logout, refresh or password reset; a token simply expires after
SESSION_LIFETIME.
"""

from fastapi import APIRouter, Response

from app import clock, config
from app.deps import CurrentUser, StoreDep
from app.errors import ApiError, error_responses
from app.schemas import LoginIn, LoginOut, SignupIn, UserOut, normalize_email
from app.security import hash_password, hash_token, new_token, verify_password

router = APIRouter(tags=["auth"])


@router.post("/auth/signup", status_code=201, responses=error_responses(409, 413, 422, 503))
def signup(payload: SignupIn, store: StoreDep) -> UserOut:
    """Create an account. The email must not be registered yet (409 `email_taken`)."""
    # Hashed before the store is called: ~50 ms of CPU that needs no connection.
    password_hash = hash_password(payload.password)
    return UserOut.model_validate(store.create_user(payload.email, payload.name, password_hash, clock.now()))


@router.post("/auth/login", responses=error_responses(401, 413, 422, 503))
def login(payload: LoginIn, store: StoreDep, response: Response) -> LoginOut:
    """Exchange email and password for a bearer token, shown only this once."""
    # The body carries a credential; no cache or proxy may keep a copy.
    response.headers["Cache-Control"] = "no-store"
    try:
        email: str | None = normalize_email(payload.email)
    except ValueError:
        email = None  # cannot be anyone's address; refused below like an unknown one
    # Two store calls rather than one, so the ~50 ms password check between
    # them holds no connection.
    user = store.user_by_email(email) if email is not None else None
    # Always one argon2 verification, also for an unknown email (against a
    # dummy hash), so the response time does not say whether the account exists.
    if not verify_password(user.password_hash if user else None, payload.password) or user is None:
        raise ApiError(401, "invalid_credentials", "Email or password is wrong.")
    token = new_token()
    expires_at = clock.now() + config.SESSION_LIFETIME
    store.create_session(hash_token(token), user.id, expires_at)
    return LoginOut(token=token, expires_at=expires_at, user=UserOut.model_validate(user))


@router.get("/me", responses=error_responses(401, 503))
def me(user: CurrentUser) -> UserOut:
    """The caller's own account."""
    return UserOut.model_validate(user)
