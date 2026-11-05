"""The app / work-mode / capsule catalog and its composition."""

from __future__ import annotations

from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server import apps
from pi_server.app import SessionRegistry
from pi_server.config import ServerConfig


def test_catalog_has_defaults_and_capsules():
    catalog = apps.catalog_json()
    ids = {app["id"] for app in catalog["apps"]}
    assert {"general", "research", "slides"} <= ids
    assert catalog["defaultApp"] == "general"
    general = next(app for app in catalog["apps"] if app["id"] == "general")
    assert {mode["name"] for mode in general["modes"]} == {"办公", "开发", "设计"}
    assert all(mode["capsules"] for mode in general["modes"])


def test_composition_selects_prompt_and_skill_binding():
    office = apps.composition_for("general", "office")
    review = apps.composition_for("research", "review")
    assert office[0] and review[0]
    assert office[0] != review[0]
    assert office[1] is None and review[1] is None


def test_unknown_app_falls_back_to_the_default():
    prompt, skills = apps.composition_for("missing", "missing")
    assert prompt == apps.composition_for("general", "office")[0]
    assert skills is None


def test_registry_options_carry_the_app_prompt(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team")
    registry = SessionRegistry(BootstrapOptions(cwd=str(tmp_path)), config)
    office = registry.options_for("alice", "default", "general", "office")
    review = registry.options_for("alice", "default", "research", "review")
    assert office.app_prompt and review.app_prompt
    assert office.app_prompt != review.app_prompt
    assert office.app_skills is None


def test_apps_are_seeded_and_scoped_per_tenant(tmp_path):
    from pi_server.db import Database

    db = Database(str(tmp_path / "team.db"))
    try:
        alice = db.create_user(username="alice", password_hash="hash")
        bob = db.create_user(username="bob", password_hash="hash")
        db.seed_apps(alice, apps.catalog_json()["apps"])
        db.seed_apps(bob, apps.catalog_json()["apps"])
        before = next(app for app in db.list_apps(bob) if app["id"] == "general")
        assert before["modes"][0]["prompt"]

        edited = next(app for app in db.list_apps(alice) if app["id"] == "general")
        edited["modes"][0]["prompt"] = "alice-only prompt"
        db.replace_app(alice, edited, builtin=True)

        alice_again = next(app for app in db.list_apps(alice) if app["id"] == "general")
        bob_again = next(app for app in db.list_apps(bob) if app["id"] == "general")
        assert alice_again["modes"][0]["prompt"] == "alice-only prompt"
        assert bob_again["modes"][0]["prompt"] != "alice-only prompt"
        composition = db.app_composition(alice, "general", "office")
        assert composition[0] == "alice-only prompt"
    finally:
        db.close()


def test_app_crud_over_http(tmp_path):
    from fastapi.testclient import TestClient
    from pi_server.app import create_app
    from pi_server.auth import ensure_admin
    from pi_server.config import ServerConfig

    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        listed = client.get("/api/apps").json()
        assert listed["canManage"] is True
        assert len(listed["apps"]) == 3

        created = client.post(
            "/api/apps",
            json={
                "name": "自定义应用",
                "description": "test",
                "accent": "#123456",
                "modes": [
                    {
                        "name": "模式一",
                        "prompt": "you are a custom mode",
                        "skillBinding": "all",
                        "capsules": [{"name": "开始", "prompt": "go"}],
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        app_id = created.json()["id"]

        updated = client.put(
            f"/api/apps/{app_id}",
            json={
                "name": "改名后的应用",
                "description": "changed",
                "accent": "#654321",
                "modes": [
                    {
                        "id": created.json()["modes"][0]["id"],
                        "name": "模式一",
                        "prompt": "changed prompt",
                        "skillBinding": "all",
                        "capsules": [],
                    }
                ],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "改名后的应用"

        assert client.delete("/api/apps/general").status_code == 400
        assert client.delete(f"/api/apps/{app_id}").status_code == 200


def test_app_audit_and_restore_builtin(tmp_path):
    from fastapi.testclient import TestClient
    from pi_server.app import create_app
    from pi_server.auth import ensure_admin
    from pi_server.config import ServerConfig

    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        general = next(item for item in client.get("/api/apps").json()["apps"] if item["id"] == "general")
        original_prompt = general["modes"][0]["prompt"]

        edited = {**general, "modes": [{**general["modes"][0], "prompt": "temporary prompt"}]}
        assert client.put("/api/apps/general", json=edited).status_code == 200

        audit = client.get("/api/apps/general/audit").json()["audit"]
        assert audit[0]["action"] == "update"
        assert audit[0]["actor"] == "admin"

        restored = client.post("/api/apps/general/restore")
        assert restored.status_code == 200
        assert restored.json()["modes"][0]["prompt"] == original_prompt
        actions = [item["action"] for item in client.get("/api/apps/general/audit").json()["audit"]]
        assert actions[:2] == ["restore", "update"]

        custom = client.post(
            "/api/apps",
            json={"name": "custom", "modes": [{"name": "m", "skillBinding": "all"}]},
        ).json()
        assert client.post(f"/api/apps/{custom['id']}/restore").status_code == 400
        assert client.delete(f"/api/apps/{custom['id']}").status_code == 200
        actions = [item["action"] for item in client.get(f"/api/apps/{custom['id']}/audit").json()["audit"]]
        assert actions == ["delete", "create"]
