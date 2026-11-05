"""Workspaces: paths, rows and per-workspace session pointers."""

from __future__ import annotations

import pytest
from pi_server.config import ServerConfig
from pi_server.db import Database, DuplicateWorkspace


def test_workspace_path_is_default_or_sibling(tmp_path):
    config = ServerConfig(data_root=str(tmp_path))
    default = config.workspace_path("alice", "default")
    named = config.workspace_path("alice", "abc123")
    assert default == config.workspace_dir("alice")
    assert named.startswith(config.workspace_root)
    assert named != default
    # Never nested under the default workspace: its file tree must not see siblings.
    assert not named.startswith(default + "/")


def test_workspace_path_rejects_traversal(tmp_path):
    config = ServerConfig(data_root=str(tmp_path))
    with pytest.raises(ValueError):
        config.workspace_path("alice", "../escape")


def test_database_workspaces_roundtrip(tmp_path):
    db = Database(str(tmp_path / "team.db"))
    try:
        uid = db.create_user(username="alice", password_hash="hash")
        default_path = str(tmp_path / "workspaces" / uid)
        db.ensure_default_workspace(uid, default_path)

        rows = db.list_workspaces(uid)
        assert [(row["id"], row["name"], row["is_default"]) for row in rows] == [
            ("default", "默认工作空间", 1)
        ]

        workspace_id = db.create_workspace(
            uid, "研究", str(tmp_path / "workspaces" / f"{uid}-research")
        )
        assert db.get_workspace(uid, workspace_id)["name"] == "研究"
        assert len(db.list_workspaces(uid)) == 2

        # Names are unique per user, not globally.
        with pytest.raises(DuplicateWorkspace):
            db.create_workspace(uid, "研究", str(tmp_path / "unused"))
        bob = db.create_user(username="bob", password_hash="hash")
        db.ensure_default_workspace(bob, str(tmp_path / "workspaces" / bob))
        db.create_workspace(bob, "研究", str(tmp_path / "workspaces" / f"{bob}-research"))

        db.rename_workspace(uid, workspace_id, "文献")
        assert db.get_workspace(uid, workspace_id)["name"] == "文献"

        db.remember_workspace_session(uid, workspace_id, "/tmp/session.jsonl")
        db.remember_workspace_session(uid, "default", "/tmp/default.jsonl")
        assert db.recall_workspace_session(uid, workspace_id) == "/tmp/session.jsonl"
        assert db.recall_workspace_session(uid, "default") == "/tmp/default.jsonl"
        assert db.recall_workspace_session(uid, "missing") is None
    finally:
        db.close()


def test_registry_options_resolve_per_workspace(tmp_path):
    from pi_coding_agent.bootstrap import BootstrapOptions
    from pi_server.app import SessionRegistry

    config = ServerConfig(data_root=str(tmp_path), auth_mode="team")
    registry = SessionRegistry(BootstrapOptions(cwd=str(tmp_path)), config)

    default = registry.options_for("alice", "default")
    custom = registry.options_for("alice", "abc123")

    assert default.cwd == config.workspace_dir("alice")
    assert custom.cwd == config.workspace_path("alice", "abc123")
    assert custom.cwd != default.cwd
    assert custom.agent_dir == default.agent_dir
