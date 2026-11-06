"""LAN self-registration: member accounts, no forced password change."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig


def test_register_creates_member_and_allows_login(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")
    with TestClient(app) as client:
        registered = client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "alicepw123", "displayName": "Alice"},
        )
        assert registered.status_code == 201, registered.text
        user = registered.json()["user"]
        assert user["username"] == "alice" and user["role"] == "user"
        assert user["mustChangePassword"] is False

        login = client.post("/api/auth/login", json={"username": "alice", "password": "alicepw123"})
        assert login.status_code == 200
        assert login.json()["user"]["displayName"] == "Alice"

        duplicate = client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "otherpw123"},
        )
        assert duplicate.status_code == 409

        short = client.post(
            "/api/auth/register",
            json={"username": "bob", "password": "short"},
        )
        assert short.status_code == 422
