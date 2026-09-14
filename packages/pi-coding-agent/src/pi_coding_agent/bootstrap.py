"""The startup sequence, shared by every entry point.

Both the CLI and the web server need exactly the same thing: resolve the working
directory and agent directory, load settings, decide project trust, discover
resources, load extensions, resolve a model against configured credentials, then
open (or create) a session.  Duplicating that would guarantee they drift, so it
lives here.

The one difference between entry points is ``with_runtime``: only the interactive
surfaces (RPC, web) can replace the active session, and only they pay for the
runtime layer.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from pi_ai import AuthStorage, FileCredentialStore, Model, ModelRegistry

from .agent_session import AgentSession
from .resource_loader import ResourceLoader
from .session_manager import SessionManager
from .settings import ProjectTrustStore, SettingsManager


@dataclass
class BootstrapOptions:
    cwd: str
    agent_dir: str | None = None
    model_pattern: str | None = None
    provider: str | None = None
    thinking: str | None = None
    api_key: str | None = None
    session_dir: str | None = None
    no_session: bool = False
    resume: str | None = None
    session_name: str | None = None
    extension_paths: Sequence[str] = ()
    no_extensions: bool = False
    no_skills: bool = False
    unknown_flags: Sequence[str] = ()
    host_mode: str = "print"
    with_runtime: bool = False


@dataclass
class Bootstrapped:
    """Everything an entry point needs after startup."""

    cwd: str
    agent_dir: str
    trusted: bool
    settings: SettingsManager
    loader: ResourceLoader
    auth: AuthStorage
    registry: ModelRegistry
    model: Model
    thinking_level: str
    session_manager: SessionManager
    extension_runner: Any = None
    runtime: Any = None
    session: AgentSession | None = None
    host_mode: str = "print"
    warnings: list[str] = field(default_factory=list)

    async def start(self) -> None:
        """Bind extensions to the session and announce ``session_start``."""
        if self.extension_runner is None:
            return
        from .extensions import ExtensionHost

        self.extension_runner.bind_host(
            ExtensionHost(
                cwd=self.cwd,
                mode=self.host_mode,
                has_ui=False,
                is_project_trusted=self.trusted,
                session=self.session,
                runtime=self.runtime,
            )
        )
        await self.session.emit_session_start("startup")

    async def shutdown(self, reason: str = "quit") -> None:
        if self.extension_runner is not None and self.session is not None:
            await self.session.emit_session_shutdown(reason)
        if self.runtime is not None:
            await self.runtime.dispose()
        elif self.session is not None:
            self.session.dispose()


# --------------------------------------------------------------------------------------
# Trust
# --------------------------------------------------------------------------------------


def resolve_project_trust(cwd: str, agent_dir: str, settings: SettingsManager) -> bool:
    """Decide project trust without a UI.

    An explicit stored decision wins; otherwise ``defaultProjectTrust`` decides.
    ``ask`` cannot be honoured non-interactively, so it declines and says so —
    failing closed is the safe direction for a setting that gates code execution.
    """
    canonical = os.path.realpath(os.path.abspath(os.path.expanduser(cwd)))
    stored = ProjectTrustStore(agent_dir).read()
    for candidate in (canonical, *_ancestors(canonical)):
        if candidate in stored:
            decision = stored[candidate]
            if decision is not None:
                return bool(decision)

    default = settings.get_default_project_trust()
    if default == "always":
        return True
    if default == "never":
        return False
    print(
        f"pi-py: project {canonical} is untrusted and trust cannot be requested "
        "non-interactively; project-local resources are disabled.",
        file=sys.stderr,
    )
    return False


def _ancestors(path: str) -> list[str]:
    parents: list[str] = []
    current = os.path.dirname(path)
    while current and current != os.path.dirname(current):
        parents.append(os.path.realpath(current))
        current = os.path.dirname(current)
    return parents


# --------------------------------------------------------------------------------------
# Model selection
# --------------------------------------------------------------------------------------


def select_model(
    registry: ModelRegistry,
    *,
    pattern: str | None,
    provider: str | None,
    settings: SettingsManager,
):
    """Resolve the model to use, or raise ``SystemExit`` with an actionable message."""
    if pattern and provider and "/" not in pattern:
        pattern = f"{provider}/{pattern}"
    if not pattern:
        default_model = settings.get_default_model()
        default_provider = settings.get_default_provider()
        if default_model and default_provider:
            pattern = f"{default_provider}/{default_model}"
        elif default_model:
            pattern = default_model

    if pattern:
        matches = registry.resolve(pattern)
        if not matches:
            available = registry.list_models()
            raise SystemExit(
                f"pi-py: no model matched {pattern!r}. Known ids include: "
                + ", ".join(f"{m.provider}/{m.id}" for m in available[:8])
            )
        return matches[0]

    available = [m for m in registry.list_models() if registry.has_configured_auth(m.provider)]
    if not available:
        raise SystemExit(
            "pi-py: no model with configured credentials. Set an API key "
            "(e.g. ANTHROPIC_API_KEY) or pass an explicit model and api key."
        )
    return registry.resolve(f"{available[0].provider}/{available[0].id}")[0]


# --------------------------------------------------------------------------------------
# Extensions
# --------------------------------------------------------------------------------------


def extension_flag_values(unknown: Sequence[str]) -> dict[str, Any]:
    """Map unrecognised ``--name``/``--name=value`` tokens onto extension flags."""
    values: dict[str, Any] = {}
    for token in unknown:
        if not token.startswith("--"):
            continue
        name, separator, raw = token[2:].partition("=")
        if not name:
            continue
        values[name] = raw if separator else True
    return values


def load_extension_runner(
    *,
    cwd: str,
    agent_dir: str,
    trusted: bool,
    extra_paths: Sequence[str],
    unknown_flags: Sequence[str],
    disabled: bool,
    on_diagnostic: Callable[[str], None] | None = None,
):
    """Discover and load extensions; returns a runner, or None when there are none.

    Never raises: a broken extension becomes a diagnostic and the session still starts.
    """
    if disabled:
        return None

    from .extensions import ExtensionRunner, load_extensions, resolve_extension_paths

    paths = resolve_extension_paths(
        cwd=cwd, agent_dir=agent_dir, project_trusted=trusted, extra_paths=extra_paths
    )
    result = load_extensions(paths, flag_values=extension_flag_values(unknown_flags))
    for diagnostic in result.diagnostics:
        message = f"extension {diagnostic.severity}: {diagnostic.path}: {diagnostic.message}"
        if on_diagnostic is not None:
            on_diagnostic(message)
        else:
            print(f"pi-py: {message}", file=sys.stderr)
    if not result.apis:
        return None
    return ExtensionRunner(result.apis)


# --------------------------------------------------------------------------------------
# The whole sequence
# --------------------------------------------------------------------------------------


async def bootstrap(
    options: BootstrapOptions,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> Bootstrapped:
    """Run the startup sequence and return a ready-to-use session."""
    cwd = os.path.abspath(os.path.expanduser(options.cwd))
    agent_dir = os.path.abspath(os.path.expanduser(options.agent_dir)) if options.agent_dir else None

    settings = SettingsManager(cwd, agent_dir, project_trusted=False)
    settings.drain_errors()  # Invalid settings are surfaced by the manager, not fatal.
    effective_agent_dir = settings.agent_dir

    auth = AuthStorage(FileCredentialStore(os.path.join(effective_agent_dir, "auth.json")))
    if options.api_key:
        auth.set_runtime_api_key(options.provider or "", options.api_key)

    registry = ModelRegistry(auth, models_path=os.path.join(effective_agent_dir, "models.json"))

    trusted = resolve_project_trust(cwd, effective_agent_dir, settings)
    settings.set_project_trusted(trusted)
    loader = ResourceLoader(
        cwd, effective_agent_dir, project_trusted=trusted, skills_enabled=not options.no_skills
    )

    def _warn(message: str) -> None:
        if on_warning is not None:
            on_warning(message)
        else:
            print(f"pi-py: {message}", file=sys.stderr)

    extension_runner = load_extension_runner(
        cwd=cwd,
        agent_dir=effective_agent_dir,
        trusted=trusted,
        extra_paths=tuple(options.extension_paths),
        unknown_flags=tuple(options.unknown_flags),
        disabled=options.no_extensions,
        on_diagnostic=_warn,
    )

    resolved = select_model(
        registry,
        pattern=options.model_pattern,
        provider=options.provider,
        settings=settings,
    )
    model = resolved.model
    thinking_level = (
        options.thinking or resolved.thinking_level or settings.get("thinkingLevel") or "off"
    )

    if options.api_key and not options.provider:
        auth.set_runtime_api_key(model.provider, options.api_key)

    if not auth.has_auth(model.provider):
        raise SystemExit(
            f"pi-py: no credentials for provider {model.provider!r}. "
            f"Set its API key in the environment or in {effective_agent_dir}/auth.json."
        )

    if options.no_session:
        session_manager = SessionManager.in_memory(cwd)
    elif options.resume:
        session_manager = SessionManager.open(options.resume, cwd=cwd)
    elif options.session_dir:
        session_manager = SessionManager.create(cwd, session_dir=options.session_dir)
    else:
        session_manager = SessionManager.create(cwd, effective_agent_dir)

    def build_session(manager: SessionManager) -> AgentSession:
        return AgentSession(
            model=model,
            cwd=cwd,
            session_manager=manager,
            settings_manager=settings,
            resource_loader=loader,
            auth_storage=auth,
            thinking_level=thinking_level,
            extension_runner=extension_runner,
        )

    runtime = None
    if options.with_runtime:
        from .agent_session_runtime import create_agent_session_runtime

        async def factory(*, cwd, agent_dir, session_manager, session_start_event=None):
            del cwd, agent_dir, session_start_event
            return build_session(session_manager)

        runtime = await create_agent_session_runtime(
            factory,
            cwd=cwd,
            agent_dir=effective_agent_dir,
            session_manager=session_manager,
            extension_runner=extension_runner,
        )
        session = runtime.session
    else:
        session = build_session(session_manager)

    if options.session_name:
        await session_manager.set_session_name(options.session_name)

    result = Bootstrapped(
        cwd=cwd,
        agent_dir=effective_agent_dir,
        trusted=trusted,
        settings=settings,
        loader=loader,
        auth=auth,
        registry=registry,
        model=model,
        thinking_level=thinking_level,
        session_manager=session_manager,
        extension_runner=extension_runner,
        runtime=runtime,
        session=session,
        host_mode=options.host_mode,
        warnings=[str(diagnostic) for diagnostic in loader.diagnostics()],
    )
    await result.start()
    return result


__all__ = [
    "BootstrapOptions",
    "Bootstrapped",
    "bootstrap",
    "extension_flag_values",
    "load_extension_runner",
    "resolve_project_trust",
    "select_model",
]
