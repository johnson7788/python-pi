"""System prompt assembly.

Port of ``packages/coding-agent/src/core/system-prompt.ts`` (upstream pi).

The prompt is rebuilt from scratch whenever the active tool set changes, so the
text is a pure function of its inputs: a custom prompt (``SYSTEM.md``) replaces
the default body entirely, while append text, project context files and the
skill catalog are layered on top in a fixed order.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Collection, Iterable, Mapping, Sequence

from .skills import Skill, format_skills_for_prompt

#: Tools assumed when the caller does not pass an explicit selection.
DEFAULT_SYSTEM_PROMPT_TOOLS: tuple[str, ...] = ("read", "bash", "edit", "write")

#: Tool preference for reading skill files, in order.
_SKILL_FILE_READ_TOOLS: tuple[str, ...] = ("read", "bash")


@dataclass(frozen=True)
class ContextFile:
    """A context/instruction file (e.g. ``AGENTS.md``) with its raw content."""

    path: str
    content: str


@dataclass(frozen=True)
class BuildSystemPromptOptions:
    """Inputs of :func:`build_system_prompt`.

    Mirrors upstream ``BuildSystemPromptOptions``.  ``cwd`` is first only
    because it is required; callers should use keyword arguments.
    """

    cwd: str
    #: Custom system prompt (replaces the default body entirely).
    custom_prompt: str | None = None
    #: Tools to include in the prompt. Default: read, bash, edit, write.
    selected_tools: Sequence[str] | None = None
    #: One-line tool snippets keyed by tool name; only these tools are listed.
    tool_snippets: Mapping[str, str] | None = None
    #: Extra guideline bullets appended before the always-on guidelines.
    prompt_guidelines: Sequence[str] | None = None
    #: Text appended to the system prompt (``APPEND_SYSTEM.md``).
    append_system_prompt: str | None = None
    #: Pre-loaded context files, in global -> outermost -> nearest order.
    context_files: Sequence[ContextFile] | None = None
    #: Pre-loaded skills advertised in the prompt.
    skills: Sequence[Skill] | None = None


def get_package_dir() -> str:
    """Directory of the installed coding-agent package.

    ``PI_PACKAGE_DIR`` overrides it for packaging systems where the module path
    does not reflect the install layout.
    """
    override = os.environ.get("PI_PACKAGE_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return str(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )


def get_readme_path() -> str:
    """Absolute path advertised as ``Main documentation``."""
    return os.path.join(get_package_dir(), "README.md")


def get_docs_path() -> str:
    """Absolute path advertised as ``Additional docs``."""
    return os.path.join(get_package_dir(), "docs")


def get_examples_path() -> str:
    """Absolute path advertised as ``Examples``."""
    return os.path.join(get_package_dir(), "examples")


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_prompt_snippet(snippet: str | None) -> str | None:
    """Collapse a tool snippet to a single trimmed line (or ``None``)."""
    if not snippet:
        return None
    one_line = _WHITESPACE_RE.sub(" ", snippet.replace("\r", " ").replace("\n", " ")).strip()
    return one_line or None


def normalize_prompt_guidelines(guidelines: Iterable[str] | None) -> list[str]:
    """Trim guidelines, drop empties and keep first-seen order."""
    if not guidelines:
        return []
    unique: dict[str, None] = {}
    for guideline in guidelines:
        normalized = guideline.strip()
        if normalized:
            unique.setdefault(normalized, None)
    return list(unique)


def get_system_prompt_options(
    *,
    cwd: str,
    selected_tools: Sequence[str],
    tool_snippets: Mapping[str, str] | None = None,
    tool_guidelines: Mapping[str, Sequence[str]] | None = None,
    known_tools: Collection[str] | None = None,
    custom_prompt: str | None = None,
    append_system_prompt: str | Sequence[str] | None = None,
    context_files: Sequence[ContextFile] | None = None,
    skills: Sequence[Skill] | None = None,
) -> BuildSystemPromptOptions:
    """Collect per-tool prompt data into normalized build options.

    Mirrors ``AgentSession._rebuildSystemPrompt``: unknown tools are dropped,
    snippets are collapsed to one line, and each tool's guidelines are gathered
    in tool order.  ``append_system_prompt`` accepts a list of files and is
    joined with a blank line, matching ``getAppendSystemPrompt().join("\\n\\n")``.
    """
    valid_tools = [name for name in selected_tools if known_tools is None or name in known_tools]

    snippets: dict[str, str] = {}
    guidelines: list[str] = []
    for name in valid_tools:
        snippet = normalize_prompt_snippet((tool_snippets or {}).get(name))
        if snippet is not None:
            snippets[name] = snippet
        guidelines.extend((tool_guidelines or {}).get(name, ()))

    append: str | None
    if isinstance(append_system_prompt, str) or append_system_prompt is None:
        append = append_system_prompt
    else:
        parts = list(append_system_prompt)
        append = "\n\n".join(parts) if parts else None

    return BuildSystemPromptOptions(
        cwd=cwd,
        custom_prompt=custom_prompt,
        selected_tools=valid_tools,
        tool_snippets=snippets,
        prompt_guidelines=normalize_prompt_guidelines(guidelines),
        append_system_prompt=append,
        context_files=list(context_files or []),
        skills=list(skills or []),
    )


def _project_context_section(context_files: Sequence[ContextFile]) -> str:
    """Render ``<project_context>`` with one ``<project_instructions>`` per file."""
    section = "\n\n<project_context>\n\n"
    section += "Project-specific instructions and guidelines:\n\n"
    for context_file in context_files:
        section += (
            f'<project_instructions path="{context_file.path}">\n'
            f"{context_file.content}\n</project_instructions>\n\n"
        )
    section += "</project_context>\n"
    return section


def _skill_file_read_tool(tools: Sequence[str]) -> str | None:
    """First of read/bash present in ``tools``, or ``None`` when neither is."""
    for candidate in _SKILL_FILE_READ_TOOLS:
        if candidate in tools:
            return candidate
    return None


def build_system_prompt(options: BuildSystemPromptOptions) -> str:
    """Build the system prompt with tools, guidelines, context and skills.

    The default body advertises only tools that supply a prompt snippet; when a
    custom prompt is present that body (tool list, guidelines and the pi
    documentation section) is dropped entirely.
    """
    prompt_cwd = options.cwd.replace("\\", "/")

    append_section = f"\n\n{options.append_system_prompt}" if options.append_system_prompt else ""
    context_files = list(options.context_files or [])
    skills = list(options.skills or [])
    tools = list(options.selected_tools) if options.selected_tools is not None else list(DEFAULT_SYSTEM_PROMPT_TOOLS)
    skill_file_read_tool = _skill_file_read_tool(tools)

    if options.custom_prompt:
        prompt = options.custom_prompt
        if append_section:
            prompt += append_section
        if context_files:
            prompt += _project_context_section(context_files)
        if skill_file_read_tool and skills:
            prompt += format_skills_for_prompt(skills, skill_file_read_tool)
        prompt += f"\nCurrent working directory: {prompt_cwd}\n"
        return prompt

    snippets = options.tool_snippets or {}
    visible_tools = [name for name in tools if snippets.get(name)]
    tools_list = (
        "\n".join(f"- {name}: {snippets[name]}" for name in visible_tools) if visible_tools else "(none)"
    )

    guidelines_list: list[str] = []
    guidelines_set: set[str] = set()

    def add_guideline(guideline: str) -> None:
        if guideline in guidelines_set:
            return
        guidelines_set.add(guideline)
        guidelines_list.append(guideline)

    has_bash = "bash" in tools
    has_powershell = "powershell" in tools
    has_grep = "grep" in tools
    has_find = "find" in tools
    has_ls = "ls" in tools

    # Only suggest a shell for file exploration when no dedicated tool exists.
    if (has_bash or has_powershell) and not has_grep and not has_find and not has_ls:
        if has_bash and has_powershell:
            add_guideline("Use bash or PowerShell for file operations like listing, searching, and finding files")
        elif has_powershell:
            add_guideline("Use PowerShell for file operations like listing, searching, and finding files")
        else:
            add_guideline("Use bash for file operations like ls, rg, find")

    for guideline in options.prompt_guidelines or []:
        normalized = guideline.strip()
        if normalized:
            add_guideline(normalized)

    # Always present, and always last.
    add_guideline("Be concise in your responses")
    add_guideline("Show file paths clearly when working with files")

    guidelines = "\n".join(f"- {guideline}" for guideline in guidelines_list)

    prompt = f"""You are an expert coding assistant operating inside pi, a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files.

Available tools:
{tools_list}

In addition to the tools above, you may have access to other custom tools depending on the project.

Guidelines:
{guidelines}

Pi documentation (read only when the user asks about pi itself, its SDK, extensions, themes, skills, or TUI):
- Main documentation: {get_readme_path()}
- Additional docs: {get_docs_path()}
- Examples: {get_examples_path()} (extensions, custom tools, SDK)
- When reading pi docs or examples, resolve docs/... under Additional docs and examples/... under Examples, not the current working directory
- When asked about: extensions (docs/extensions.md, examples/extensions/), themes (docs/themes.md), skills (docs/skills.md), prompt templates (docs/prompt-templates.md), TUI components (docs/tui.md), keybindings (docs/keybindings.md), SDK integrations (docs/sdk.md), custom providers (docs/custom-provider.md), adding models (docs/models.md), pi packages (docs/packages.md), environment variables (docs/environment-variables.md)
- When working on pi topics, read the docs and examples, and follow .md cross-references before implementing
- Always read pi .md files completely and follow links to related docs (e.g., tui.md for TUI API details)"""

    if append_section:
        prompt += append_section
    if context_files:
        prompt += _project_context_section(context_files)
    if skill_file_read_tool and skills:
        prompt += format_skills_for_prompt(skills, skill_file_read_tool)

    prompt += f"\nCurrent working directory: {prompt_cwd}"
    return prompt
