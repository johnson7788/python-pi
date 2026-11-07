"""Scheduled prompts: next-run calculation and the background runner."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import utcnow

#: Re-check due schedules at this cadence.  A schedule's own minute granularity is finer.
TICK_SECONDS = 20


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def next_run_at(
    *,
    kind: str,
    now: datetime | None = None,
    run_at: str | None = None,
    hour: int | None = None,
    minute: int | None = None,
    weekday: int | None = None,
    interval_minutes: int | None = None,
) -> str | None:
    """Next occurrence in UTC; ``None`` means "does not repeat"."""
    current = now or datetime.now(timezone.utc)
    if kind == "once":
        target = parse_iso(run_at)
        return None if target is None or current >= target else iso_utc(target)
    if kind == "interval":
        return iso_utc(current + timedelta(minutes=max(1, int(interval_minutes or 60))))
    local = current.astimezone()
    target_hour = 0 if hour is None else int(hour)
    target_minute = 0 if minute is None else int(minute)
    candidate = local.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if kind == "daily":
        if candidate <= local:
            candidate += timedelta(days=1)
        return iso_utc(candidate)
    if kind == "weekly":
        wanted = 0 if weekday is None else int(weekday) % 7
        days = (wanted - candidate.weekday()) % 7
        candidate += timedelta(days=days)
        if candidate <= local:
            candidate += timedelta(days=7)
        return iso_utc(candidate)
    return None


class ScheduleRunner:
    """Owns the background loop; execution reuses the normal worker/agent path."""

    def __init__(self, database: Any, registry: Any) -> None:
        self.database = database
        self.registry = registry
        self._task: asyncio.Task | None = None
        self._running: set[str] = set()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one bad schedule must not stop the loop
                print(f"pi-py: schedule tick failed: {error}", file=sys.stderr)
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        for schedule in self.database.due_schedules(utcnow()):
            schedule_id = str(schedule["id"])
            if schedule_id in self._running:
                continue
            asyncio.create_task(self._execute(schedule_id, manual=False))

    async def run_now(self, schedule: Any) -> None:
        asyncio.create_task(self._execute(str(schedule["id"]), manual=True))

    async def _execute(self, schedule_id: str, *, manual: bool) -> None:
        schedule = self.database.get_schedule_by_id(schedule_id)
        if schedule is None:
            return
        self._running.add(schedule_id)
        run_id = self.database.start_schedule_run(schedule_id)
        worker = None
        session_file: str | None = None
        status = "success"
        error: str | None = None
        try:
            worker = await self.registry.acquire_scheduled(
                str(schedule["uid"]),
                str(schedule["workspace"]),
                str(schedule["app_id"]),
                str(schedule["mode_id"]),
            ) if hasattr(self.registry, "acquire_scheduled") else await self.registry.acquire(
                str(schedule["uid"]),
                str(schedule["workspace"]),
                str(schedule["app_id"]),
                str(schedule["mode_id"]),
            )
            if worker.busy:
                raise RuntimeError("worker is busy with another turn")
            if hasattr(worker, "run_scheduled_prompt"):
                session_file = await worker.run_scheduled_prompt(str(schedule["prompt"]))
            else:
                session_file = await worker.run_prompt(str(schedule["prompt"]))
        except Exception as failure:  # noqa: BLE001
            status = "failed"
            error = str(failure)
        finally:
            if worker is not None:
                try:
                    await worker.shutdown("scheduled run complete")
                except Exception:  # noqa: BLE001 - cleanup must not hide the run result
                    pass
            self.database.finish_schedule_run(
                run_id, status=status, session_file=session_file, error=error
            )
            row = self.database.get_schedule_by_id(schedule_id)
            if row is not None:
                kind = str(row["kind"])
                if manual:
                    next_at = row["next_run_at"]
                    enabled = bool(row["enabled"])
                elif status != "success" and kind in ("once",):
                    next_at = iso_utc(datetime.now(timezone.utc) + timedelta(minutes=5))
                    enabled = True
                else:
                    next_at = next_run_at(
                        kind=kind,
                        run_at=row["run_at"],
                        hour=row["hour"],
                        minute=row["minute"],
                        weekday=row["weekday"],
                        interval_minutes=row["interval_minutes"],
                    )
                    enabled = next_at is not None
                self.database.finish_schedule(
                    schedule_id,
                    status=status,
                    error=error,
                    next_run_at=next_at,
                    enabled=enabled,
                )
                if status == "success":
                    body = "已生成新会话" if session_file else "已完成"
                    self.database.create_notification(
                        str(row["uid"]),
                        kind="schedule_success",
                        title=f"定时任务完成：{row['name']}",
                        body=body,
                        link="/workbench/scheduled",
                    )
                else:
                    self.database.create_notification(
                        str(row["uid"]),
                        kind="schedule_failed",
                        title=f"定时任务失败：{row['name']}",
                        body=error or "未知错误",
                        link="/workbench/scheduled",
                    )
            self._running.discard(schedule_id)


def schedule_json(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "prompt": row["prompt"],
        "workspace": row["workspace"],
        "appId": row["app_id"],
        "modeId": row["mode_id"],
        "kind": row["kind"],
        "runAt": row["run_at"],
        "hour": row["hour"],
        "minute": row["minute"],
        "weekday": row["weekday"],
        "intervalMinutes": row["interval_minutes"],
        "enabled": bool(row["enabled"]),
        "nextRunAt": row["next_run_at"],
        "lastRunAt": row["last_run_at"],
        "lastStatus": row["last_status"],
        "lastError": row["last_error"],
        "createdAt": row["created_at"],
    }


def run_json(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "status": row["status"],
        "sessionFile": row["session_file"],
        "error": row["error"],
    }
