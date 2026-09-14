"""Tests for resources, skills, settings and the system prompt.

Covers the discovery precedence, trust gating and prompt assembly documented in
``docs/research/03-coding-agent-core.md`` sections 4-6.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pi_coding_agent.resource_loader import (
    ResourceLoader,
    collect_extension_paths,
    find_config_dir,
    get_agent_dir,
    load_project_context_files,
)
from pi_coding_agent.settings import (
    DEFAULT_COMPACTION_KEEP_RECENT_TOKENS,
    DEFAULT_COMPACTION_RESERVE_TOKENS,
    DEFAULT_HTTP_IDLE_TIMEOUT_MS,
    DEFAULT_RETRY_BASE_DELAY_MS,
    DEFAULT_RETRY_MAX_RETRIES,
    DEFAULT_MAX_AGENT_RETRY_DELAY_MS,
    ENV_SETTINGS_MAP,
    ProjectNotTrustedError,
    ProjectTrustStore,
    SettingsManager,
    SettingsValidationError,
    TrustStoreError,
    deep_merge,
)
from pi_coding_agent.skills import (
    Skill,
    discover_skills,
    discover_skills_with_diagnostics,
    expand_skill_command,
    format_skills_for_prompt,
)
from pi_coding_agent.system_prompt import (
    DEFAULT_SYSTEM_PROMPT_TOOLS,
    BuildSystemPromptOptions,
    ContextFile,
    build_system_prompt,
    get_docs_path,
    get_system_prompt_options,
)

SKILL_TEMPLATE = """---
name: {name}
description: {description}
---
# {name}

Body of the skill.
"""


def write_skill(path: Path, name: str, description: str, extra: str = "") -> Path:
    """Create ``path/SKILL.md`` with minimal valid frontmatter."""
    path.mkdir(parents=True, exist_ok=True)
    body = SKILL_TEMPLATE.format(name=name, description=description) + extra
    (path / "SKILL.md").write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient PI_* settings overrides out of every test."""
    for env_name in ENV_SETTINGS_MAP:
        monkeypatch.delenv(env_name, raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    return directory


@pytest.fixture
def agent_dir(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake user-level agent directory (``~/.pi/agent``)."""
    directory = home / ".pi" / "agent"
    directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(directory))
    return directory


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    (directory / "nested" / "deep").mkdir(parents=True)
    return directory


# ---------------------------------------------------------------------------
# Skills: discovery and validation
# ---------------------------------------------------------------------------


def test_discover_skills_stops_at_skill_root_and_reads_root_markdown(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir / "alpha", "alpha", "Alpha skill")
    # Nested SKILL.md below a skill root must NOT become a separate skill.
    write_skill(skills_dir / "alpha" / "nested", "nested", "Nested skill")
    (skills_dir / "loose.md").write_text(
        "---\nname: loose\ndescription: Loose skill\n---\nbody\n", encoding="utf-8"
    )
    # No description -> plain markdown, not a skill.
    (skills_dir / "notes.md").write_text("just notes\n", encoding="utf-8")

    skills = discover_skills([str(skills_dir)])

    assert sorted(skill.name for skill in skills) == ["alpha", "loose"]
    alpha = next(skill for skill in skills if skill.name == "alpha")
    assert alpha.location.endswith(os.path.join("alpha", "SKILL.md"))
    assert alpha.base_dir == os.path.dirname(alpha.location)
    assert alpha.disable_model_invocation is False


def test_discover_skills_skips_hidden_and_node_modules(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir / ".hidden", "hidden", "Hidden")
    write_skill(skills_dir / "node_modules" / "dep", "dep", "Dependency")
    write_skill(skills_dir / "real", "real", "Real")

    assert [skill.name for skill in discover_skills([str(skills_dir)])] == ["real"]


def test_frontmatter_validation_rejects_bad_names_and_missing_descriptions(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    (skills_dir / "badname").mkdir(parents=True)
    (skills_dir / "badname" / "SKILL.md").write_text(
        "---\nname: Bad_Name\ndescription: invalid name\n---\nbody\n", encoding="utf-8"
    )
    (skills_dir / "nodesc").mkdir(parents=True)
    (skills_dir / "nodesc" / "SKILL.md").write_text("---\nname: nodesc\n---\nbody\n", encoding="utf-8")
    write_skill(skills_dir / "good", "good", "A valid skill")

    result = discover_skills_with_diagnostics([str(skills_dir)])

    assert [skill.name for skill in result.skills] == ["good"]
    messages = "\n".join(diagnostic.message for diagnostic in result.diagnostics)
    assert "invalid characters" in messages
    assert "description is required" in messages
    assert all(diagnostic.path for diagnostic in result.diagnostics)


def test_skill_name_falls_back_to_parent_directory_and_rejects_long_names(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    (skills_dir / "fallback").mkdir(parents=True)
    (skills_dir / "fallback" / "SKILL.md").write_text(
        "---\ndescription: no explicit name\n---\nbody\n", encoding="utf-8"
    )
    long_name = "a" * 65
    write_skill(skills_dir / "long", long_name, "too long")

    result = discover_skills_with_diagnostics([str(skills_dir)])

    assert [skill.name for skill in result.skills] == ["fallback"]
    assert any("exceeds 64 characters" in diagnostic.message for diagnostic in result.diagnostics)


def test_skill_name_collision_keeps_first(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_skill(first / "one", "dupe", "First wins")
    write_skill(second / "two", "dupe", "Second loses")

    result = discover_skills_with_diagnostics([str(first), str(second)])

    assert [skill.description for skill in result.skills] == ["First wins"]
    assert any(diagnostic.type == "collision" for diagnostic in result.diagnostics)


def test_format_skills_for_prompt_block_and_expansion(tmp_path: Path) -> None:
    location = write_skill(tmp_path / "skills" / "alpha", "alpha", "Alpha <skill>") / "SKILL.md"
    visible = Skill(
        name="alpha", description="Alpha <skill>", location=str(location), disable_model_invocation=False
    )
    hidden = Skill(
        name="secret", description="hidden", location=str(location), disable_model_invocation=True
    )

    block = format_skills_for_prompt([visible, hidden], "read")

    assert block.startswith("\n\nThe following skills provide specialized instructions")
    assert "Use the read tool to load a skill's file" in block
    assert "<available_skills>" in block
    assert "<name>alpha</name>" in block
    assert "<description>Alpha &lt;skill&gt;</description>" in block
    assert f"<location>{location}</location>" in block
    assert "secret" not in block
    assert format_skills_for_prompt([hidden], "read") == ""
    assert format_skills_for_prompt([], "read") == ""
    assert "Use bash to load a skill's file" in format_skills_for_prompt([visible], "bash")

    expanded = expand_skill_command("alpha", [visible])
    assert expanded is not None
    assert expanded.startswith(f'<skill name="alpha" location="{location}">')
    assert "Body of the skill." in expanded
    assert "---" not in expanded
    assert expand_skill_command("unknown", [visible]) is None


# ---------------------------------------------------------------------------
# ResourceLoader: context files, trust gating, overrides, prompts, extensions
# ---------------------------------------------------------------------------


def test_context_file_ordering_global_outermost_nearest(tmp_path: Path, agent_dir: Path, project: Path) -> None:
    (agent_dir / "AGENTS.md").write_text("global", encoding="utf-8")
    (project / "AGENTS.md").write_text("outermost", encoding="utf-8")
    (project / "nested" / "deep" / "AGENTS.md").write_text("nearest", encoding="utf-8")

    files = load_project_context_files(str(project / "nested" / "deep"), str(agent_dir))
    contents = [context_file.content for context_file in files]

    assert contents[0] == "global"
    local = [
        context_file.content
        for context_file in files
        if context_file.path.startswith(str(project))
    ]
    assert local == ["outermost", "nearest"]


def test_context_file_candidate_precedence(tmp_path: Path, agent_dir: Path, project: Path) -> None:
    (project / "AGENTS.md").write_text("plain", encoding="utf-8")
    (project / "AGENTS.override.md").write_text("override", encoding="utf-8")
    (project / "nested" / "CLAUDE.md").write_text("claude", encoding="utf-8")
    (project / "nested" / "AGENTS.md").write_text("agents", encoding="utf-8")

    files = load_project_context_files(str(project / "nested"), str(agent_dir))
    by_path = {context_file.path: context_file.content for context_file in files}

    assert by_path[str(project / "AGENTS.override.md")] == "override"
    assert str(project / "AGENTS.md") not in by_path  # override wins in the same directory
    # AGENTS.md wins over CLAUDE.md inside the same directory.
    assert by_path[str(project / "nested" / "AGENTS.md")] == "agents"
    assert str(project / "nested" / "CLAUDE.md") not in by_path


def test_untrusted_project_resources_ignored_until_trusted(agent_dir: Path, project: Path) -> None:
    config_dir = project / ".pi"
    write_skill(config_dir / "skills" / "foo", "foo", "Project skill")
    (config_dir / "prompts").mkdir(parents=True)
    (config_dir / "prompts" / "hello.md").write_text("---\ndescription: Hi\n---\nSay hi\n", encoding="utf-8")
    (config_dir / "extensions").mkdir(parents=True)
    (config_dir / "extensions" / "tool.py").write_text("# extension\n", encoding="utf-8")
    (config_dir / "SYSTEM.md").write_text("PROJECT SYSTEM", encoding="utf-8")
    (config_dir / "APPEND_SYSTEM.md").write_text("PROJECT APPEND", encoding="utf-8")
    (project / "AGENTS.md").write_text("project context", encoding="utf-8")

    loader = ResourceLoader(str(project), str(agent_dir), project_trusted=False)

    assert loader.skills() == []
    assert loader.prompt_templates() == []
    assert loader.extension_paths() == []
    assert loader.system_prompt_override() is None
    assert loader.append_system_prompt() == []
    # Context files are ordinary readable context: never trust-gated.
    assert [context_file.content for context_file in loader.context_files()][-1] == "project context"

    loader.set_project_trusted(True)

    assert [skill.name for skill in loader.skills()] == ["foo"]
    assert [template.name for template in loader.prompt_templates()] == ["hello"]
    assert [os.path.basename(path) for path in loader.extension_paths()] == ["tool.py"]
    assert loader.system_prompt_override() == "PROJECT SYSTEM"
    assert loader.append_system_prompt() == ["PROJECT APPEND"]


async def test_reload_two_phase_applies_trust_decision(agent_dir: Path, project: Path) -> None:
    write_skill(project / ".pi" / "skills" / "gated", "gated", "Gated skill")
    loader = ResourceLoader(str(project), str(agent_dir), project_trusted=False)

    seen: list[bool] = []

    def resolve(source: ResourceLoader) -> bool:
        # The pre-trust pass must not expose project resources.
        seen.append(source.skills() == [])
        return True

    await loader.reload(resolve_project_trust=resolve)

    assert seen == [True]
    assert [skill.name for skill in loader.skills()] == ["gated"]


def test_global_and_project_context_files_compose(agent_dir: Path, project: Path) -> None:
    (agent_dir / "AGENTS.md").write_text("global instructions", encoding="utf-8")
    (project / "nested" / "deep" / "AGENTS.md").write_text("deep instructions", encoding="utf-8")

    loader = ResourceLoader(str(project / "nested" / "deep"), str(agent_dir))

    contents = [context_file.content for context_file in loader.context_files()]
    assert contents[0] == "global instructions"
    assert contents[-1] == "deep instructions"


def test_skill_precedence_user_then_agent_then_project(agent_dir: Path, home: Path, project: Path) -> None:
    write_skill(home / ".agents" / "skills" / "shared", "shared", "From user agents dir")
    write_skill(agent_dir / "skills" / "only-agent", "only-agent", "From agent dir")
    write_skill(project / ".pi" / "skills" / "only-project", "only-project", "From project")
    write_skill(project / ".agents" / "skills" / "ancestor", "ancestor", "From ancestor")

    loader = ResourceLoader(str(project), str(agent_dir), project_trusted=True)

    assert sorted(skill.name for skill in loader.skills()) == [
        "ancestor",
        "only-agent",
        "only-project",
        "shared",
    ]

    # A same-named skill in a later directory loses to the earlier one.
    write_skill(project / ".pi" / "skills" / "shared", "shared", "Duplicate loses")
    loader.load()
    shared = next(skill for skill in loader.skills() if skill.name == "shared")
    assert shared.description == "From user agents dir"


def test_prompt_template_description_and_argument_hint(agent_dir: Path, project: Path) -> None:
    prompts = project / ".pi" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "review.md").write_text(
        "---\ndescription: Review the diff\nargument-hint: <branch>\n---\nReview $1\n",
        encoding="utf-8",
    )
    long_first_line = "x" * 80
    (prompts / "auto.md").write_text(f"\n{long_first_line}\n", encoding="utf-8")

    loader = ResourceLoader(str(project), str(agent_dir), project_trusted=True)
    templates = {template.name: template for template in loader.prompt_templates()}

    assert templates["review"].description == "Review the diff"
    assert templates["review"].argument_hint == "<branch>"
    assert templates["review"].content == "Review $1"
    assert templates["auto"].description == "x" * 60 + "..."


def test_collect_extension_paths_shapes(tmp_path: Path) -> None:
    extensions = tmp_path / "extensions"
    (extensions / "pkg").mkdir(parents=True)
    (extensions / "tool.py").write_text("", encoding="utf-8")
    (extensions / "tool.ts").write_text("", encoding="utf-8")
    (extensions / "pkg" / "index.ts").write_text("", encoding="utf-8")
    (extensions / "pkg" / "nested").mkdir()
    (extensions / "pkg" / "nested" / "index.py").write_text("", encoding="utf-8")
    (extensions / "readme.md").write_text("", encoding="utf-8")

    found = [os.path.basename(path) for path in collect_extension_paths(str(extensions))]

    assert sorted(found) == ["index.ts", "tool.py", "tool.ts"]


def test_find_config_dir_from_package_json(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    assert find_config_dir(str(project)) == ".pi"

    (project / "package.json").write_text('{"piConfig": {"configDir": ".agent-config"}}', encoding="utf-8")
    assert find_config_dir(str(project)) == ".agent-config"

    (project / "package.json").write_text("{not json", encoding="utf-8")
    assert find_config_dir(str(project)) == ".pi"


def test_get_agent_dir_honours_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "custom-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(target))
    assert get_agent_dir() == str(target)

    monkeypatch.delenv("PI_CODING_AGENT_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert get_agent_dir() == str(tmp_path / ".pi" / "agent")


def test_project_config_dir_applies_to_resources(agent_dir: Path, project: Path) -> None:
    (project / "package.json").write_text('{"piConfig": {"configDir": ".agent"}}', encoding="utf-8")
    write_skill(project / ".agent" / "skills" / "custom", "custom", "Custom config dir")

    loader = ResourceLoader(str(project), str(agent_dir), project_trusted=True)

    assert loader.config_dir_name == ".agent"
    assert [skill.name for skill in loader.skills()] == ["custom"]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_default_system_prompt_sections() -> None:
    prompt = build_system_prompt(
        BuildSystemPromptOptions(
            cwd="/work/dir",
            selected_tools=list(DEFAULT_SYSTEM_PROMPT_TOOLS),
            tool_snippets={
                "read": "Read a file",
                "bash": "Run a shell command",
                "edit": "Edit a file",
                "write": "Write a file",
            },
        )
    )

    assert prompt.startswith(
        "You are an expert coding assistant operating inside pi, a coding agent harness. "
        "You help users by reading files, executing commands, editing code, and writing new files."
    )
    assert "Available tools:\n- read: Read a file\n- bash: Run a shell command" in prompt
    assert "In addition to the tools above, you may have access to other custom tools depending on the project." in prompt
    assert "Guidelines:" in prompt
    # No dedicated search tools -> the bash guideline is present.
    assert "- Use bash for file operations like ls, rg, find" in prompt
    assert "- Be concise in your responses" in prompt
    assert "- Show file paths clearly when working with files" in prompt
    assert "Pi documentation (read only when the user asks about pi itself" in prompt
    assert "- Main documentation: " in prompt
    assert "- Additional docs: " in prompt
    assert "- Examples: " in prompt
    assert prompt.endswith("Current working directory: /work/dir")
    assert DEFAULT_SYSTEM_PROMPT_TOOLS == ("read", "bash", "edit", "write")
    assert get_docs_path().endswith("docs")


def test_default_prompt_only_lists_tools_with_snippets() -> None:
    prompt = build_system_prompt(
        BuildSystemPromptOptions(
            cwd="/work",
            selected_tools=["read", "grep", "ls", "find"],
            tool_snippets={"read": "Read a file", "grep": "Search files", "ls": "List a directory"},
        )
    )

    lines = prompt.split("Available tools:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert lines == ["- read: Read a file", "- grep: Search files", "- ls: List a directory"]
    # grep/find/ls are present -> no shell file-operation guideline.
    assert "for file operations like" not in prompt


def test_guideline_uses_powershell_variant() -> None:
    prompt = build_system_prompt(
        BuildSystemPromptOptions(
            cwd="/work",
            selected_tools=["powershell"],
            tool_snippets={"powershell": "Run PowerShell"},
        )
    )
    assert "- Use PowerShell for file operations like listing, searching, and finding files" in prompt

    both = build_system_prompt(
        BuildSystemPromptOptions(
            cwd="/work",
            selected_tools=["bash", "powershell"],
            tool_snippets={"bash": "Run bash", "powershell": "Run PowerShell"},
        )
    )
    assert (
        "- Use bash or PowerShell for file operations like listing, searching, and finding files" in both
    )


def test_prompt_guidelines_are_deduplicated_and_always_on_last() -> None:
    prompt = build_system_prompt(
        BuildSystemPromptOptions(
            cwd="/work",
            selected_tools=["read"],
            tool_snippets={"read": "Read a file"},
            prompt_guidelines=["  Be concise in your responses  ", "Read before editing"],
        )
    )
    guidelines = prompt.split("Guidelines:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert guidelines == [
        "- Be concise in your responses",
        "- Read before editing",
        "- Show file paths clearly when working with files",
    ]


def test_custom_prompt_replaces_default_body() -> None:
    prompt = build_system_prompt(
        BuildSystemPromptOptions(
            cwd="/work\\sub",
            custom_prompt="CUSTOM BODY",
            append_system_prompt="APPENDED",
            context_files=[ContextFile(path="/work/AGENTS.md", content="rules")],
            skills=[Skill(name="alpha", description="Alpha", location="/skills/alpha/SKILL.md")],
        )
    )

    assert prompt.startswith("CUSTOM BODY\n\nAPPENDED")
    assert "Available tools:" not in prompt
    assert "Guidelines:" not in prompt
    assert "<project_context>" in prompt
    assert '<project_instructions path="/work/AGENTS.md">\nrules\n</project_instructions>' in prompt
    assert "<available_skills>" in prompt
    assert "<name>alpha</name>" in prompt
    assert prompt.endswith("Current working directory: /work/sub\n")


def test_project_context_and_skills_in_default_prompt() -> None:
    base = build_system_prompt(
        BuildSystemPromptOptions(
            cwd="/work",
            selected_tools=["read"],
            tool_snippets={"read": "Read a file"},
        )
    )
    body = base[: base.rindex("\nCurrent working directory: /work")]
    assert "\n\n<project_context>" not in body

    prompt = build_system_prompt(
        BuildSystemPromptOptions(
            cwd="/work",
            selected_tools=["read"],
            tool_snippets={"read": "Read a file"},
            context_files=[ContextFile(path="/work/AGENTS.md", content="rules")],
            skills=[Skill(name="alpha", description="Alpha", location="/skills/alpha/SKILL.md")],
        )
    )

    assert (
        prompt
        == body
        + "\n\n<project_context>\n\nProject-specific instructions and guidelines:\n\n"
        '<project_instructions path="/work/AGENTS.md">\nrules\n</project_instructions>\n\n'
        "</project_context>\n"
        + format_skills_for_prompt(
            [Skill(name="alpha", description="Alpha", location="/skills/alpha/SKILL.md")], "read"
        )
        + "\nCurrent working directory: /work"
    )


def test_get_system_prompt_options_normalizes_tool_data() -> None:
    options = get_system_prompt_options(
        cwd="/work",
        selected_tools=["read", "missing", "bash"],
        tool_snippets={"read": "Read\n  a file", "bash": "Run a command", "missing": "nope"},
        tool_guidelines={"read": ["Only read what you need"], "bash": ["Quote paths"]},
        known_tools={"read", "bash"},
        append_system_prompt=["one", "two"],
    )

    assert options.selected_tools == ["read", "bash"]
    assert options.tool_snippets == {"read": "Read a file", "bash": "Run a command"}
    assert options.prompt_guidelines == ["Only read what you need", "Quote paths"]
    assert options.append_system_prompt == "one\n\ntwo"


def test_loaders_feed_system_prompt(agent_dir: Path, project: Path) -> None:
    write_skill(project / ".pi" / "skills" / "alpha", "alpha", "Alpha skill")
    (project / ".pi" / "SYSTEM.md").write_text("SYSTEM OVERRIDE", encoding="utf-8")
    loader = ResourceLoader(str(project), str(agent_dir), project_trusted=True)

    options = get_system_prompt_options(
        cwd=str(project),
        selected_tools=list(DEFAULT_SYSTEM_PROMPT_TOOLS),
        tool_snippets={"read": "Read a file", "bash": "Run a command"},
        tool_guidelines={},
        known_tools={"read", "bash", "edit", "write", "grep"},
        custom_prompt=loader.system_prompt_override(),
        append_system_prompt=loader.append_system_prompt() or None,
        context_files=loader.context_files(),
        skills=loader.skills(),
    )
    prompt = build_system_prompt(options)

    assert prompt.startswith("SYSTEM OVERRIDE")
    assert "Available tools:" not in prompt
    assert "<available_skills>" in prompt
    assert "<name>alpha</name>" in prompt
    assert f"Current working directory: {project}" in prompt


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_deep_merge_replaces_arrays_and_skips_none() -> None:
    base = {"theme": "dark", "extensions": ["a"], "compaction": {"enabled": True, "reserveTokens": 1}}
    overrides = {"theme": None, "extensions": ["b", "c"], "compaction": {"reserveTokens": 2}}

    merged = deep_merge(base, overrides)

    assert merged["theme"] == "dark"
    assert merged["extensions"] == ["b", "c"]
    assert merged["compaction"] == {"enabled": True, "reserveTokens": 2}


def test_settings_defaults(agent_dir: Path, project: Path) -> None:
    manager = SettingsManager(str(project), str(agent_dir))

    assert manager.get_theme() is None
    assert manager.get_default_project_trust() == "ask"
    assert manager.get_compaction_settings() == {
        "enabled": True,
        "reserveTokens": DEFAULT_COMPACTION_RESERVE_TOKENS,
        "keepRecentTokens": DEFAULT_COMPACTION_KEEP_RECENT_TOKENS,
    }
    assert manager.get_retry_settings() == {
        "enabled": True,
        "maxRetries": DEFAULT_RETRY_MAX_RETRIES,
        "baseDelayMs": DEFAULT_RETRY_BASE_DELAY_MS,
        "maxAgentDelayMs": DEFAULT_MAX_AGENT_RETRY_DELAY_MS,
    }
    assert manager.get_http_idle_timeout_ms() == DEFAULT_HTTP_IDLE_TIMEOUT_MS
    assert manager.get_websocket_connect_timeout_ms() is None
    assert manager.get_bash_timeout_ms() is None
    assert manager.get_auto_compaction() is True
    assert manager.get_enabled_models() is None
    assert manager.get_extension_paths() == []
    assert manager.get_telemetry().enabled is True
    assert manager.get_telemetry().analytics is False


def test_settings_precedence_project_over_global_and_overrides(agent_dir: Path, project: Path) -> None:
    write_json(
        agent_dir / "settings.json",
        {"theme": "dark", "defaultModel": "global-model", "extensions": ["/global/ext"]},
    )
    write_json(
        project / ".pi" / "settings.json",
        {"theme": "light", "extensions": ["/project/ext"]},
    )

    manager = SettingsManager(str(project), str(agent_dir), project_trusted=True)

    assert manager.get_theme() == "light"
    assert manager.get_default_model() == "global-model"
    assert manager.get_extension_paths() == ["/project/ext"]

    manager.set_env_overrides({"theme": "env-theme"})
    assert manager.get_theme() == "env-theme"

    manager.set_cli_overrides({"theme": "cli-theme", "defaultModel": "cli-model"})
    assert manager.get_theme() == "cli-theme"
    assert manager.get_default_model() == "cli-model"
    assert manager.get("theme") == "cli-theme"
    assert manager.get("missing.key", "fallback") == "fallback"


def test_settings_ignore_project_scope_when_untrusted(agent_dir: Path, project: Path) -> None:
    write_json(agent_dir / "settings.json", {"theme": "dark"})
    write_json(project / ".pi" / "settings.json", {"theme": "light"})

    manager = SettingsManager(str(project), str(agent_dir), project_trusted=False)

    assert manager.get_theme() == "dark"
    assert manager.get_project_settings() == {}
    manager.set_project_trusted(True)
    assert manager.get_theme() == "light"


def test_settings_validation_matches_upstream(agent_dir: Path, project: Path) -> None:
    write_json(
        agent_dir / "settings.json",
        {
            "compaction": {"reserveTokens": -1},
            "httpIdleTimeoutMs": "nonsense",
            "modelOverrides": {"p/m": 5},
        },
    )
    manager = SettingsManager(str(project), str(agent_dir))

    with pytest.raises(SettingsValidationError):
        manager.get_compaction_reserve_tokens()
    with pytest.raises(SettingsValidationError):
        manager.get_http_idle_timeout_ms()
    with pytest.raises(SettingsValidationError):
        manager.get_model_overrides()

    write_json(agent_dir / "settings.json", {"compaction": {"modelOverrides": {"p/m": {"reserveTokens": "x"}}}})
    manager.load()
    with pytest.raises(SettingsValidationError):
        manager.get_compaction_reserve_tokens("p/m")

    write_json(agent_dir / "settings.json", {"defaultProjectTrust": "sometimes", "retry": {"maxRetries": "many"}})
    manager.load()
    # Silently clamped fields fall back to their defaults.
    assert manager.get_default_project_trust() == "ask"
    assert manager.get_retry_settings()["maxRetries"] == DEFAULT_RETRY_MAX_RETRIES


def test_settings_per_model_compaction_override(agent_dir: Path, project: Path) -> None:
    write_json(
        agent_dir / "settings.json",
        {
            "compaction": {
                "reserveTokens": 1000,
                "modelOverrides": {"anthropic/claude": {"reserveTokens": 2000, "keepRecentTokens": 3000}},
            }
        },
    )
    manager = SettingsManager(str(project), str(agent_dir))

    assert manager.get_compaction_reserve_tokens() == 1000
    assert manager.get_compaction_reserve_tokens("anthropic/claude") == 2000
    assert manager.get_compaction_keep_recent_tokens("anthropic/claude") == 3000


async def test_partial_settings_write_preserves_sibling_keys(agent_dir: Path, project: Path) -> None:
    settings_path = agent_dir / "settings.json"
    write_json(
        settings_path,
        {
            "theme": "dark",
            "defaultModel": "keep-me",
            "extensions": ["/keep/ext"],
            "compaction": {"enabled": True, "reserveTokens": 4096, "keepRecentTokens": 8192},
        },
    )
    manager = SettingsManager(str(project), str(agent_dir))

    await manager.set_theme("light")

    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert stored["theme"] == "light"
    assert stored["defaultModel"] == "keep-me"
    assert stored["extensions"] == ["/keep/ext"]
    assert stored["compaction"] == {"enabled": True, "reserveTokens": 4096, "keepRecentTokens": 8192}

    # A nested write must keep the sibling nested keys on disk.
    await manager.set_compaction_enabled(False)

    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert stored["compaction"]["enabled"] is False
    assert stored["compaction"]["reserveTokens"] == 4096
    assert stored["compaction"]["keepRecentTokens"] == 8192
    assert stored["theme"] == "light"
    assert manager.get_compaction_settings()["reserveTokens"] == 4096


async def test_project_writes_require_trust_and_are_field_local(agent_dir: Path, project: Path) -> None:
    global_path = agent_dir / "settings.json"
    project_path = project / ".pi" / "settings.json"
    write_json(global_path, {"theme": "dark"})
    write_json(project_path, {"defaultModel": "keep-me"})

    manager = SettingsManager(str(project), str(agent_dir), project_trusted=False)
    with pytest.raises(ProjectNotTrustedError):
        await manager.set_project_theme("light")

    manager.set_project_trusted(True)
    await manager.set_project_theme("solarized")

    stored_project = json.loads(project_path.read_text(encoding="utf-8"))
    assert stored_project == {"defaultModel": "keep-me", "theme": "solarized"}
    assert json.loads(global_path.read_text(encoding="utf-8")) == {"theme": "dark"}


async def test_settings_writes_round_trip_and_load(agent_dir: Path, project: Path) -> None:
    manager = SettingsManager(str(project), str(agent_dir))

    await manager.set_default_model("gpt-x")
    await manager.set_retry_max_retries(7)
    await manager.set_http_idle_timeout_ms(30000)
    await manager.set_compaction_reserve_tokens(2048)
    await manager.set_enabled_models(["a", "b"])

    reloaded = SettingsManager(str(project), str(agent_dir))
    assert reloaded.get_default_model() == "gpt-x"
    assert reloaded.get_retry_settings()["maxRetries"] == 7
    assert reloaded.get_http_idle_timeout_ms() == 30000
    assert reloaded.get_compaction_reserve_tokens() == 2048
    assert reloaded.get_enabled_models() == ["a", "b"]


async def test_settings_setter_validation_raises(agent_dir: Path, project: Path) -> None:
    manager = SettingsManager(str(project), str(agent_dir))

    with pytest.raises(SettingsValidationError):
        await manager.set_compaction_reserve_tokens(-5)
    with pytest.raises(SettingsValidationError):
        await manager.set_compaction_keep_recent_tokens(1.5)
    with pytest.raises(SettingsValidationError):
        await manager.set_http_idle_timeout_ms(-1)
    with pytest.raises(SettingsValidationError):
        await manager.set_websocket_connect_timeout_ms("soon")
    with pytest.raises(SettingsValidationError):
        await manager.set_model_overrides({"p/m": 3})


async def test_settings_load_error_is_reported_and_write_refused(agent_dir: Path, project: Path) -> None:
    (agent_dir / "settings.json").write_text("{not json", encoding="utf-8")
    manager = SettingsManager(str(project), str(agent_dir))

    errors = manager.drain_errors()
    assert [error.scope for error in errors] == ["global"]
    assert manager.get_theme() is None

    with pytest.raises(SettingsValidationError):
        await manager.set_theme("light")
    # The unparsable file is left untouched.
    assert (agent_dir / "settings.json").read_text(encoding="utf-8") == "{not json"


def test_trust_store_round_trip(agent_dir: Path, project: Path) -> None:
    store = ProjectTrustStore(str(agent_dir))
    assert store.get_trust(str(project)) is None

    store.set_trust(str(project), True)
    assert store.get_trust(str(project)) is True
    assert store.get_trust(str(project / "nested" / "deep")) is True

    store.set_trust(str(project / "nested"), False)
    assert store.get_trust(str(project / "nested" / "deep")) is False
    assert store.get_trust(str(project)) is True

    raw = (agent_dir / "trust.json").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    keys = list(json.loads(raw))
    assert keys == sorted(keys)

    store.set_trust(str(project), None)
    assert store.get_trust(str(project)) is None

    (agent_dir / "trust.json").write_text('{"a": "yes"}', encoding="utf-8")
    with pytest.raises(TrustStoreError):
        store.get_trust(str(project))
