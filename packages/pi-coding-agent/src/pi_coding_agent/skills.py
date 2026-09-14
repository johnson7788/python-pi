"""Agent Skills discovery and prompt formatting.

Port of ``packages/coding-agent/src/core/skills.ts`` (upstream pi).

Why this module exists
----------------------
A skill is a markdown file (``SKILL.md`` or a root-level ``*.md`` in a skills
directory) carrying YAML frontmatter.  Skills are *not* inlined into the model
context: only their metadata is advertised in the system prompt, and the model
opens the file with the read/bash tool when a description matches the task.

Discovery rules (mirrors upstream ``loadSkillsFromDirInternal``), in order:

1. a directory that directly contains ``SKILL.md`` is a skill root -- it is
   loaded and recursion **stops there**, so nested ``SKILL.md`` files below it
   are never rediscovered as separate skills;
2. otherwise direct ``*.md`` children of the *root* directory are treated as
   skills (``include_root_files``);
3. otherwise every non-hidden subdirectory is scanned recursively.

Validation is deliberately non-fatal: a malformed skill is skipped and turned
into a diagnostic, so one bad file can never break discovery.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import yaml

#: Max skill name length per the Agent Skills specification.
MAX_NAME_LENGTH = 64

#: Max description length per the Agent Skills specification.
MAX_DESCRIPTION_LENGTH = 1024

#: Directories that are never scanned for skills.
_SKIP_DIR_NAMES = frozenset({"node_modules"})

_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")

#: Frontmatter key that opts a skill out of model-driven invocation.
DISABLE_MODEL_INVOCATION_KEY = "disable-model-invocation"

#: Diagnostic kinds, matching upstream ``ResourceDiagnostic["type"]``.
DIAGNOSTIC_WARNING = "warning"
DIAGNOSTIC_ERROR = "error"
DIAGNOSTIC_COLLISION = "collision"


@dataclass(frozen=True)
class ResourceDiagnostic:
    """A non-fatal problem found while loading a resource.

    Discovery never raises: every rejected file, unreadable directory or name
    collision surfaces here so the caller can render or log it.
    """

    type: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class Skill:
    """A discovered skill.

    ``location`` is the absolute path of the markdown file so the model can
    open it directly; ``base_dir`` (its parent directory) is what relative
    references inside the file resolve against.
    """

    name: str
    description: str
    location: str
    disable_model_invocation: bool = False

    @property
    def base_dir(self) -> str:
        """Directory holding the skill file; relative paths resolve here."""
        return os.path.dirname(self.location)


@dataclass
class SkillDiscoveryResult:
    """Aggregate result of a discovery pass."""

    skills: list[Skill] = field(default_factory=list)
    diagnostics: list[ResourceDiagnostic] = field(default_factory=list)


def parse_frontmatter(content: str) -> tuple[dict[str, object], str]:
    """Split a markdown document into frontmatter mapping and body.

    Follows upstream ``utils/frontmatter.ts``: only a document whose very first
    characters are ``---`` carries frontmatter, the block ends at the first
    newline followed by ``---``, and the body is stripped.  A YAML syntax error
    propagates as :class:`yaml.YAMLError` so the caller can report it.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]

    if not normalized.startswith("---"):
        return {}, normalized

    end_index = normalized.find("\n---", 3)
    if end_index == -1:
        return {}, normalized

    yaml_string = normalized[4:end_index]
    body = normalized[end_index + 4 :].strip()
    parsed = yaml.safe_load(yaml_string)
    if parsed is None:
        return {}, body
    if not isinstance(parsed, dict):
        raise yaml.YAMLError("frontmatter must be a YAML mapping")
    return {str(key): value for key, value in parsed.items()}, body


def strip_frontmatter(content: str) -> str:
    """Return the markdown body without its YAML frontmatter."""
    return parse_frontmatter(content)[1]


def validate_skill_name(name: str) -> list[str]:
    """Return the spec violations for ``name`` (empty when valid)."""
    errors: list[str] = []
    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_NAME_LENGTH} characters ({len(name)})")
    if not _NAME_PATTERN.match(name):
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens")
    return errors


def validate_skill_description(description: object) -> list[str]:
    """Return the spec violations for ``description`` (empty when valid)."""
    if not isinstance(description, str) or description.strip() == "":
        return ["description is required"]
    if len(description) > MAX_DESCRIPTION_LENGTH:
        return [f"description exceeds {MAX_DESCRIPTION_LENGTH} characters ({len(description)})"]
    return []


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _is_file(path: str) -> bool:
    return os.path.isfile(path)


def _skill_from_file(
    file_path: str,
    *,
    is_declared_skill: bool | None = None,
) -> tuple[Skill | None, list[ResourceDiagnostic]]:
    """Load and validate one markdown file as a skill.

    Returns ``(None, diagnostics)`` when the file cannot be read, has broken
    frontmatter, lacks a description, or violates the naming rules.  ``SKILL.md``
    files report read/parse failures as warnings; plain root ``*.md`` files are
    silent for parse failures because most markdown files are not skills.
    """
    diagnostics: list[ResourceDiagnostic] = []
    declared = os.path.basename(file_path) == "SKILL.md" if is_declared_skill is None else is_declared_skill

    try:
        raw = _read_text(file_path)
    except OSError as error:
        diagnostics.append(ResourceDiagnostic(DIAGNOSTIC_WARNING, str(error), file_path))
        return None, diagnostics

    try:
        frontmatter, _body = parse_frontmatter(raw)
    except yaml.YAMLError as error:
        if declared:
            diagnostics.append(ResourceDiagnostic(DIAGNOSTIC_WARNING, f"failed to parse frontmatter: {error}", file_path))
        return None, diagnostics

    description = frontmatter.get("description")
    has_description = isinstance(description, str) and description.strip() != ""
    if not declared and not has_description:
        # Root-level markdown files only become skills when they opted in with a
        # description; everything else is ordinary documentation.
        return None, diagnostics

    parent_name = os.path.basename(os.path.dirname(file_path))
    raw_name = frontmatter.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else parent_name

    name_errors = validate_skill_name(name)
    description_errors = validate_skill_description(description)
    for message in name_errors + description_errors:
        diagnostics.append(ResourceDiagnostic(DIAGNOSTIC_WARNING, message, file_path))

    if name_errors or description_errors:
        # The Python port skips invalid skills outright (upstream only warns and
        # still loads them) so that a model never sees an unusable skill name.
        return None, diagnostics

    assert isinstance(description, str)
    skill = Skill(
        name=name,
        description=description,
        location=os.path.abspath(file_path),
        disable_model_invocation=frontmatter.get(DISABLE_MODEL_INVOCATION_KEY) is True,
    )
    return skill, diagnostics


def _load_skills_from_dir(
    directory: str,
    *,
    include_root_files: bool,
    diagnostics: list[ResourceDiagnostic],
    seen_files: set[str],
    out: list[Skill],
) -> None:
    """Recursively collect skills from ``directory`` in place."""
    if not os.path.isdir(directory):
        return

    skill_file = os.path.join(directory, "SKILL.md")
    if _is_file(skill_file):
        # A directory holding SKILL.md *is* the skill root: recursion stops so
        # nested files are not re-registered as independent skills.
        _add_skill_file(skill_file, diagnostics=diagnostics, seen_files=seen_files, out=out)
        return

    try:
        entries = sorted(os.listdir(directory))
    except OSError as error:
        diagnostics.append(ResourceDiagnostic(DIAGNOSTIC_WARNING, str(error), directory))
        return

    for entry_name in entries:
        if entry_name.startswith(".") or entry_name in _SKIP_DIR_NAMES:
            continue
        full_path = os.path.join(directory, entry_name)
        if os.path.isdir(full_path):
            _load_skills_from_dir(
                full_path,
                include_root_files=False,
                diagnostics=diagnostics,
                seen_files=seen_files,
                out=out,
            )
        elif include_root_files and _is_file(full_path) and entry_name.endswith(".md"):
            _add_skill_file(full_path, diagnostics=diagnostics, seen_files=seen_files, out=out)


def _add_skill_file(
    file_path: str,
    *,
    diagnostics: list[ResourceDiagnostic],
    seen_files: set[str],
    out: list[Skill],
) -> None:
    real_path = os.path.realpath(file_path)
    if real_path in seen_files:
        return
    skill, file_diagnostics = _skill_from_file(file_path)
    diagnostics.extend(file_diagnostics)
    if skill is None:
        return
    seen_files.add(real_path)
    out.append(skill)


def discover_skills_with_diagnostics(dirs: Sequence[str]) -> SkillDiscoveryResult:
    """Discover skills from ``dirs`` (lowest precedence first) with diagnostics.

    Name collisions keep the **first** loaded skill, matching upstream where the
    winner is the earliest path in the resolved list; the loser is reported as a
    ``collision`` diagnostic.  Files reached twice through symlinks are ignored.
    """
    result = SkillDiscoveryResult()
    seen_files: set[str] = set()
    by_name: dict[str, Skill] = {}

    for directory in dirs:
        collected: list[Skill] = []
        _load_skills_from_dir(
            directory,
            include_root_files=True,
            diagnostics=result.diagnostics,
            seen_files=seen_files,
            out=collected,
        )
        for skill in collected:
            existing = by_name.get(skill.name)
            if existing is not None:
                result.diagnostics.append(
                    ResourceDiagnostic(
                        DIAGNOSTIC_COLLISION,
                        f'name "{skill.name}" collision',
                        skill.location,
                    )
                )
                continue
            by_name[skill.name] = skill

    result.skills = list(by_name.values())
    return result


def discover_skills(dirs: Sequence[str]) -> list[Skill]:
    """Discover skills from ``dirs`` (lowest precedence first).

    Convenience wrapper; use :func:`discover_skills_with_diagnostics` when the
    skip reasons matter.  Never raises for unreadable or invalid inputs.
    """
    return discover_skills_with_diagnostics(dirs).skills


def escape_xml(value: str) -> str:
    """Escape the five XML entities upstream escapes in skill metadata."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_skills_for_prompt(skills: Iterable[Skill], file_read_tool: str = "read") -> str:
    """Render the ``<available_skills>`` system-prompt block.

    Only metadata is emitted (name, description, location) so the prompt stays
    small; skills with ``disable_model_invocation`` are hidden and can only be
    triggered explicitly with ``/skill:<name>``.  Returns ``""`` when nothing is
    visible, in which case the caller appends nothing at all.
    """
    visible = [skill for skill in skills if not skill.disable_model_invocation]
    if not visible:
        return ""

    lines = [
        "",
        "",
        "The following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its description."
        if file_read_tool == "read"
        else "Use bash to load a skill's file when the task matches its description.",
        (
            "When a skill file references a relative path, resolve it against the skill directory "
            "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands."
        ),
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{escape_xml(skill.name)}</name>")
        lines.append(f"    <description>{escape_xml(skill.description)}</description>")
        lines.append(f"    <location>{escape_xml(skill.location)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def find_skill(name: str, skills: Iterable[Skill]) -> Skill | None:
    """Return the first skill named ``name`` (case-sensitive, like upstream)."""
    for skill in skills:
        if skill.name == name:
            return skill
    return None


def expand_skill_command(name: str, skills: Iterable[Skill], args: str = "") -> str | None:
    """Build the body injection used by the ``/skill:<name>`` command.

    The skill file is read, its frontmatter stripped, and the remainder wrapped
    in ``<skill name="..." location="...">`` so the model knows where relative
    references resolve.  Returns ``None`` for an unknown or unreadable skill so
    the caller can pass the original text through unchanged.
    """
    skill = find_skill(name, skills)
    if skill is None:
        return None
    try:
        body = strip_frontmatter(_read_text(skill.location)).strip()
    except (OSError, yaml.YAMLError):
        return None
    block = (
        f'<skill name="{skill.name}" location="{skill.location}">\n'
        f"References are relative to {skill.base_dir}.\n\n{body}\n</skill>"
    )
    return f"{block}\n\n{args}" if args else block
