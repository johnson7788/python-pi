"""Sidecar-backed XLSX open/save smoke test.

Skipped when the Rust sidecar has not been built; run
``./scripts/build-xlsx-sidecar.sh`` first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server import office
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig

FIXTURE = Path("/Users/admin/git/AIOffice/backend/app/seed/sheets-sample.xlsx")
SIDECAR = Path(__file__).resolve().parents[1] / "office" / "xlsx-engine" / "target" / "release" / "xlsx-sidecar"


@pytest.mark.skipif(not SIDECAR.is_file(), reason="xlsx sidecar not built")
def test_sheets_open_save(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        uid = login.json()["user"]["uid"]
        client.get("/api/workspaces")
        workspace = app.state.db.get_workspace(uid, "default")["path"]
        target = Path(workspace) / "sheet.xlsx"
        target.write_bytes(FIXTURE.read_bytes())
        doc_id = office.encode_doc_id("default", "/sheet.xlsx")

        opened = client.post("/api/office/sheets/open", content=FIXTURE.read_bytes())
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["sessionId"]
        assert opened.json()["sheets"]

        saved = client.post(
            "/api/office/sheets/save",
            json={
                "sessionId": session_id,
                "docId": doc_id,
                "replacements": [],
                "removals": [],
                "additions": [],
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["docId"] == doc_id
        assert target.read_bytes().startswith(b"PK")
        assert client.post("/api/office/sheets/close", json={"sessionId": session_id}).status_code == 200
