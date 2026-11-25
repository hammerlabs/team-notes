"""Password hashing and bearer tokens.

Passwords are hashed with argon2. Tokens are random strings handed to the
client once; only their SHA-256 is stored, so a leaked database does not leak
usable tokens. A fast hash is enough for tokens because they carry 256 bits
of randomness and cannot be guessed.
"""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Module-level so the test suite can swap in cheap parameters (see conftest).
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an argon2 hash of `password`."""
    return _hasher.hash(password)


# Verified against when the account does not exist, so an unknown email costs
# the same argon2 work as a wrong password and login time does not reveal
# which addresses are registered (measured before: 48 ms known, 1.4 ms unknown).
_DUMMY_HASH = _hasher.hash("no account has this password")


def verify_password(password_hash: str | None, password: str) -> bool:
    """True if `password` matches `password_hash`. None (no such user) is always False, at the same cost."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


def new_token() -> str:
    """A fresh random bearer token (43 URL-safe characters)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The value stored in `sessions.token_hash` for `token`."""
    return hashlib.sha256(token.encode()).hexdigest()
