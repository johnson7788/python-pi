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


def test_apps_are_seeded_once_and_shared_by_the_deployment(tmp_path):
    from pi_server.db import Database

    path = str(tmp_path / "team.db")
    db = Database(path)
    try:
        db.seed_team_apps(apps.catalog_json()["apps"])
        edited = next(app for app in db.list_team_apps() if app["id"] == "general")
        edited["modes"][0]["prompt"] = "team-wide prompt"
        db.replace_team_app(edited, builtin=True)

        # Re-seeding must not overwrite an edit, and a second connection sees it.
        db.seed_team_apps(apps.catalog_json()["apps"])
        assert db.team_app_composition("general", "office")[0] == "team-wide prompt"
    finally:
        db.close()

    reopened = Database(path)
    try:
        again = next(app for app in reopened.list_team_apps() if app["id"] == "general")
        assert again["modes"][0]["prompt"] == "team-wide prompt"
        assert again["builtin"] is True
    finally:
        reopened.close()


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


def _team_client(tmp_path):
    from fastapi.testclient import TestClient
    from pi_server.app import create_app
    from pi_server.auth import ensure_admin
    from pi_server.config import ServerConfig

    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")
    return config, app, TestClient(app)


def test_only_admins_can_change_apps(tmp_path):
    from pi_server import auth

    config, app, client = _team_client(tmp_path)
    bob = app.state.db.create_user(username="bob", password_hash=auth.hash_password("pw123456"))
    del bob
    with client:
        client.post("/api/auth/login", json={"username": "bob", "password": "pw123456"})
        listed = client.get("/api/apps").json()
        assert listed["canManage"] is False
        created = client.post(
            "/api/apps", json={"name": "x", "modes": [{"name": "m", "skillBinding": "all"}]}
        )
        assert created.status_code == 403
        assert client.delete("/api/apps/general").status_code == 403


def test_app_duplicate_order_export_import(tmp_path):
    _, _, client = _team_client(tmp_path)
    with client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        created = client.post(
            "/api/apps",
            json={
                "name": "可复制",
                "accent": "#123456",
                "modes": [
                    {
                        "name": "模式",
                        "prompt": "p",
                        "skillBinding": "all",
                        "examples": ["例子一"],
                        "placeholder": "说点什么",
                        "icon": "book",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        app_id = created.json()["id"]

        copied = client.post(f"/api/apps/{app_id}/duplicate")
        assert copied.status_code == 201, copied.text
        assert copied.json()["id"] != app_id
        assert "副本" in copied.json()["name"]
        assert copied.json()["modes"][0]["examples"] == ["例子一"]

        ids = [item["id"] for item in client.get("/api/apps").json()["apps"]]
        reversed_ids = list(reversed(ids))
        assert client.post("/api/apps/order", json={"ids": reversed_ids}).status_code == 200
        assert [item["id"] for item in client.get("/api/apps").json()["apps"]] == reversed_ids

        exported = client.get("/api/apps/export")
        assert exported.status_code == 200
        payload = exported.json()
        assert payload["format"] == 1 and len(payload["apps"]) == len(ids)
        assert all("builtin" not in item for item in payload["apps"])

        imported = client.post("/api/apps/import", json={"apps": [payload["apps"][0]]})
        assert imported.status_code == 201, imported.text
        assert len(client.get("/api/apps").json()["apps"]) == len(ids) + 1


def test_delete_is_blocked_while_a_schedule_uses_the_app(tmp_path):
    _, app, client = _team_client(tmp_path)
    with client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        created = client.post(
            "/api/apps",
            json={"name": "有任务", "modes": [{"name": "m", "skillBinding": "all"}]},
        ).json()
        admin = client.get("/api/auth/me").json()["user"]
        app.state.db.create_schedule(
            uid=admin["uid"],
            workspace="default",
            app_id=created["id"],
            mode_id=created["modes"][0]["id"],
            name="nightly",
            prompt="x",
            kind="interval",
            next_run_at="2099-01-01T00:00:00Z",
            interval_minutes=60,
        )
        blocked = client.delete(f"/api/apps/{created['id']}")
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["schedules"] == ["nightly"]

        # Once the schedule is gone, delete succeeds and clears plugin enablement.
        schedule = app.state.db.list_schedules(admin["uid"])[0]
        app.state.db.delete_schedule(admin["uid"], schedule["id"])
        app.state.db.set_plugin_enabled(admin["uid"], created["id"], "hello-pi", True)
        assert client.delete(f"/api/apps/{created['id']}").status_code == 200
        assert app.state.db.plugin_states(admin["uid"], created["id"]) == {}


def test_session_header_records_the_app(tmp_path):
    from pi_coding_agent.session_manager import SessionManager, SessionInfo

    manager = SessionManager.create(
        str(tmp_path), session_dir=str(tmp_path / "sessions"), app="research", mode="review"
    )
    file_path = manager.get_session_file()
    assert file_path
    manager._rewrite_file()
    header = manager._file_entries[0]
    assert getattr(header, "app", None) == "research"
    assert getattr(header, "mode", None) == "review"

    reopened = SessionManager.open(file_path)
    assert getattr(reopened._file_entries[0], "app", None) == "research"

    import asyncio

    listed = asyncio.run(SessionManager.list(str(tmp_path), session_dir=str(tmp_path / "sessions")))
    assert listed and isinstance(listed[0], SessionInfo)
    assert listed[0].app == "research" and listed[0].mode == "review"


def test_switching_to_a_session_from_another_app_is_refused(tmp_path):
    import asyncio

    from pi_coding_agent.modes.rpc_mode import RpcMode
    from pi_coding_agent.session_manager import SessionManager

    manager = SessionManager.create(
        str(tmp_path), session_dir=str(tmp_path / "sessions"), app="research", mode="review"
    )
    manager._rewrite_file()
    path = manager.get_session_file()

    class FakeSession:
        def subscribe(self, _listener):
            return lambda: None

    class Runtime:
        calls = 0
        session = FakeSession()

        def set_rebind_session(self, _callback):
            return None

        async def switch_session(self, session_path: str):
            Runtime.calls += 1
            return {"cancelled": False}

    runtime = Runtime()
    mode = RpcMode(runtime=runtime, app_id="general", mode_id="office")
    response = asyncio.run(mode._cmd_switch_session({"sessionPath": path}, "cmd-1"))
    assert response["success"] is False
    assert "belongs to app" in response["error"]
    assert Runtime.calls == 0
