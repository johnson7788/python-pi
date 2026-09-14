"""Accounts, cookie sessions, and the FastAPI dependencies that enforce them.

Four choices, each one deliberate:

**scrypt, not bcrypt or passlib.**  The repository's runtime dependencies are two
packages; ``hashlib.scrypt`` is a memory-hard KDF in the standard library, so storing
credentials costs no new dependency.

**Server-side sessions, not JWTs.**  The token is random and only its SHA-256 is stored,
so disabling an account or forcing a logout takes effect on the next request.  A JWT
would need a revocation list to do that — which is the table we would have written
anyway, minus the signing key to leak.

**The browser authenticates with a cookie.**  The WebSocket handshake is a same-origin
request, so the cookie rides along and no credential ever appears in a query string,
where it would land in access logs and browser history.

**A missing account still pays for a hash.**  ``authenticate`` verifies against a dummy
hash when the username does not exist, so response time does not reveal which login
names are real.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import COOKIE_NAME, ROLES, ServerConfig
from .db import Database, utcnow

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

#: Refuse to honour an absurd cost factor read out of a tampered row.
_MAX_SCRYPT_N = 2**20

#: How stale ``last_seen_at`` may get before a request rewrites it.  Without this,
#: every asset request would be a database write.
_TOUCH_INTERVAL_SECONDS = 300

ADMIN_ROLES = frozenset({"admin", "superadmin"})

_token_missing = object()


# --------------------------------------------------------------------------------------
# Password and token primitives
# --------------------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def hash_password(password: str) -> str:
    """Hash a password as ``scrypt$n$r$p$salt$hash`` (self-describing, upgradeable)."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "$".join(
        ["scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), _b64(salt), _b64(derived)]
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification.  Any malformed input is simply a failed login."""
    if not password or not isinstance(encoded, str):
        return False
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = _unb64(parts[4])
        expected = _unb64(parts[5])
    except (ValueError, TypeError):
        return False
    if not 1 < n <= _MAX_SCRYPT_N or r < 1 or p < 1 or not expected:
        return False
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


_dummy_hash: str | None = None


def _dummy() -> str:
    """A hash to verify against when the account does not exist (constant work)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    return _dummy_hash


def new_session_token() -> str:
    """A fresh cookie value.  Only its digest is ever persisted."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthUser:
    """The authenticated caller.  Never carries the password hash."""

    uid: str
    username: str
    display_name: str
    role: str
    must_change_password: bool = False
    monthly_token_quota: int | None = None
    monthly_cost_quota: float | None = None
    #: Digest of the session this request arrived on, when there was one.
    token_hash: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "username": self.username,
            "displayName": self.display_name,
            "role": self.role,
            "isAdmin": self.is_admin,
            "mustChangePassword": self.must_change_password,
            "monthlyTokenQuota": self.monthly_token_quota,
            "monthlyCostQuota": self.monthly_cost_quota,
        }


def user_from_row(row: Any, token_hash: str | None = None) -> AuthUser:
    return AuthUser(
        uid=row["uid"],
        username=row["username"],
        display_name=row["display_name"] or row["username"],
        role=row["role"],
        must_change_password=bool(row["must_change_password"]),
        monthly_token_quota=row["monthly_token_quota"],
        monthly_cost_quota=row["monthly_cost_quota"],
        token_hash=token_hash,
    )


def session_expiry(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _needs_touch(last_seen_at: str | None) -> bool:
    if not last_seen_at:
        return True
    try:
        seen = datetime.fromisoformat(last_seen_at)
    except ValueError:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    return age >= _TOUCH_INTERVAL_SECONDS


def resolve_session(db: Database, token: str | None) -> AuthUser | None:
    """Turn a cookie value into an :class:`AuthUser`, or ``None`` if it is not live."""
    if not token:
        return None
    token_hash = hash_token(token)
    row = db.get_auth_session(token_hash)
    if row is None:
        return None
    if _needs_touch(row["last_seen_at"]):
        db.touch_auth_session(token_hash, last_seen_at=utcnow())
    return user_from_row(row, token_hash)


def authenticate(db: Database, username: str, password: str) -> AuthUser | None:
    """Check credentials.  Returns ``None`` for unknown, disabled or wrong-password accounts."""
    row = db.get_user_by_username(username)
    if row is None:
        verify_password(password, _dummy())  # equalise the work done for unknown users
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    if row["status"] != "active":
        return None
    return user_from_row(row)


def start_session(
    db: Database, user: AuthUser, config: ServerConfig, *, user_agent: str = ""
) -> str:
    """Issue a cookie value for ``user`` and record the login."""
    token = new_session_token()
    db.create_auth_session(
        token_hash=hash_token(token),
        uid=user.uid,
        expires_at=session_expiry(config.session_ttl_hours),
        user_agent=user_agent,
    )
    db.update_user(user.uid, last_login_at=utcnow())
    return token


def ensure_admin(
    db: Database, username: str, password: str | None = None
) -> tuple[str, str] | None:
    """Create the first superadmin if the database has no accounts.

    Returns ``(username, password)`` when an account was created so the caller can
    print the one-time credential, and ``None`` when accounts already exist.  A
    generated password must be changed at first login; a caller-supplied one is
    assumed to come from a secret store and is not forced.
    """
    if db.count_users() > 0:
        return None
    generated = password or secrets.token_urlsafe(12)
    db.create_user(
        username=username,
        password_hash=hash_password(generated),
        display_name=username,
        role="superadmin",
        must_change_password=password is None,
    )
    return username, generated


def set_cookie(response: JSONResponse, token: str, config: ServerConfig) -> None:
    """Attach the session cookie with the attributes a browser session needs."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=config.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
        path="/",
    )


def clear_cookie(response: JSONResponse) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


# --------------------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------------------


def get_config(request: Request) -> ServerConfig:
    return request.app.state.server_config


def get_database(request: Request) -> Database:
    return request.app.state.db


def current_user(request: Request) -> AuthUser | None:
    """Resolve the caller once per request, caching the answer on ``request.state``."""
    cached = getattr(request.state, "auth_user", _token_missing)
    if cached is not _token_missing:
        return cached
    db = getattr(request.app.state, "db", None)
    user = resolve_session(db, request.cookies.get(COOKIE_NAME)) if db is not None else None
    request.state.auth_user = user
    return user


def reject_unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "unauthenticated"}
    )


async def get_current_user(request: Request) -> AuthUser | None:
    return current_user(request)


async def get_required_user(request: Request) -> AuthUser:
    user = current_user(request)
    if user is None:
        raise reject_unauthenticated()
    return user


async def get_admin_user(user: AuthUser = Depends(get_required_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "forbidden"})
    return user


async def get_superadmin_user(user: AuthUser = Depends(get_required_user)) -> AuthUser:
    if not user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "forbidden"})
    return user


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": f"unknown role: {role}"},
        )
    return role


__all__ = [
    "ADMIN_ROLES",
    "AuthUser",
    "authenticate",
    "clear_cookie",
    "current_user",
    "ensure_admin",
    "get_admin_user",
    "get_config",
    "get_current_user",
    "get_database",
    "get_required_user",
    "get_superadmin_user",
    "hash_password",
    "hash_token",
    "new_session_token",
    "resolve_session",
    "reject_unauthenticated",
    "session_expiry",
    "set_cookie",
    "start_session",
    "user_from_row",
    "validate_role",
    "verify_password",
]
