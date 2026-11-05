"""Marketplace browsing, ratings and installs."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig


def _client(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")
    return config, app


def test_market_lists_rates_and_installs_a_skill(tmp_path):
    config, app = _client(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        payload = client.get("/api/market?kind=skill").json()
        assert len(payload["items"]) >= 4
        item = payload["items"][0]
        assert item["source"] and item["installed"] is False

        rated = client.post(f"/api/market/skill/{item['id']}/rating", json={"score": 5})
        assert rated.status_code == 200
        assert rated.json()["average"] == 5.0 and rated.json()["count"] == 1

        again = client.get("/api/market?kind=skill").json()["items"][0]
        assert again["rating"]["mine"] == 5
        assert again["rating"]["count"] == 1

        installed = client.post(f"/api/market/skills/{item['id']}/install")
        assert installed.status_code == 200, installed.text
        assert (tmp_path / "agents" / app.state.db.list_users()[0]["uid"] / "skills" / item["id"] / "SKILL.md").is_file()

        after = client.get("/api/market?kind=skill").json()["items"][0]
        assert after["installed"] is True
        assert client.post(f"/api/market/skills/{item['id']}/install").status_code == 409


def test_market_plugin_install_copies_the_bundled_package(tmp_path):
    config, app = _client(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        payload = client.get("/api/market?kind=plugin").json()
        item = next(entry for entry in payload["items"] if entry["id"] == "hello-pi")
        assert item["canInstall"] if "canInstall" in item else True

        installed = client.post("/api/market/plugins/hello-pi/install")
        assert installed.status_code == 200, installed.text
        assert (tmp_path / "plugins" / "hello-pi" / "plugin.json").is_file()
        assert client.post("/api/market/plugins/hello-pi/install").status_code == 409


def test_market_search_and_detail(tmp_path):
    config, app = _client(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        filtered = client.get("/api/market?kind=skill&q=证据").json()["items"]
        assert [item["id"] for item in filtered] == ["evidence-table"]

        community = client.get("/api/market?kind=skill&q=社区").json()["items"]
        assert {item["id"] for item in community} == {"weekly-report", "literature-screening"}

        detail = client.get("/api/market/skill/evidence-table").json()
        assert detail["source"] == "团队"
        assert "## 输出格式" in detail["readme"]

        assert client.get("/api/market/skill/missing").status_code == 404


def test_market_tag_filter_and_sort(tmp_path):
    config, app = _client(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        tagged = client.get("/api/market?kind=skill&tag=研究").json()["items"]
        assert {item["id"] for item in tagged} == {"evidence-table", "literature-screening"}

        by_name = client.get("/api/market?kind=skill&sort=name").json()["items"]
        names = [item["name"] for item in by_name]
        assert names == sorted(names)

        payload = client.get("/api/market?kind=skill").json()
        assert "研究" in payload["tags"]
