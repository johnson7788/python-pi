"""Tests for the skills on/off switch.

Why this exists: skill descriptions are injected into *every* system prompt. A
machine-wide ``~/.agents/skills`` shared with other agent tooling held 100 skills
here, which measured 43,923 characters (~11k tokens) of system prompt — 95% of it
the ``<available_skills>`` block — on every single request. pi has per-path
overrides in settings for this; the port had no switch at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pi_coding_agent.resource_loader import ResourceLoader
from pi_coding_agent.system_prompt import BuildSystemPromptOptions, build_system_prompt
from pi_coding_agent.tools import create_builtin_tools


def _make_skill(root: Path, name: str, description: str) -> None:
    directory = root / ".agents" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8"
    )


def _system_prompt(loader: ResourceLoader, cwd: str) -> str:
    tools = create_builtin_tools(cwd, ("read", "bash", "edit", "write"))
    return build_system_prompt(
        BuildSystemPromptOptions(
            cwd=cwd,
            selected_tools=[tool.name for tool in tools],
            tool_snippets={tool.name: (tool.prompt_snippet or tool.description) for tool in tools},
            context_files=loader.context_files(),
            skills=loader.skills(),
            append_system_prompt="\n".join(loader.append_system_prompt()) or None,
        )
    )


@pytest.fixture()
def home_with_skills(tmp_path, monkeypatch):
    """A fake $HOME whose ``.agents/skills`` holds a few skills."""
    home = tmp_path / "home"
    home.mkdir()
    _make_skill(home, "alpha-skill", "Does alpha things. " * 40)
    _make_skill(home, "beta-skill", "Does beta things. " * 40)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_skills_are_discovered_by_default(home_with_skills, tmp_path):
    loader = ResourceLoader(str(tmp_path), str(tmp_path / "agent"), project_trusted=False)
    names = {skill.name for skill in loader.skills()}
    assert {"alpha-skill", "beta-skill"} <= names


def test_skills_can_be_switched_off(home_with_skills, tmp_path):
    loader = ResourceLoader(
        str(tmp_path), str(tmp_path / "agent"), project_trusted=False, skills_enabled=False
    )
    assert loader.skills() == []

    # And the system prompt consequently shrinks by the whole skills block.
    cwd = str(tmp_path)
    with_skills = _system_prompt(
        ResourceLoader(cwd, str(tmp_path / "agent"), skills_enabled=True), cwd
    )
    without = _system_prompt(
        ResourceLoader(cwd, str(tmp_path / "agent"), skills_enabled=False), cwd
    )
    assert "<available_skills>" in with_skills
    assert "<available_skills>" not in without
    assert len(without) < len(with_skills) / 2


def test_bootstrap_option_reaches_the_loader(home_with_skills, tmp_path):
    """The CLI flag must actually change what the prompt contains."""
    import asyncio

    from pi_agent import AgentTool
    from pi_coding_agent.bootstrap import BootstrapOptions, bootstrap

    credentials = tmp_path / "agent"
    credentials.mkdir()
    (credentials / "auth.json").write_text('{"local": {"type": "api_key", "key": "k"}}')
    (credentials / "models.json").write_text(
        '{"providers": {"local": {"api": "openai-completions",'
        ' "baseUrl": "http://127.0.0.1:1/v1",'
        ' "models": [{"id": "m", "contextWindow": 1000, "maxTokens": 100}]}}}'
    )

    async def prompt_with(no_skills: bool) -> tuple[str, list[AgentTool]]:
        state = await bootstrap(
            BootstrapOptions(
                cwd=str(tmp_path),
                agent_dir=str(credentials),
                model_pattern="local/m",
                no_skills=no_skills,
                no_extensions=True,
            )
        )
        try:
            return state.session.get_system_prompt(), state.session.get_all_tools()
        finally:
            await state.shutdown()

    with_prompt, _ = asyncio.run(prompt_with(False))
    without_prompt, _ = asyncio.run(prompt_with(True))

    assert "<available_skills>" in with_prompt
    assert "<available_skills>" not in without_prompt
    assert len(without_prompt) < len(with_prompt)
