"""Plugin catalog, enablement and loading."""

from __future__ import annotations

import json
from pathlib import Path

from pi_coding_agent.bootstrap import BootstrapOptions
from pi_coding_agent.extensions import load_extensions
from pi_server import plugins
from pi_server.app import SessionRegistry
from pi_server.config import ServerConfig
from pi_server.db import Database

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "plugins" / "hello-pi"


def _write_plugin(root: Path, plugin_id: str, *, entry: str = "main.py") -> Path:
    directory = root / plugin_id
    directory.mkdir(parents=True)
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "0.1.0",
                "entry": entry,
                "description": "test",
                "capabilities": ["tools"],
            }
        ),
        encoding="utf-8",
    )
    (directory / entry).write_text("def setup(api):\n    pass\n", encoding="utf-8")
    return directory


def test_scan_finds_valid_and_skips_broken(tmp_path):
    _write_plugin(tmp_path, "hello")
    broken = tmp_path / "bad-id"
    broken.mkdir()
    (broken / "plugin.json").write_text(
        json.dumps({"id": "bad-id", "entry": "missing.py", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (broken / "main.py").write_text("def setup(api):\n    pass\n", encoding="utf-8")
    (tmp_path / "not-a-plugin").mkdir()

    found = plugins.scan(str(tmp_path))
    assert [info.id for info in found] == ["hello"]


def test_example_plugin_registers_a_tool():
    result = load_extensions([str(EXAMPLE / "main.py")], discover_entry_points=False)
    assert result.diagnostics == []
    names = {tool.name for api in result.apis for tool in api.tools.values()}
    assert "hello_plugin" in names


def test_database_plugin_enablement_roundtrip(tmp_path):
    db = Database(str(tmp_path / "team.db"))
    try:
        uid = db.create_user(username="alice", password_hash="hash")
        assert db.enabled_plugins(uid, "general") == []
        db.set_plugin_enabled(uid, "general", "hello-pi", True)
        assert db.enabled_plugins(uid, "general") == ["hello-pi"]
        # App-scoped: research does not see general's toggle.
        assert db.enabled_plugins(uid, "research") == []
        db.set_plugin_enabled(uid, "general", "hello-pi", False)
        assert db.enabled_plugins(uid, "general") == []
    finally:
        db.close()


def test_registry_adds_enabled_plugin_paths(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team")
    entry = str(EXAMPLE / "main.py")
    registry = SessionRegistry(
        BootstrapOptions(cwd=str(tmp_path)),
        config,
        plugin_paths_lookup=lambda _uid, app_id: [entry] if app_id == "general" else [],
    )
    assert registry.options_for("alice", "default", "general", "office").plugin_paths == (entry,)
    assert registry.options_for("alice", "default", "research", "review").plugin_paths == ()
