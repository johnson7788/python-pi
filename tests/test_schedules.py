"""Schedules: next-run math, DB rows, headless execution and the HTTP surface."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server import schedules
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig
from pi_server.db import Database


def test_next_run_once_and_interval():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    future = (now + timedelta(hours=1)).isoformat(timespec="seconds")
    assert schedules.next_run_at(kind="once", now=now, run_at=future) == future
    assert schedules.next_run_at(kind="once", now=now, run_at="2020-01-01T00:00:00Z") is None
    interval = schedules.next_run_at(kind="interval", now=now, interval_minutes=15)
    assert interval == (now + timedelta(minutes=15)).isoformat(timespec="seconds")


def test_next_run_daily_and_weekly_are_future_utc():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    daily = schedules.next_run_at(kind="daily", now=now, hour=9, minute=30)
    parsed = schedules.parse_iso(daily)
    assert parsed is not None and parsed > now
    assert parsed - now <= timedelta(days=1)

    weekly = schedules.next_run_at(kind="weekly", now=now, hour=9, minute=30, weekday=0)
    parsed_week = schedules.parse_iso(weekly)
    assert parsed_week is not None and parsed_week > now
    assert parsed_week - now <= timedelta(days=8)


def test_database_due_schedules_are_claimed_by_next_time(tmp_path):
    db = Database(str(tmp_path / "team.db"))
    try:
        uid = db.create_user(username="alice", password_hash="hash")
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        due = db.create_schedule(
            uid=uid, workspace="default", app_id="general", mode_id="office",
            name="due", prompt="go", kind="interval", next_run_at=past, interval_minutes=30,
        )
        later = db.create_schedule(
            uid=uid, workspace="default", app_id="general", mode_id="office",
            name="later", prompt="go", kind="interval", next_run_at=future, interval_minutes=30,
        )
        ids = [str(row["id"]) for row in db.due_schedules(schedules.utcnow())]
        assert ids == [due]
        assert db.get_schedule(uid, later) is not None
        assert db.mark_schedule_running(due, past, future) is True
        assert db.mark_schedule_running(due, past, future) is False
    finally:
        db.close()


def test_runner_executes_headless_and_records_a_run(tmp_path):
    db = Database(str(tmp_path / "team.db"))
    try:
        uid = db.create_user(username="alice", password_hash="hash")
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        schedule_id = db.create_schedule(
            uid=uid, workspace="default", app_id="general", mode_id="office",
            name="daily check", prompt="summarize", kind="interval",
            next_run_at=past, interval_minutes=60,
        )

        class FakeWorker:
            busy = False

            async def run_scheduled_prompt(self, text: str) -> str:
                assert text == "summarize"
                return "/tmp/session.jsonl"

            async def shutdown(self, reason: str = "") -> None:
                assert reason

        class FakeRegistry:
            used_scheduled = False

            async def acquire_scheduled(self, *args):
                self.used_scheduled = True
                return FakeWorker()

            async def acquire(self, *args):
                raise AssertionError("scheduled runs must use a disposable worker")

        registry = FakeRegistry()
        runner = schedules.ScheduleRunner(db, registry)
        asyncio.run(runner._execute(schedule_id, manual=False))

        row = db.get_schedule_by_id(schedule_id)
        assert row["last_status"] == "success"
        assert row["next_run_at"] > past
        runs = db.list_schedule_runs(schedule_id)
        assert len(runs) == 1 and runs[0]["status"] == "success"
        assert runs[0]["session_file"] == "/tmp/session.jsonl"
        assert registry.used_scheduled
        notifications = db.list_notifications(uid)
        assert len(notifications) == 1
        assert notifications[0]["kind"] == "schedule_success"
    finally:
        db.close()


def test_schedule_http_surface(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        created = client.post(
            "/api/schedules",
            json={
                "name": "每小时巡检",
                "prompt": "检查服务状态",
                "kind": "interval",
                "intervalMinutes": 60,
            },
        )
        assert created.status_code == 201, created.text
        schedule = created.json()
        assert schedule["enabled"] is True and schedule["nextRunAt"]

        listed = client.get("/api/schedules").json()["schedules"]
        assert [item["id"] for item in listed] == [schedule["id"]]
        assert client.get("/api/schedules/runs").json()["runs"] == []

        disabled = client.patch(f"/api/schedules/{schedule['id']}", json={"enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        assert client.delete(f"/api/schedules/{schedule['id']}").status_code == 200
        assert client.get("/api/schedules").json()["schedules"] == []

        bad = client.post(
            "/api/schedules",
            json={"name": "bad", "prompt": "go", "kind": "cron"},
        )
        assert bad.status_code == 422


def test_single_mode_schedules_use_a_local_store(tmp_path):
    from pi_coding_agent.bootstrap import BootstrapOptions
    from pi_server.app import create_app
    from pi_server.config import ServerConfig

    config = ServerConfig(data_root=str(tmp_path), auth_mode="single")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    with TestClient(app) as client:
        assert client.get("/api/schedules").json()["schedules"] == []
        created = client.post(
            "/api/schedules",
            json={"name": "本地巡检", "prompt": "检查状态", "kind": "interval", "intervalMinutes": 60},
        )
        assert created.status_code == 201, created.text
        schedule = created.json()
        listed = client.get("/api/schedules").json()["schedules"]
        assert [item["id"] for item in listed] == [schedule["id"]]
        assert client.delete(f"/api/schedules/{schedule['id']}").status_code == 200
