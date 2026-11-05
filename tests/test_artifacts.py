"""Artifact listing: workspace-scoped deliverables, newest first."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig


def test_artifacts_lists_editable_deliverables(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        assert response.status_code == 200

        for path, content in (
            ("/notes.md", "# notes\n"),
            ("/data.csv", "a,b\n1,2\n"),
            ("/demo.pptx", "not really pptx"),
            ("/ignore.bin", "binary"),
        ):
            response = client.put(
                f"/api/workspace/file?path={path}",
                json={"content": content},
            )
            assert response.status_code == 200, response.text

        payload = client.get("/api/artifacts").json()
        by_name = {item["name"]: item for item in payload["artifacts"]}
        assert by_name["notes.md"]["kind"] == "document"
        assert by_name["data.csv"]["kind"] == "sheet"
        assert by_name["demo.pptx"]["kind"] == "slides"
        assert by_name["notes.md"]["editable"] is True
        assert by_name["demo.pptx"]["editable"] is False
        assert "ignore.bin" not in by_name


def test_artifacts_are_scoped_to_a_named_workspace(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        workspace = client.post("/api/workspaces", json={"name": "研究空间"}).json()
        client.put(
            f"/api/workspace/file?path=/only.md&workspace={workspace['id']}",
            json={"content": "# only\n"},
        )

        default_payload = client.get("/api/artifacts").json()
        assert default_payload["artifacts"] == []
        named_payload = client.get(f"/api/artifacts?workspace={workspace['id']}").json()
        assert [item["name"] for item in named_payload["artifacts"]] == ["only.md"]
