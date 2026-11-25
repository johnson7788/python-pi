"""Account and quota routes.

Split from ``app.py`` as the first router module.  Handlers receive the two helpers
they need as parameters instead of closing over the factory's locals, which is what
makes the module importable and the dependencies visible in the signature.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pi_server import auth
from pi_server.config import COOKIE_NAME
from pi_server.db import DuplicateUsername, month_start
from pi_server.errors import error
from pi_server.schemas import (
    CreateUserRequest,
    LoginRequest,
    PasswordChangeRequest,
    RegisterRequest,
    UpdateUserRequest,
)
from pi_server.support import user_row_to_json

from pi_server.constants import MIN_PASSWORD_LENGTH


def register(
    app: FastAPI,
    *,
    require_team_mode: Callable[[], Any],
    ensure_default_workspace: Callable[[str], None],
    server_config: Any,
) -> None:
    # -- accounts ----------------------------------------------------------------------

    @app.post("/api/auth/login")
    async def login(payload: LoginRequest, request: Request) -> JSONResponse:
        db = require_team_mode()
        user = auth.authenticate(db, payload.username, payload.password)
        if user is None:
            # One message for every failure mode: never reveal whether a name exists.
            raise error(401, "invalid username or password")
        token = auth.start_session(
            db, user, server_config, user_agent=request.headers.get("user-agent", "")
        )
        response = JSONResponse({"user": user.to_json()})
        auth.set_cookie(response, token, server_config)
        return response

    @app.post("/api/auth/register", status_code=201)
    async def register(payload: RegisterRequest) -> dict[str, Any]:
        """Open LAN registration: a new member creates their own account.

        Registration is intentionally available only in team mode.  It creates a
        member (never an administrator), and the chosen password is final — the
        user is not forced to change it again.
        """
        db = require_team_mode()
        username = payload.username.strip()
        display_name = payload.displayName.strip() or username
        if not 2 <= len(username) <= 64 or any(char.isspace() for char in username):
            raise error(422, "username must be 2-64 characters without spaces")
        if len(payload.password) < MIN_PASSWORD_LENGTH:
            raise error(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        try:
            uid = db.create_user(
                username=username,
                password_hash=auth.hash_password(payload.password),
                display_name=display_name,
                role="user",
                must_change_password=False,
            )
        except DuplicateUsername:
            raise error(409, "username already exists") from None
        ensure_default_workspace(uid)
        created = db.get_user(uid)
        assert created is not None
        return {"user": user_row_to_json(created)}

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        db = require_team_mode()
        token = request.cookies.get(COOKIE_NAME)
        if token:
            db.revoke_auth_session(auth.hash_token(token))
        response = JSONResponse({"ok": True})
        auth.clear_cookie(response)
        return response

    @app.get("/api/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        user = auth.current_user(request)
        return {
            "authMode": server_config.auth_mode,
            "user": user.to_json() if user is not None else None,
        }

    @app.post("/api/auth/password")
    async def change_password(
        payload: PasswordChangeRequest,
        user: auth.AuthUser = Depends(auth.get_required_user),
    ) -> JSONResponse:
        """Change your own password.  Other devices are signed out; this one is not."""
        db = require_team_mode()
        row = db.get_user(user.uid)
        if row is None:
            raise error(404, "account not found")
        if not auth.verify_password(payload.currentPassword, row["password_hash"]):
            raise error(403, "current password is incorrect")
        if len(payload.newPassword) < MIN_PASSWORD_LENGTH:
            raise error(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        db.set_password(user.uid, auth.hash_password(payload.newPassword), must_change=False)
        db.revoke_user_sessions(user.uid)
        token = auth.start_session(db, user, server_config)
        refreshed = db.get_user(user.uid)
        response = JSONResponse(
            {"user": user_row_to_json(refreshed) if refreshed is not None else user.to_json()}
        )
        auth.set_cookie(response, token, server_config)
        return response

    # -- administration ----------------------------------------------------------------

    @app.get("/api/admin/users")
    async def admin_list_users(
        _actor: auth.AuthUser = Depends(auth.get_admin_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        return {"users": [user_row_to_json(row) for row in db.list_users()]}

    @app.post("/api/admin/users")
    async def admin_create_user(
        payload: CreateUserRequest,
        actor: auth.AuthUser = Depends(auth.get_admin_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        auth.validate_role(payload.role)
        if payload.role != "user" and not actor.is_superadmin:
            raise error(403, "only a superadmin can create administrators")
        if len(payload.password) < MIN_PASSWORD_LENGTH:
            raise error(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        try:
            uid = db.create_user(
                username=payload.username,
                password_hash=auth.hash_password(payload.password),
                display_name=payload.displayName or payload.username,
                role=payload.role,
                must_change_password=payload.mustChangePassword,
                monthly_token_quota=payload.monthlyTokenQuota,
                monthly_cost_quota=payload.monthlyCostQuota,
            )
        except DuplicateUsername:
            raise error(409, f"username already exists: {payload.username}") from None
        created = db.get_user(uid)
        assert created is not None
        return {"user": user_row_to_json(created)}

    @app.patch("/api/admin/users/{uid}")
    async def admin_update_user(
        uid: str,
        payload: UpdateUserRequest,
        actor: auth.AuthUser = Depends(auth.get_admin_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        target = db.get_user(uid)
        if target is None:
            raise error(404, "account not found")

        sent = payload.model_dump(exclude_unset=True)
        if "role" in sent:
            auth.validate_role(sent["role"])
            if not actor.is_superadmin:
                raise error(403, "only a superadmin can change roles")
        if "status" in sent and sent["status"] not in {"active", "disabled"}:
            raise error(422, "status must be active or disabled")
        if uid == actor.uid and ("role" in sent or "status" in sent):
            raise error(400, "you cannot change your own role or status")
        if len(sent.get("password") or "") and len(sent["password"]) < MIN_PASSWORD_LENGTH:
            raise error(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        # Never let the last usable administrator lock everyone out.
        removes_admin = sent.get("status") == "disabled" or sent.get("role") == "user"
        if target["role"] in auth.ADMIN_ROLES and target["status"] == "active" and removes_admin:
            if db.count_active_admins() <= 1:
                raise error(400, "the last active administrator cannot be demoted or disabled")

        fields: dict[str, Any] = {}
        if "displayName" in sent:
            fields["display_name"] = sent["displayName"] or target["username"]
        if "role" in sent:
            fields["role"] = sent["role"]
        if "status" in sent:
            fields["status"] = sent["status"]
        if "monthlyTokenQuota" in sent:
            fields["monthly_token_quota"] = sent["monthlyTokenQuota"]
        if "monthlyCostQuota" in sent:
            fields["monthly_cost_quota"] = sent["monthlyCostQuota"]
        if "mustChangePassword" in sent:
            fields["must_change_password"] = bool(sent["mustChangePassword"])
        if "password" in sent:
            fields["password_hash"] = auth.hash_password(sent["password"])
            fields["must_change_password"] = bool(sent.get("mustChangePassword", True))
        if fields:
            db.update_user(uid, **fields)

        # A credential or account change must invalidate existing browser sessions.
        if "password" in sent or sent.get("status") == "disabled":
            db.revoke_user_sessions(uid)

        updated = db.get_user(uid)
        assert updated is not None
        return {"user": user_row_to_json(updated)}

    # -- usage and quota ---------------------------------------------------------------

    @app.get("/api/usage/me")
    async def usage_me(
        user: auth.AuthUser = Depends(auth.get_required_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        since = month_start()
        row = db.get_user(user.uid)
        return {
            "windowStart": since,
            "summary": db.usage_summary(user.uid, since),
            "byDay": db.usage_by_day(user.uid, since),
            "byKind": db.usage_by_kind(user.uid, since),
            "bySession": db.usage_by_session(user.uid, since),
            "quota": {
                "tokens": row["monthly_token_quota"] if row is not None else None,
                "cost": row["monthly_cost_quota"] if row is not None else None,
            },
        }

    @app.get("/api/admin/usage")
    async def admin_usage(
        _actor: auth.AuthUser = Depends(auth.get_admin_user),
    ) -> dict[str, Any]:
        db = require_team_mode()
        since = month_start()
        return {"windowStart": since, "users": db.usage_all_users(since)}

    @app.get("/api/admin/audit")
    async def admin_audit(
        _actor: auth.AuthUser = Depends(auth.get_admin_user),
        uid: str | None = Query(None, description="Restrict to one account"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        db = require_team_mode()
        return {"records": db.query_audit(uid=uid, limit=limit, offset=offset)}
