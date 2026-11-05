"""P0 office document API: opaque ids, blob roundtrip, sha guard, version backup."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server import office
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig

FIXTURE = Path("/Users/admin/git/AIOffice/frontend/apps/docs/tests/pagination-corpus/docx/13-english-report.docx")


def _client(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")
    return config, app


def test_office_blob_roundtrip_and_sha_guard(tmp_path):
    config, app = _client(tmp_path)
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        uid = login.json()["user"]["uid"]
        workspace, _agent = config.ensure_user_layout(uid)
        target = Path(workspace) / "report.docx"
        target.write_bytes(FIXTURE.read_bytes())
        doc_id = office.encode_doc_id("default", "/report.docx")

        meta = client.get(f"/api/office/documents/{doc_id}")
        assert meta.status_code == 200, meta.text
        assert meta.json()["name"] == "report.docx"
        original_sha = meta.json()["sha"]

        blob = client.get(f"/api/office/documents/{doc_id}/blob")
        assert blob.status_code == 200 and blob.content.startswith(b"PK")

        saved = client.put(
            f"/api/office/documents/{doc_id}/blob",
            content=b"PK\x03\x04modified",
            headers={"If-Match": original_sha},
        )
        assert saved.status_code == 200, saved.text
        assert target.read_bytes() == b"PK\x03\x04modified"
        assert list((Path(workspace) / ".pi" / "versions" / doc_id).glob("*.docx"))

        conflict = client.put(
            f"/api/office/documents/{doc_id}/blob",
            content=b"PK\x03\x04conflict",
            headers={"If-Match": original_sha},
        )
        assert conflict.status_code == 409


def test_office_rejects_invalid_ids_and_paths(tmp_path):
    config, app = _client(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        assert client.get("/api/office/documents/not-a-doc").status_code == 400
        valid = office.encode_doc_id("default", "/missing.docx")
        assert client.get(f"/api/office/documents/{valid}").status_code == 404

        bad_ext = office.encode_doc_id("default", "/notes.txt")
        assert client.get(f"/api/office/documents/{bad_ext}").status_code == 400
        traversal = office.encode_doc_id("default", "/../escape.docx")
        assert client.get(f"/api/office/documents/{traversal}").status_code == 400


def test_office_lock_and_version_restore(tmp_path):
    from pi_server import auth

    config, app = _client(tmp_path)
    with TestClient(app) as client:
        admin = client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        uid = admin.json()["user"]["uid"]
        workspace, _agent = config.ensure_user_layout(uid)
        target = Path(workspace) / "paper.docx"
        target.write_bytes(FIXTURE.read_bytes())
        doc_id = office.encode_doc_id("default", "/paper.docx")

        bob_id = app.state.db.create_user(username="bob", password_hash=auth.hash_password("pw123456"))
        bob_workspace, _bob_agent = config.ensure_user_layout(bob_id)
        (Path(bob_workspace) / "paper.docx").write_bytes(FIXTURE.read_bytes())
        bob = TestClient(app)
        bob.post("/api/auth/login", json={"username": "bob", "password": "pw123456"})

        lock = client.post(f"/api/office/documents/{doc_id}/lock")
        assert lock.status_code == 200, lock.text
        token = lock.json()["token"]
        assert bob.post(f"/api/office/documents/{doc_id}/lock").status_code == 409
        assert (
            client.put(
                f"/api/office/documents/{doc_id}/blob",
                content=b"PK\x03\x04locked",
            ).status_code
            == 200
        )

        versions = client.get(f"/api/office/documents/{doc_id}/versions").json()["versions"]
        assert versions
        restored = client.post(
            f"/api/office/documents/{doc_id}/versions/{versions[0]['id']}/restore",
            headers={"X-Office-Token": token},
        )
        assert restored.status_code == 200, restored.text

        assert client.delete(f"/api/office/documents/{doc_id}/lock", headers={"X-Office-Token": token}).status_code == 200
        assert bob.post(f"/api/office/documents/{doc_id}/lock").status_code == 200
