"""Scheduled tasks and the in-app notification feed.

Split from ``app.py``.  A schedule runs the same worker a turn runs, but unattended:
no tab is guaranteed to be open, so a question it asks is refused rather than parked,
and the result is announced through the notification feed.

``schedule_uid`` / ``schedule_store`` stay in the factory because single-mode keeps a
separate database for local schedules; they are passed in explicitly.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Request
from pi_server import schedules as schedule_catalog
from pi_server.errors import error
from pi_server.schemas import CreateScheduleRequest, UpdateScheduleRequest


def register(
    app: FastAPI,
    *,
    schedule_uid: Callable[[Request], str],
    schedule_store: Callable[[], Any],
    require_team_mode: Callable[[], Any],
    registry: Any,
    resolve_workspace: Callable[[str, str | None], str],
    resolve_app_mode: Callable[[str | None, str | None], tuple[str, str]],
    schedule_runner: Any,
) -> None:
    def _notification_json(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "body": row["body"],
            "link": row["link"],
            "read": bool(row["read"]),
            "createdAt": row["created_at"],
        }

    @app.get("/api/notifications")
    async def list_notifications(request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        return {
            "notifications": [
                _notification_json(row) for row in db.list_notifications(uid)
            ],
            "unread": db.unread_notifications(uid),
        }

    @app.post("/api/notifications/{notification_id}/read")
    async def read_notification(notification_id: str, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        db.mark_notification(uid, notification_id)
        return {"id": notification_id, "read": True}

    @app.post("/api/notifications/read-all")
    async def read_all_notifications(request: Request) -> dict[str, Any]:
        db = schedule_store()
        db.mark_all_notifications(schedule_uid(request))
        return {"read": True}

    # -- schedules -------------------------------------------------------------------

    @app.get("/api/schedules")
    async def list_schedules(request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        return {
            "schedules": [
                schedule_catalog.schedule_json(row) for row in db.list_schedules(uid)
            ]
        }

    @app.get("/api/schedules/runs")
    async def list_schedule_runs(request: Request, limit: int = 50) -> dict[str, Any]:
        db = schedule_store()
        rows = db.list_user_schedule_runs(schedule_uid(request), limit)
        return {
            "runs": [
                {
                    **schedule_catalog.run_json(row),
                    "scheduleId": row["schedule_id"],
                    "scheduleName": row["schedule_name"],
                    "workspace": row["schedule_workspace"],
                    "appId": row["schedule_app_id"],
                    "modeId": row["schedule_mode_id"],
                }
                for row in rows
            ]
        }

    @app.post("/api/schedules", status_code=201)
    async def create_schedule(payload: CreateScheduleRequest, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        name = payload.name.strip()
        prompt = payload.prompt.strip()
        if not 1 <= len(name) <= 80 or not prompt:
            raise error(422, "name/prompt are required")
        kind = payload.kind
        if kind not in ("once", "interval", "daily", "weekly"):
            raise error(422, f"unknown schedule kind: {kind}")
        if kind in ("daily", "weekly") and (
            payload.hour is None or payload.minute is None
            or not 0 <= payload.hour <= 23 or not 0 <= payload.minute <= 59
        ):
            raise error(422, "hour/minute are required")
        if kind == "weekly" and (payload.weekday is None or not 0 <= payload.weekday <= 6):
            raise error(422, "weekday is required")
        if kind == "interval" and (
            payload.intervalMinutes is None or not 1 <= payload.intervalMinutes <= 10080
        ):
            raise error(422, "intervalMinutes must be 1-10080")
        workspace = resolve_workspace(uid, payload.workspace)
        app_id, mode_id = resolve_app_mode(payload.appId, payload.modeId)
        if kind == "once" and schedule_catalog.parse_iso(payload.runAt) is None:
            raise error(422, "runAt must be ISO-8601")
        next_at = schedule_catalog.next_run_at(
            kind=kind,
            run_at=payload.runAt,
            hour=payload.hour,
            minute=payload.minute,
            weekday=payload.weekday,
            interval_minutes=payload.intervalMinutes,
        )
        if next_at is None:
            raise error(422, "runAt must be in the future")
        schedule_id = db.create_schedule(
            uid=uid,
            workspace=workspace,
            app_id=app_id,
            mode_id=mode_id,
            name=name,
            prompt=prompt,
            kind=kind,
            run_at=payload.runAt,
            hour=payload.hour,
            minute=payload.minute,
            weekday=payload.weekday,
            interval_minutes=payload.intervalMinutes,
            next_run_at=next_at,
        )
        row = db.get_schedule(uid, schedule_id)
        return schedule_catalog.schedule_json(row)

    @app.patch("/api/schedules/{schedule_id}")
    async def update_schedule(
        schedule_id: str, payload: UpdateScheduleRequest, request: Request
    ) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        row = db.get_schedule(uid, schedule_id)
        if row is None:
            raise error(404, "unknown schedule")
        next_at = None
        if payload.enabled:
            next_at = schedule_catalog.next_run_at(
                kind=str(row["kind"]),
                run_at=row["run_at"],
                hour=row["hour"],
                minute=row["minute"],
                weekday=row["weekday"],
                interval_minutes=row["interval_minutes"],
            )
            if next_at is None:
                raise error(422, "schedule has no future run")
        db.set_schedule_enabled(uid, schedule_id, payload.enabled, next_at)
        return schedule_catalog.schedule_json(db.get_schedule(uid, schedule_id))

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        if db.get_schedule(uid, schedule_id) is None:
            raise error(404, "unknown schedule")
        db.delete_schedule(uid, schedule_id)
        return {"deleted": schedule_id}

    @app.post("/api/schedules/{schedule_id}/run", status_code=202)
    async def run_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        row = db.get_schedule(uid, schedule_id)
        if row is None:
            raise error(404, "unknown schedule")
        if schedule_runner is None:
            raise error(503, "scheduler is not running")
        await schedule_runner.run_now(row)
        return {"accepted": True, "id": schedule_id}

    @app.get("/api/schedules/{schedule_id}/runs")
    async def schedule_runs(schedule_id: str, request: Request) -> dict[str, Any]:
        db = schedule_store()
        uid = schedule_uid(request)
        if db.get_schedule(uid, schedule_id) is None:
            raise error(404, "unknown schedule")
        return {
            "runs": [
                schedule_catalog.run_json(row)
                for row in db.list_schedule_runs(schedule_id)
            ]
        }
