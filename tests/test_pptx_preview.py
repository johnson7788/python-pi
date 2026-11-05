"""PPTX text preview without a python-pptx dependency."""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig


def _pptx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<?xml version="1.0"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>研究综述</a:t></a:r></a:p>
    <a:p><a:r><a:t>GLP-1 与 2 型糖尿病</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>""",
        )
        archive.writestr("ppt/slides/slide2.xml", "<not-xml")
    return buffer.getvalue()


def test_pptx_preview_extracts_slide_text_and_rejects_bad_files(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        client.put(
            "/api/workspace/file?path=/demo.pptx",
            content=_pptx_bytes(),
            headers={"content-type": "application/octet-stream"},
        )
        response = client.get("/api/artifacts/pptx?path=/demo.pptx")
        assert response.status_code == 422  # slide2 is intentionally malformed

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "ppt/slides/slide1.xml",
                """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                        xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                     <p:cSld><a:t>Only one slide</a:t></p:cSld></p:sld>""",
            )
        client.put(
            "/api/workspace/file?path=/ok.pptx",
            content=buffer.getvalue(),
            headers={"content-type": "application/octet-stream"},
        )
        payload = client.get("/api/artifacts/pptx?path=/ok.pptx").json()
        assert payload["slides"][0]["texts"] == ["Only one slide"]
