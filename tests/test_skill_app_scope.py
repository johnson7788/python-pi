"""技能的应用作用域：`apps: [chart]` 只在该应用下可见。

技能可以声明自己只属于某几个应用，声明后不会出现在通用工作台里。
"""

from __future__ import annotations


from pi_coding_agent.resource_loader import ResourceLoader


def _write_skill(root, name: str, apps: list[str] | None = None) -> None:
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: test skill {name}\n"
    if apps:
        front += f"apps: [{', '.join(apps)}]\n"
    front += "---\n\n# body\n"
    (directory / "SKILL.md").write_text(front, encoding="utf-8")


def _loader(root, app_id: str) -> ResourceLoader:
    return ResourceLoader(
        str(root), str(root), project_trusted=True, include_home_skills=False, app_id=app_id
    )


def test_scoped_skill_only_visible_in_its_app(tmp_path):
    _write_skill(tmp_path, "common-skill")
    _write_skill(tmp_path, "chart-only", ["chart"])

    chart = {skill.name for skill in _loader(tmp_path, "chart").skills()}
    general = {skill.name for skill in _loader(tmp_path, "general").skills()}

    assert "chart-only" in chart
    assert "common-skill" in chart
    assert "chart-only" not in general
    assert "common-skill" in general


def test_no_app_context_keeps_scoped_skill(tmp_path):
    """CLI 没有应用概念，不能因为一个 apps: 限定就把技能藏起来。"""
    _write_skill(tmp_path, "chart-only", ["chart"])
    names = {skill.name for skill in _loader(tmp_path, "").skills()}
    assert "chart-only" in names


def test_explicit_allowlist_still_wins(tmp_path):
    _write_skill(tmp_path, "chart-only", ["chart"])
    loader = ResourceLoader(
        str(tmp_path), str(tmp_path), project_trusted=True,
        include_home_skills=False, skill_allowlist=("chart-only",), app_id="general",
    )
    assert loader.skills() == []
