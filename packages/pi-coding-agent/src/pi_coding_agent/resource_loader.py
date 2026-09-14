"""Resource discovery for the coding agent.

Port of ``packages/coding-agent/src/core/resource-loader.ts`` (upstream pi).

What is discovered
------------------
* **context files** -- ``AGENTS.override.md`` / ``AGENTS.md`` / ``AGENTS.MD`` /
  ``CLAUDE.md`` / ``CLAUDE.MD``, first hit per directory, global first and then
  from the outermost ancestor down to the cwd (so the nearest project file wins
  last in the prompt);
* **system prompt overrides** -- ``SYSTEM.md`` replaces the default prompt and
  ``APPEND_SYSTEM.md`` is appended;
* **skills** -- Agent Skills directories (see :mod:`pi_coding_agent.skills`);
* **prompt templates** -- ``*.md`` files with optional frontmatter;
* **extensions** -- discovered (not executed) for a later version.

Discovery precedence (low -> high)
----------------------------------
The same order upstream resolves in ``DefaultPackageManager.resolve()`` and the
note documents: user ``~/.agents/skills``, then ``<agent_dir>/skills``, then the
ancestors' ``.agents/skills`` (nearest first, ``$HOME`` excluded because it is
the user-level directory already covered), then ``<config_dir>/skills``.

Trust gating
------------
Every project-local resource -- ``.pi/{SYSTEM.md,APPEND_SYSTEM.md,skills,prompts,
extensions,settings.json}`` and project ``.agents/skills`` -- is loaded only
when the project is trusted.  This keeps a freshly cloned repository from
injecting instructions or extension code before the user approves it.  Context
files are *not* gated: upstream treats ``AGENTS.md`` as ordinary readable
context.  ``reload(resolve_project_trust=...)`` implements the upstream
two-phase load: a global-only pass first so the trust decision can be made
without project code, then the final pass with the decision applied.
"""

from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from .skills import ResourceDiagnostic, Skill, discover_skills_with_diagnostics, parse_frontmatter
from .system_prompt import ContextFile

#: Default project config directory name, mirroring upstream ``CONFIG_DIR_NAME``.
DEFAULT_CONFIG_DIR_NAME = ".pi"

#: Environment variable that relocates the user-level agent directory.
AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"

#: Context file candidates, in the order upstream probes them per directory.
CONTEXT_FILE_CANDIDATES: tuple[str, ...] = (
    "AGENTS.override.md",
    "AGENTS.md",
    "AGENTS.MD",
    "CLAUDE.md",
    "CLAUDE.MD",
)

#: Prompt template description line truncation, matching upstream.
_DESCRIPTION_MAX_LENGTH = 60

#: Extension file/dir names recognised by the v0.1 discovery pass.
_EXTENSION_FILE_SUFFIXES = (".ts", ".py")
_EXTENSION_INDEX_NAMES = ("index.ts", "index.py")


@dataclass(frozen=True)
class PromptTemplate:
    """A ``/*.md`` prompt template usable as ``/name args``."""

    name: str
    description: str
    content: str
    location: str
    argument_hint: str | None = None


@dataclass
class _LoadedResources:
    """Mutable snapshot produced by one load pass."""

    context_files: list[ContextFile] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    prompt_templates: list[PromptTemplate] = field(default_factory=list)
    extension_paths: list[str] = field(default_factory=list)
    system_prompt_override: str | None = None
    system_prompt_source: str | None = None
    append_system_prompt: list[str] = field(default_factory=list)
    append_system_prompt_sources: list[str] = field(default_factory=list)
    diagnostics: list[ResourceDiagnostic] = field(default_factory=list)


def get_agent_dir() -> str:
    """User-level agent directory.

    ``PI_CODING_AGENT_DIR`` wins so tests and alternative installs can point at
    a sandbox; otherwise it is ``~/.pi/agent`` like upstream.
    """
    override = os.environ.get(AGENT_DIR_ENV)
    if override and override.strip():
        return os.path.abspath(os.path.expanduser(override.strip()))
    return os.path.join(os.path.abspath(os.path.expanduser("~")), DEFAULT_CONFIG_DIR_NAME, "agent")


def find_config_dir(cwd: str) -> str:
    """Project config directory *name* for ``cwd``.

    ``package.json`` may rename it with ``piConfig.configDir`` (upstream reads
    the same field from its own package metadata); anything missing, malformed
    or non-string falls back to ``.pi``.
    """
    package_json = os.path.join(cwd, "package.json")
    try:
        with open(package_json, "r", encoding="utf-8", errors="replace") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError):
        return DEFAULT_CONFIG_DIR_NAME

    if isinstance(parsed, dict):
        pi_config = parsed.get("piConfig")
        if isinstance(pi_config, dict):
            config_dir = pi_config.get("configDir")
            if isinstance(config_dir, str) and config_dir.strip():
                return config_dir.strip()
    return DEFAULT_CONFIG_DIR_NAME


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def load_context_file_from_dir(directory: str) -> ContextFile | None:
    """First existing candidate context file directly inside ``directory``."""
    for filename in CONTEXT_FILE_CANDIDATES:
        candidate = os.path.join(directory, filename)
        if not os.path.isfile(candidate):
            continue
        content = _read_text(candidate)
        if content is None:
            continue
        return ContextFile(path=candidate, content=content)
    return None


def load_project_context_files(cwd: str, agent_dir: str) -> list[ContextFile]:
    """Load global and ancestor context files in prompt order.

    The global ``agent_dir`` file comes first, then ancestors from the outermost
    down to the cwd-nearest, so a nearer ``AGENTS.md`` can override a broader
    one by appearing later.  Duplicate paths are collapsed.
    """
    resolved_cwd = os.path.abspath(cwd)
    resolved_agent_dir = os.path.abspath(agent_dir)

    files: list[ContextFile] = []
    seen: set[str] = set()

    global_context = load_context_file_from_dir(resolved_agent_dir)
    if global_context is not None:
        files.append(global_context)
        seen.add(global_context.path)

    ancestor_files: list[ContextFile] = []
    current = resolved_cwd
    while True:
        context_file = load_context_file_from_dir(current)
        if context_file is not None and context_file.path not in seen:
            # Prepend so the outermost ancestor ends up before the nearest one.
            ancestor_files.insert(0, context_file)
            seen.add(context_file.path)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    files.extend(ancestor_files)
    return files


def find_git_repo_root(start_dir: str) -> str | None:
    """Nearest ancestor containing ``.git``, or ``None`` outside a repository."""
    current = os.path.abspath(start_dir)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def collect_ancestor_agents_skill_dirs(cwd: str) -> list[str]:
    """``<dir>/.agents/skills`` for cwd and its ancestors, nearest first.

    The walk stops at the git repository root when one exists, matching the
    upstream boundary for project-level Agent Skills.
    """
    directories: list[str] = []
    resolved_cwd = os.path.abspath(cwd)
    repo_root = find_git_repo_root(resolved_cwd)

    current = resolved_cwd
    while True:
        directories.append(os.path.join(current, ".agents", "skills"))
        if repo_root is not None and current == repo_root:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return directories


def _load_prompt_template(path: str) -> PromptTemplate | None:
    """Parse one ``*.md`` prompt template; ``None`` when unreadable."""
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        frontmatter, body = parse_frontmatter(raw)
    except Exception:
        return None

    name = os.path.basename(path)
    if name.endswith(".md"):
        name = name[:-3]

    description_value = frontmatter.get("description")
    description = description_value if isinstance(description_value, str) else ""
    if not description:
        first_line = next((line for line in body.split("\n") if line.strip()), "")
        if first_line:
            description = first_line[:_DESCRIPTION_MAX_LENGTH]
            if len(first_line) > _DESCRIPTION_MAX_LENGTH:
                description += "..."

    argument_hint_value = frontmatter.get("argument-hint")
    argument_hint = argument_hint_value if isinstance(argument_hint_value, str) else None

    return PromptTemplate(
        name=name,
        description=description,
        content=body,
        location=os.path.abspath(path),
        argument_hint=argument_hint,
    )


def _load_prompt_templates_from_dir(directory: str) -> list[PromptTemplate]:
    """Non-recursive ``*.md`` scan of ``directory``."""
    if not os.path.isdir(directory):
        return []
    templates: list[PromptTemplate] = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    for entry_name in entries:
        if entry_name.startswith("."):
            continue
        path = os.path.join(directory, entry_name)
        if os.path.isfile(path) and entry_name.endswith(".md"):
            template = _load_prompt_template(path)
            if template is not None:
                templates.append(template)
    return templates


def collect_extension_paths(directory: str) -> list[str]:
    """Discover extension entry points one level below ``directory``.

    v0.1 records paths only -- nothing is imported or executed.  Supported
    shapes are ``foo.ts`` / ``foo.py`` and directories with ``index.ts`` /
    ``index.py``.
    """
    if not os.path.isdir(directory):
        return []
    paths: list[str] = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    for entry_name in entries:
        if entry_name.startswith(".") or entry_name == "node_modules":
            continue
        full_path = os.path.join(directory, entry_name)
        if os.path.isfile(full_path) and entry_name.endswith(_EXTENSION_FILE_SUFFIXES):
            paths.append(os.path.abspath(full_path))
        elif os.path.isdir(full_path):
            for index_name in _EXTENSION_INDEX_NAMES:
                index_path = os.path.join(full_path, index_name)
                if os.path.isfile(index_path):
                    paths.append(os.path.abspath(index_path))
                    break
    return paths


def _dedupe_paths(paths: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication on canonical paths."""
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        canonical = os.path.realpath(path)
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append(path)
    return unique


class ResourceLoader:
    """Discovers context files, prompt overrides, skills, prompts, extensions.

    The loader is synchronous at rest: construction performs one load pass, and
    read accessors return the cached snapshot.  Call :meth:`reload` (or
    :meth:`set_project_trusted`) after the trust decision to pick up or drop
    project-local resources.
    """

    def __init__(
        self,
        cwd: str,
        agent_dir: str | None = None,
        project_trusted: bool = False,
        *,
        skills_enabled: bool = True,
    ) -> None:
        self._cwd = os.path.abspath(os.path.expanduser(cwd))
        self._agent_dir = (
            os.path.abspath(os.path.expanduser(agent_dir)) if agent_dir else get_agent_dir()
        )
        self._project_trusted = bool(project_trusted)
        #: Skill discovery is unconditional in pi (with per-path overrides in
        #: settings, which this port does not implement yet).  A machine-wide
        #: ``~/.agents/skills`` shared with other agent tooling can add tens of
        #: thousands of characters to *every* system prompt, so it needs an off
        #: switch that does not require editing anyone's settings.
        self._skills_enabled = bool(skills_enabled)
        self._config_dir_name = find_config_dir(self._cwd)
        self._snapshot = _LoadedResources()
        self.load()

    # -- accessors ---------------------------------------------------------------

    @property
    def cwd(self) -> str:
        """Absolute working directory this loader was created for."""
        return self._cwd

    @property
    def agent_dir(self) -> str:
        """Absolute user-level agent directory."""
        return self._agent_dir

    @property
    def config_dir_name(self) -> str:
        """Project config directory name (``.pi`` unless package.json overrides)."""
        return self._config_dir_name

    @property
    def config_dir(self) -> str:
        """Absolute project config directory path."""
        return os.path.join(self._cwd, self._config_dir_name)

    def is_project_trusted(self) -> bool:
        """Whether project-local resources are currently loaded."""
        return self._project_trusted

    def context_files(self) -> list[ContextFile]:
        """Context files in prompt order (global -> outermost -> nearest)."""
        return list(self._snapshot.context_files)

    def skills(self) -> list[Skill]:
        """Discovered skills, in discovery precedence order."""
        return list(self._snapshot.skills)

    def prompt_templates(self) -> list[PromptTemplate]:
        """Discovered ``*.md`` prompt templates."""
        return list(self._snapshot.prompt_templates)

    def extension_paths(self) -> list[str]:
        """Discovered extension entry points (never executed)."""
        return list(self._snapshot.extension_paths)

    def system_prompt_override(self) -> str | None:
        """Contents of ``SYSTEM.md``, or ``None`` to use the default prompt."""
        return self._snapshot.system_prompt_override

    def system_prompt_source(self) -> str | None:
        """Path of the ``SYSTEM.md`` that supplied the override, if any."""
        return self._snapshot.system_prompt_source

    def append_system_prompt(self) -> list[str]:
        """``APPEND_SYSTEM.md`` contents, project first then global."""
        return list(self._snapshot.append_system_prompt)

    def append_system_prompt_sources(self) -> list[str]:
        """Paths backing :meth:`append_system_prompt`."""
        return list(self._snapshot.append_system_prompt_sources)

    def diagnostics(self) -> list[ResourceDiagnostic]:
        """Non-fatal problems from the most recent load pass."""
        return list(self._snapshot.diagnostics)

    # -- loading -----------------------------------------------------------------

    def set_project_trusted(self, trusted: bool, *, reload: bool = True) -> None:
        """Update project trust, reloading so gated resources appear or vanish."""
        self._project_trusted = bool(trusted)
        if reload:
            self.load()

    def load(self) -> None:
        """Run one discovery pass and replace the cached snapshot."""
        self._snapshot = self._discover()

    async def reload(
        self,
        resolve_project_trust: Callable[["ResourceLoader"], bool | Awaitable[bool]] | None = None,
    ) -> None:
        """Re-run discovery, optionally resolving project trust first.

        Two-phase like upstream: when ``resolve_project_trust`` is given, a
        global-only pass runs first so the decision never depends on project
        code, then the callback's verdict is applied and discovery runs again.
        """
        if resolve_project_trust is not None:
            self._project_trusted = False
            self.load()
            decision = resolve_project_trust(self)
            if inspect.isawaitable(decision):
                decision = await decision
            self._project_trusted = bool(decision)
        self.load()

    def _discover(self) -> _LoadedResources:
        snapshot = _LoadedResources()
        snapshot.context_files = load_project_context_files(self._cwd, self._agent_dir)
        self._load_skills(snapshot)
        self._load_prompts(snapshot)
        self._load_extensions(snapshot)
        self._load_system_prompt(snapshot)
        return snapshot

    def _load_skills(self, snapshot: _LoadedResources) -> None:
        if not self._skills_enabled:
            return

        dirs: list[str] = []
        home = os.path.abspath(os.path.expanduser("~"))

        # User-level Agent Skills convention: always trusted, never gated.
        dirs.append(os.path.join(home, ".agents", "skills"))
        dirs.append(os.path.join(self._agent_dir, "skills"))

        if self._project_trusted:
            user_agents_dir = os.path.realpath(os.path.join(home, ".agents", "skills"))
            for ancestors_dir in collect_ancestor_agents_skill_dirs(self._cwd):
                if os.path.realpath(ancestors_dir) == user_agents_dir:
                    continue
                dirs.append(ancestors_dir)
            dirs.append(os.path.join(self.config_dir, "skills"))

        result = discover_skills_with_diagnostics(_dedupe_paths(dirs))
        snapshot.skills = result.skills
        snapshot.diagnostics.extend(result.diagnostics)

    def _load_prompts(self, snapshot: _LoadedResources) -> None:
        dirs = [os.path.join(self._agent_dir, "prompts")]
        if self._project_trusted:
            dirs.append(os.path.join(self.config_dir, "prompts"))

        seen_names: set[str] = set()
        for directory in dirs:
            for template in _load_prompt_templates_from_dir(directory):
                if template.name in seen_names:
                    snapshot.diagnostics.append(
                        ResourceDiagnostic("collision", f'name "/{template.name}" collision', template.location)
                    )
                    continue
                seen_names.add(template.name)
                snapshot.prompt_templates.append(template)

    def _load_extensions(self, snapshot: _LoadedResources) -> None:
        dirs = [os.path.join(self._agent_dir, "extensions")]
        if self._project_trusted:
            dirs.append(os.path.join(self.config_dir, "extensions"))
        paths: list[str] = []
        for directory in dirs:
            paths.extend(collect_extension_paths(directory))
        snapshot.extension_paths = _dedupe_paths(paths)

    def _load_system_prompt(self, snapshot: _LoadedResources) -> None:
        project_system = os.path.join(self.config_dir, "SYSTEM.md")
        global_system = os.path.join(self._agent_dir, "SYSTEM.md")

        source: str | None = None
        if self._project_trusted and os.path.isfile(project_system):
            source = project_system
        elif os.path.isfile(global_system):
            source = global_system

        if source is not None:
            content = _read_text(source)
            if content is None:
                snapshot.diagnostics.append(
                    ResourceDiagnostic("warning", "Could not read system prompt file", source)
                )
            else:
                snapshot.system_prompt_override = content
                snapshot.system_prompt_source = source

        # APPEND_SYSTEM.md layers on top of whichever prompt is active.  The
        # project file is listed first so it is appended before the global one.
        append_sources: list[str] = []
        if self._project_trusted:
            project_append = os.path.join(self.config_dir, "APPEND_SYSTEM.md")
            if os.path.isfile(project_append):
                append_sources.append(project_append)
        global_append = os.path.join(self._agent_dir, "APPEND_SYSTEM.md")
        if os.path.isfile(global_append):
            append_sources.append(global_append)

        for append_source in append_sources:
            content = _read_text(append_source)
            if content is None:
                snapshot.diagnostics.append(
                    ResourceDiagnostic("warning", "Could not read append system prompt file", append_source)
                )
                continue
            snapshot.append_system_prompt.append(content)
            snapshot.append_system_prompt_sources.append(append_source)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ResourceLoader(cwd={self._cwd!r}, agent_dir={self._agent_dir!r}, "
            f"project_trusted={self._project_trusted})"
        )
