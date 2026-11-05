"""Per-message feedback: rating, replacement and session-file boundary."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig


def test_feedback_roundtrip_and_path_guard(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        uid = login.json()["user"]["uid"]
        session_file = config.agent_dir(uid) + "/sessions/demo.jsonl"
        import os
        os.makedirs(os.path.dirname(session_file), exist_ok=True)
        open(session_file, "w", encoding="utf-8").close()

        rated = client.post(
            "/api/feedback",
            json={"sessionFile": session_file, "timestamp": 123.5, "rating": 1},
        )
        assert rated.status_code == 200, rated.text
        listed = client.get("/api/feedback", params={"session": session_file}).json()["feedback"]
        assert listed == [{"timestamp": 123.5, "rating": 1, "comment": ""}]

        changed = client.post(
            "/api/feedback",
            json={"sessionFile": session_file, "timestamp": 123.5, "rating": -1},
        )
        assert changed.status_code == 200
        assert client.get("/api/feedback", params={"session": session_file}).json()["feedback"][0]["rating"] == -1

        bad = client.post(
            "/api/feedback",
            json={"sessionFile": session_file, "timestamp": 1, "rating": 5},
        )
        assert bad.status_code == 422

        outside = client.post(
            "/api/feedback",
            json={"sessionFile": "/etc/passwd", "timestamp": 1, "rating": 1},
        )
        assert outside.status_code == 403
