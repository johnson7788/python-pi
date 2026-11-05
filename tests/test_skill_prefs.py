"""Per-user skill preferences.

The loader filters disabled skills on read; the team database stores the toggles.
These tests cover both halves without starting a server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pi_coding_agent.resource_loader import ResourceLoader
from pi_server.db import Database


def _make_skill(root: Path, name: str, description: str) -> None:
    directory = root / ".agents" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
        encoding="utf-8",
    )


@pytest.fixture()
def skills_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _make_skill(home, "alpha-skill", "Does alpha things.")
    _make_skill(home, "beta-skill", "Does beta things.")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_disabled_skill_disappears_from_loader(skills_home, tmp_path):
    loader = ResourceLoader(
        str(tmp_path),
        str(tmp_path / "agent"),
        disabled_skills=("beta-skill",),
    )
    assert {skill.name for skill in loader.skills()} == {"alpha-skill"}


def test_home_skills_can_be_excluded_from_team_sessions(skills_home, tmp_path):
    agent = tmp_path / "agent"
    shared = agent / "skills" / "team-skill"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text(
        "---\nname: team-skill\ndescription: Shared by the deployment.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    loader = ResourceLoader(
        str(tmp_path),
        str(agent),
        include_home_skills=False,
    )
    names = {skill.name for skill in loader.skills()}
    assert "team-skill" in names
    assert "alpha-skill" not in names


def test_set_disabled_skills_filters_without_reload(skills_home, tmp_path):
    loader = ResourceLoader(str(tmp_path), str(tmp_path / "agent"))
    assert {"alpha-skill", "beta-skill"} <= {skill.name for skill in loader.skills()}

    loader.set_disabled_skills(["alpha-skill"])
    assert {skill.name for skill in loader.skills()} == {"beta-skill"}

    loader.set_disabled_skills(["alpha-skill", "beta-skill"])
    assert loader.skills() == []

    loader.set_disabled_skills([])
    assert {"alpha-skill", "beta-skill"} <= {skill.name for skill in loader.skills()}


def test_database_skill_prefs_roundtrip(tmp_path):
    db = Database(str(tmp_path / "team.db"))
    try:
        uid = db.create_user(username="alice", password_hash="hash")
        assert db.skill_prefs(uid) == {}
        assert db.disabled_skills(uid) == []

        db.set_skill_pref(uid, "beta-skill", False)
        assert db.skill_prefs(uid) == {"beta-skill": False}
        assert db.disabled_skills(uid) == ["beta-skill"]

        db.set_skill_pref(uid, "beta-skill", True)
        assert db.skill_prefs(uid) == {"beta-skill": True}
        assert db.disabled_skills(uid) == []

        # Preferences are scoped per user; another account starts clean.
        bob = db.create_user(username="bob", password_hash="hash")
        assert db.skill_prefs(bob) == {}
    finally:
        db.close()
