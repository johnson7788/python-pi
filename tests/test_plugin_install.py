"""Admin plugin upload: zip validation and installation."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "plugins" / "hello-pi"


def _example_zip(root: str = "hello-pi") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("plugin.json", "main.py"):
            archive.write(EXAMPLE / name, f"{root}/{name}")
    return buffer.getvalue()


def test_admin_can_install_a_plugin_zip(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        response = client.post(
            "/api/plugins/install",
            content=_example_zip(),
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["id"] == "hello-pi"
        assert (tmp_path / "plugins" / "hello-pi" / "plugin.json").is_file()

        plugins = client.get("/api/plugins").json()["plugins"]
        assert [item["id"] for item in plugins] == ["hello-pi"]
        assert plugins[0]["enabled"] is False

        removed = client.delete("/api/plugins/hello-pi")
        assert removed.status_code == 200, removed.text
        assert not (tmp_path / "plugins" / "hello-pi").exists()
        assert client.get("/api/plugins").json()["plugins"] == []
        assert client.delete("/api/plugins/hello-pi").status_code == 404

        bad = client.post(
            "/api/plugins/install",
            content=b"not a zip",
            headers={"content-type": "application/octet-stream"},
        )
        assert bad.status_code == 422


def test_plugin_zip_cannot_escape_the_plugin_root(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.txt", "nope")
        archive.writestr("plugin.json", "{}")
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        response = client.post(
            "/api/plugins/install",
            content=buffer.getvalue(),
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 422
        assert not (tmp_path / "escape.txt").exists()
