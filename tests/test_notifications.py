"""Notification center: DB roundtrip and the HTTP surface."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig
from pi_server.db import Database


def test_database_notifications_roundtrip(tmp_path):
    db = Database(str(tmp_path / "team.db"))
    try:
        uid = db.create_user(username="alice", password_hash="hash")
        assert db.unread_notifications(uid) == 0
        first = db.create_notification(uid, kind="schedule_success", title="done", body="ok")
        second = db.create_notification(uid, kind="schedule_failed", title="failed", body="boom")
        assert db.unread_notifications(uid) == 2
        assert [row["id"] for row in db.list_notifications(uid)] == [second, first]

        db.mark_notification(uid, first)
        assert db.unread_notifications(uid) == 1
        db.mark_all_notifications(uid)
        assert db.unread_notifications(uid) == 0
    finally:
        db.close()


def test_notifications_http_surface(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        uid = login.json()["user"]["uid"]
        app.state.db.create_notification(
            uid, kind="schedule_success", title="done", link="/workbench/scheduled"
        )

        payload = client.get("/api/notifications").json()
        assert payload["unread"] == 1
        assert payload["notifications"][0]["title"] == "done"

        notification_id = payload["notifications"][0]["id"]
        assert client.post(f"/api/notifications/{notification_id}/read").status_code == 200
        assert client.get("/api/notifications").json()["unread"] == 0

        app.state.db.create_notification(uid, kind="schedule_failed", title="failed")
        assert client.post("/api/notifications/read-all").status_code == 200
        assert client.get("/api/notifications").json()["unread"] == 0
