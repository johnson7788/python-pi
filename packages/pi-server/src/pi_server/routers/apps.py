"""Team apps: the catalog, their audit trail and per-app overrides.

Split from ``app.py``.  The helpers that read and diff an app travel with the routes:
``app_by_id`` resolves a catalog row, ``app_diff`` is what the audit page renders, and
``normalize_app`` is the single place a submitted app body is made safe.

Dependencies that stay in the factory are passed explicitly: the database accessor,
the registry (for cache eviction), and the catalog functions the modes resolve
through.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from pi_server import apps as app_catalog
from pi_server import auth
from pi_server.errors import error
from pi_server.schemas import AppUpsertRequest


def register(
    app: FastAPI,
    *,
    database: Any,
    server_config: Any,
    registry: Any,
    require_team_mode: Callable[[], Any],
    ensure_apps: Callable[[], None],
    all_apps: Callable[[], list[dict[str, Any]]],
    normalize_app: Callable[..., dict[str, Any]],
    safe_id: Callable[[Any, str], str],
) -> None:
    def _loads_or_none(raw: Any) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None

    def app_by_id(app_id: str) -> dict[str, Any] | None:
        return next((item for item in all_apps() if item["id"] == app_id), None)

    def require_app_admin(request: Request) -> Any:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise error(403, "only administrators can manage apps")
        return user

    def app_name_taken(name: str, *, exclude: str | None = None) -> bool:
        needle = name.strip().lower()
        return any(
            item["id"] != exclude and str(item["name"]).strip().lower() == needle
            for item in all_apps()
        )

    def app_diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
        """Human-readable change list; the audit table stores whole snapshots."""
        if not before or not after:
            return []
        changes: list[str] = []
        for key, label in (("name", "名称"), ("description", "描述"), ("accent", "标识色")):
            if str(before.get(key) or "") != str(after.get(key) or ""):
                changes.append(
                    f"{label}：{before.get(key) or '(空)'} → {after.get(key) or '(空)'}"
                )
        old_modes = {str(m.get("id")): m for m in before.get("modes") or []}
        new_modes = {str(m.get("id")): m for m in after.get("modes") or []}
        added = [str(m.get("name") or m.get("id")) for mid, m in new_modes.items() if mid not in old_modes]
        removed = [str(m.get("name") or m.get("id")) for mid, m in old_modes.items() if mid not in new_modes]
        if added:
            changes.append("新增模式：" + "、".join(added))
        if removed:
            changes.append("删除模式：" + "、".join(removed))
        for mid, mode in new_modes.items():
            old = old_modes.get(mid)
            if old and str(old.get("prompt") or "") != str(mode.get("prompt") or ""):
                changes.append(f"模式「{mode.get('name') or mid}」的提示词已修改")
        return changes or ["没有字段变化"]

    @app.get("/api/apps")
    async def list_apps(request: Request) -> dict[str, Any]:
        if database is None:
            catalog = app_catalog.catalog_json()
            return {**catalog, "canManage": False}
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        apps = all_apps()
        first = apps[0] if apps else None
        default_app = (
            app_catalog.DEFAULT_APP_ID
            if any(app["id"] == app_catalog.DEFAULT_APP_ID for app in apps)
            else (first["id"] if first else "")
        )
        default_mode = ""
        if first is not None:
            chosen = next((app for app in apps if app["id"] == default_app), first)
            default_mode = chosen["modes"][0]["id"] if chosen["modes"] else ""
        return {
            "apps": apps,
            "defaultApp": default_app,
            "defaultMode": default_mode,
            "canManage": bool(user.is_admin),
        }

    @app.post("/api/apps", status_code=201)
    async def create_app_endpoint(payload: AppUpsertRequest, request: Request) -> dict[str, Any]:
        user = require_app_admin(request)
        app_id = f"app-{uuid.uuid4().hex[:8]}"
        app = normalize_app(payload, app_id)
        if app_name_taken(app["name"]):
            raise error(409, f"an app named {app['name']!r} already exists")
        db = require_team_mode()
        db.replace_team_app(app)
        created = app_by_id(app_id) or app
        db.record_team_app_audit(
            user.uid, user.display_name or user.username, app_id, "create", None, created
        )
        return created

    @app.put("/api/apps/{app_id}")
    async def update_app(app_id: str, payload: AppUpsertRequest, request: Request) -> dict[str, Any]:
        user = require_app_admin(request)
        existing = app_by_id(app_id)
        if existing is None:
            raise error(404, f"unknown app: {app_id}")
        app = normalize_app(payload, app_id)
        if app_name_taken(app["name"], exclude=app_id):
            raise error(409, f"an app named {app['name']!r} already exists")
        db = require_team_mode()
        db.replace_team_app(app, builtin=bool(existing.get("builtin")))
        updated = app_by_id(app_id) or app
        db.record_team_app_audit(
            user.uid,
            user.display_name or user.username,
            app_id,
            "update",
            existing,
            updated,
        )
        await registry.evict_app(app_id)
        return updated

    @app.delete("/api/apps/{app_id}")
    async def delete_app(app_id: str, request: Request) -> dict[str, Any]:
        user = require_app_admin(request)
        existing = app_by_id(app_id)
        if existing is None:
            raise error(404, f"unknown app: {app_id}")
        if existing.get("builtin"):
            raise error(400, "built-in apps cannot be deleted")
        db = require_team_mode()
        references = db.app_references(app_id)
        if references["schedules"]:
            raise error(409, "this app is still used by scheduled tasks; move or delete them first", details={"schedules": references["schedules"]})
        db.delete_team_app(app_id)
        db.delete_app_references(app_id)
        db.record_team_app_audit(
            user.uid, user.display_name or user.username, app_id, "delete", existing, None
        )
        await registry.evict_app(app_id)
        return {"deleted": app_id, "cleaned": references}

    @app.post("/api/apps/{app_id}/restore")
    async def restore_app(app_id: str, request: Request) -> dict[str, Any]:
        user = require_app_admin(request)
        existing = app_by_id(app_id)
        if existing is None:
            raise error(404, f"unknown app: {app_id}")
        if not existing.get("builtin"):
            raise error(400, "only built-in apps can be restored")
        default = next(
            (item for item in app_catalog.catalog_json()["apps"] if item["id"] == app_id), None
        )
        if default is None:
            raise error(404, f"no built-in default: {app_id}")
        db = require_team_mode()
        db.replace_team_app(default, builtin=True)
        restored = app_by_id(app_id) or default
        db.record_team_app_audit(
            user.uid,
            user.display_name or user.username,
            app_id,
            "restore",
            existing,
            restored,
        )
        await registry.evict_app(app_id)
        return restored

    @app.post("/api/apps/{app_id}/duplicate", status_code=201)
    async def duplicate_app(app_id: str, request: Request) -> dict[str, Any]:
        user = require_app_admin(request)
        existing = app_by_id(app_id)
        if existing is None:
            raise error(404, f"unknown app: {app_id}")
        base_name = f"{existing['name']} 副本"[:40]
        name = base_name
        suffix = 2
        while app_name_taken(name):
            name = f"{base_name[:36]} {suffix}"
            suffix += 1
        copy = json.loads(json.dumps(existing))
        new_id = f"app-{uuid.uuid4().hex[:8]}"
        copy.update({"id": new_id, "name": name, "builtin": False})
        db = require_team_mode()
        db.replace_team_app(copy)
        created = app_by_id(new_id) or copy
        db.record_team_app_audit(
            user.uid, user.display_name or user.username, new_id, "duplicate", existing, created
        )
        return created

    @app.post("/api/apps/order")
    async def reorder_apps(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        require_app_admin(request)
        ids = payload.get("ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise error(422, "ids must be a list of app ids")
        known = [item["id"] for item in all_apps()]
        if set(ids) - set(known):
            raise error(422, "ids contains an unknown app")
        ordered = list(dict.fromkeys(ids)) + [item for item in known if item not in ids]
        require_team_mode().reorder_team_apps(ordered)
        return {"ids": ordered}

    @app.get("/api/apps/export")
    async def export_apps(request: Request) -> dict[str, Any]:
        user = require_app_admin(request)
        del user
        apps = []
        for item in all_apps():
            copy = json.loads(json.dumps(item))
            copy.pop("builtin", None)
            apps.append(copy)
        return {
            "format": 1,
            "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "apps": apps,
        }

    @app.post("/api/apps/import", status_code=201)
    async def import_apps(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        user = require_app_admin(request)
        raw_apps = payload.get("apps")
        if not isinstance(raw_apps, list) or not raw_apps:
            raise error(422, "apps must be a non-empty list")
        db = require_team_mode()
        created: list[dict[str, Any]] = []
        for raw in raw_apps[:20]:
            if not isinstance(raw, dict):
                continue
            new_id = f"app-{uuid.uuid4().hex[:8]}"
            try:
                app = normalize_app(AppUpsertRequest(**raw), new_id)
            except (HTTPException, TypeError, ValueError):
                continue
            name = app["name"]
            suffix = 2
            while app_name_taken(name):
                name = f"{app['name'][:36]} {suffix}"
                suffix += 1
            app["name"] = name
            db.replace_team_app(app)
            created.append(app_by_id(new_id) or app)
            db.record_team_app_audit(
                user.uid, user.display_name or user.username, new_id, "import", None, app
            )
        if not created:
            raise error(422, "no valid apps in this file")
        return {"imported": created}

    @app.post("/api/apps/preview")
    async def preview_app(payload: AppUpsertRequest, request: Request) -> dict[str, Any]:
        require_app_admin(request)
        app = normalize_app(payload, "preview")
        modes = [
            {
                "id": mode["id"],
                "name": mode["name"],
                "prompt": mode["prompt"],
                "chars": len(mode["prompt"]),
                "skills": "all" if mode["skillBinding"] == "all" else mode["skillBinding"],
            }
            for mode in app["modes"]
        ]
        return {"app": app, "modes": modes}

    @app.get("/api/apps/{app_id}/audit")
    async def app_audit(app_id: str, request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        rows = require_team_mode().list_team_app_audit(app_id)
        if not rows and app_by_id(app_id) is None:
            raise error(404, f"unknown app: {app_id}")
        audit = []
        for row in rows:
            before = _loads_or_none(row["before_json"])
            after = _loads_or_none(row["after_json"])
            audit.append(
                {
                    "id": row["id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "createdAt": row["created_at"],
                    "changes": app_diff(before, after),
                    "before": before,
                    "after": after,
                }
            )
        return {"audit": audit}
