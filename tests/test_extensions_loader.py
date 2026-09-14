"""Tests for Python-native extension discovery and loading.

Covers the contract in ``pi_coding_agent.extensions.loader``: pi-faithful path
resolution (including the project trust gate), isolated module loading under
synthetic names, the sync/async factory contract, entry-point discovery, and the
promise that every failure is a diagnostic instead of an exception.
"""

from __future__ import annotations

import importlib.metadata
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from pi_coding_agent.extensions import (
    ExtensionAPI,
    ExtensionRunner,
    ToolCallEvent,
    ToolCallEventResult,
)
from pi_coding_agent.extensions.loader import (
    ENTRY_POINT_GROUP,
    ExtensionDiagnostic,
    ExtensionLoadResult,
    load_extensions,
    resolve_extension_paths,
)

MINIMAL_SOURCE = """
from pi_coding_agent.extensions import ExtensionAPI


def setup(pi: ExtensionAPI) -> None:
    pi.register_flag("loaded", "Registered when the extension loaded", default=True)
"""

ASYNC_SOURCE = """
import asyncio

from pi_coding_agent.extensions import ExtensionAPI


async def setup(pi: ExtensionAPI) -> None:
    await asyncio.sleep(0)
    pi.register_flag("async_loaded", "Registered by an async factory", default=True)
"""

BLOCKER_SOURCE = """
from pi_coding_agent.extensions import ToolCallEventResult


def setup(pi):
    pi.register_flag("owner", "Which extension ran", default="{owner}")

    def block(event, ctx):
        return ToolCallEventResult(block=True, reason="{owner}")

    pi.on("tool_call", block)
"""


def write_module(path: Path, source: str) -> Path:
    """Write a dedented module, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return path


def resolve(tmp_path: Path, *, agent_dir: Path | None = None, extra_paths=(), trusted: bool = False):
    return resolve_extension_paths(
        cwd=str(tmp_path),
        agent_dir=str(agent_dir if agent_dir is not None else tmp_path / "agent"),
        project_trusted=trusted,
        extra_paths=extra_paths,
    )


# --------------------------------------------------------------------------------------
# Loading: happy paths
# --------------------------------------------------------------------------------------


def test_valid_sync_extension_loads_and_registrations_land(tmp_path: Path) -> None:
    ext = write_module(
        tmp_path / "greet.py",
        """
        from pi_coding_agent.extensions import ExtensionAPI


        def _on_start(event, ctx):
            return None


        def setup(pi: ExtensionAPI) -> None:
            pi.register_flag("greeting", "How to greet", default="hello")
            pi.register_command("hello", "Say hello", lambda args, ctx: "hi")
            pi.on("session_start", _on_start)
        """,
    )

    result = load_extensions([str(ext)], discover_entry_points=False)

    assert result.diagnostics == []
    assert result.paths == [str(ext)]
    assert len(result.apis) == 1
    api = result.apis[0]
    assert api.path == str(ext)
    assert api.get_flag("greeting") == "hello"
    assert set(api.commands) == {"hello"}
    assert len(api.handlers["session_start"]) == 1


def test_async_factory_loads_from_sync_caller(tmp_path: Path) -> None:
    # A sync test has no running loop, so the loader may drive the coroutine.
    ext = write_module(tmp_path / "async_ext.py", ASYNC_SOURCE)

    result = load_extensions([str(ext)], discover_entry_points=False)

    assert result.diagnostics == []
    assert len(result.apis) == 1
    assert result.apis[0].get_flag("async_loaded") is True


async def test_async_factory_under_a_running_loop_is_a_diagnostic(tmp_path: Path) -> None:
    ext = write_module(tmp_path / "async_ext.py", ASYNC_SOURCE)

    result = load_extensions([str(ext)], discover_entry_points=False)

    assert result.apis == []
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.path == str(ext)
    assert diagnostic.severity == "error"
    assert "event loop" in diagnostic.message
    assert "load_extensions()" in diagnostic.message


# --------------------------------------------------------------------------------------
# Factory resolution
# --------------------------------------------------------------------------------------


def test_factory_attribute_precedence_setup_then_extension_then_register(tmp_path: Path) -> None:
    all_three = write_module(
        tmp_path / "all_three.py",
        """
        def setup(pi):
            pi.register_flag("chosen", "which factory ran", default="setup")


        def extension(pi):
            pi.register_flag("chosen", "which factory ran", default="extension")


        def register(pi):
            pi.register_flag("chosen", "which factory ran", default="register")
        """,
    )
    extension_and_register = write_module(
        tmp_path / "two.py",
        """
        def extension(pi):
            pi.register_flag("chosen", "which factory ran", default="extension")


        def register(pi):
            pi.register_flag("chosen", "which factory ran", default="register")
        """,
    )
    register_only = write_module(
        tmp_path / "one.py",
        """
        def register(pi):
            pi.register_flag("chosen", "which factory ran", default="register")
        """,
    )

    result = load_extensions(
        [str(all_three), str(extension_and_register), str(register_only)],
        discover_entry_points=False,
    )

    assert result.diagnostics == []
    assert [api.get_flag("chosen") for api in result.apis] == ["setup", "extension", "register"]


def test_module_without_factory_is_skipped_with_a_diagnostic(tmp_path: Path) -> None:
    no_factory = write_module(tmp_path / "nofactory.py", "VALUE = 1\n")
    good = write_module(tmp_path / "good.py", MINIMAL_SOURCE)

    result = load_extensions([str(no_factory), str(good)], discover_entry_points=False)

    assert [api.path for api in result.apis] == [str(good)]
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].path == str(no_factory)
    assert "setup" in result.diagnostics[0].message


# --------------------------------------------------------------------------------------
# Isolation and failure handling
# --------------------------------------------------------------------------------------


def test_same_named_files_in_different_directories_do_not_clobber(tmp_path: Path) -> None:
    first = write_module(
        tmp_path / "a" / "index.py",
        """
        def setup(pi):
            pi.register_flag("which", "which file", default="a")
            pi.register_flag("module_name", "synthetic module name", default=__name__)
        """,
    )
    second = write_module(
        tmp_path / "b" / "index.py",
        """
        def setup(pi):
            pi.register_flag("which", "which file", default="b")
            pi.register_flag("module_name", "synthetic module name", default=__name__)
        """,
    )

    result = load_extensions([str(first), str(second)], discover_entry_points=False)

    assert result.diagnostics == []
    assert len(result.apis) == 2
    assert {api.get_flag("which") for api in result.apis} == {"a", "b"}
    module_names = {api.get_flag("module_name") for api in result.apis}
    assert len(module_names) == 2
    assert all(str(name).startswith("pi_extension_") for name in module_names)


def test_raising_factory_does_not_abort_other_extensions(tmp_path: Path) -> None:
    bad = write_module(
        tmp_path / "bad.py",
        """
        def setup(pi):
            raise ValueError("boom")
        """,
    )
    good = write_module(tmp_path / "good.py", MINIMAL_SOURCE)

    result = load_extensions([str(bad), str(good)], discover_entry_points=False)

    assert [api.path for api in result.apis] == [str(good)]
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].path == str(bad)
    assert "boom" in result.diagnostics[0].message
    assert result.diagnostics[0].severity == "error"


def test_syntax_error_is_a_diagnostic_and_leaves_no_module_behind(tmp_path: Path) -> None:
    broken = write_module(tmp_path / "broken.py", "def setup(pi:\n")

    first = load_extensions([str(broken)], discover_entry_points=False)
    second = load_extensions([str(broken)], discover_entry_points=False)

    assert first.apis == [] and second.apis == []
    for result in (first, second):
        assert len(result.diagnostics) == 1
        assert "SyntaxError" in result.diagnostics[0].message


def test_every_failure_mode_returns_a_diagnostic_instead_of_raising(tmp_path: Path) -> None:
    syntax = write_module(tmp_path / "broken.py", "def setup(pi:\n")
    import_failure = write_module(tmp_path / "import_failure.py", "raise RuntimeError('import boom')\n")
    no_factory = write_module(tmp_path / "nofactory.py", "VALUE = 1\n")
    raising = write_module(tmp_path / "raising.py", "def setup(pi):\n    raise RuntimeError('kaboom')\n")
    missing = tmp_path / "missing.py"
    a_directory = tmp_path / "a_directory"
    a_directory.mkdir()

    result = load_extensions(
        [str(syntax), str(import_failure), str(no_factory), str(raising), str(missing), str(a_directory)],
        discover_entry_points=False,
    )

    assert result.apis == []
    assert len(result.diagnostics) == 6
    assert all(diagnostic.severity == "error" for diagnostic in result.diagnostics)
    by_path = {diagnostic.path: diagnostic.message for diagnostic in result.diagnostics}
    assert "import boom" in by_path[str(import_failure)]
    assert "kaboom" in by_path[str(raising)]
    assert "does not exist" in by_path[str(missing)]
    assert "directory" in by_path[str(a_directory)]


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


def test_duplicate_paths_are_deduplicated_by_realpath(tmp_path: Path) -> None:
    ext = write_module(tmp_path / "dup.py", MINIMAL_SOURCE)
    agent_dir = tmp_path / "agent"

    discovered = resolve_extension_paths(
        cwd=str(tmp_path),
        agent_dir=str(agent_dir),
        project_trusted=False,
        extra_paths=[str(ext), str(ext)],
    )
    assert discovered == [str(ext)]

    result = load_extensions([str(ext), str(ext)], discover_entry_points=False)
    assert len(result.apis) == 1
    assert result.paths == [str(ext)]


def test_project_extensions_are_gated_on_trust(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    global_ext = write_module(agent_dir / "extensions" / "global_ext.py", MINIMAL_SOURCE)
    project_ext = write_module(tmp_path / ".pi" / "extensions" / "project_ext.py", MINIMAL_SOURCE)

    untrusted = resolve_extension_paths(
        cwd=str(tmp_path), agent_dir=str(agent_dir), project_trusted=False
    )
    assert untrusted == [str(global_ext)]

    trusted = resolve_extension_paths(cwd=str(tmp_path), agent_dir=str(agent_dir), project_trusted=True)
    # Project-local first, then global (upstream discoverAndLoadExtensions order).
    assert trusted == [str(project_ext), str(global_ext)]


async def test_project_extension_outranks_global_extension_in_load_order(tmp_path: Path) -> None:
    """Regression: project-local extensions are discovered before global ones.

    Load order is precedence: ``emit_tool_call`` stops at the first handler that
    blocks, so the project extension's block must win over the global one.
    """
    agent_dir = tmp_path / "agent"
    global_ext = write_module(
        agent_dir / "extensions" / "global_ext.py", BLOCKER_SOURCE.format(owner="global")
    )
    project_ext = write_module(
        tmp_path / ".pi" / "extensions" / "project_ext.py", BLOCKER_SOURCE.format(owner="project")
    )

    paths = resolve_extension_paths(
        cwd=str(tmp_path), agent_dir=str(agent_dir), project_trusted=True
    )
    assert paths == [str(project_ext), str(global_ext)]

    result = load_extensions(paths, discover_entry_points=False)
    assert result.diagnostics == []
    assert [api.get_flag("owner") for api in result.apis] == ["project", "global"]

    runner = ExtensionRunner(result.apis)
    outcome = await runner.emit_tool_call(ToolCallEvent(tool_name="bash", tool_call_id="call-1"))
    assert outcome is not None
    assert outcome.block is True
    assert outcome.reason == "project"


def test_package_directory_form_is_discovered_and_loads(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    package = agent_dir / "extensions" / "pkg_ext"
    write_module(package / "helper.py", "VALUE = 'from-helper'\n")
    init = write_module(
        package / "__init__.py",
        """
        from .helper import VALUE


        def setup(pi):
            pi.register_flag("pkg", "package extension", default=VALUE)
        """,
    )

    paths = resolve_extension_paths(cwd=str(tmp_path), agent_dir=str(agent_dir), project_trusted=False)
    assert paths == [str(init)]

    result = load_extensions(paths, discover_entry_points=False)
    assert result.diagnostics == []
    # The relative import proves the package search path is wired up.
    assert result.apis[0].get_flag("pkg") == "from-helper"


def test_package_init_wins_over_index_py_in_the_same_directory(tmp_path: Path) -> None:
    # A Python package's entry point is __init__.py; index.py (the TS-era form
    # kept by the shared discovery helper) is a submodule, not a second extension.
    agent_dir = tmp_path / "agent"
    package = agent_dir / "extensions" / "dual"
    init = write_module(package / "__init__.py", MINIMAL_SOURCE)
    write_module(package / "index.py", MINIMAL_SOURCE)

    paths = resolve_extension_paths(cwd=str(tmp_path), agent_dir=str(agent_dir), project_trusted=False)

    assert paths == [str(init)]


def test_non_python_files_and_nested_directories_are_ignored(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    extensions = agent_dir / "extensions"
    good = write_module(extensions / "good.py", MINIMAL_SOURCE)
    (extensions / "notes.txt").write_text("not an extension", encoding="utf-8")
    (extensions / "legacy.ts").write_text("export default function () {}", encoding="utf-8")
    # A directory without __init__.py is not an entry point...
    write_module(extensions / "noindex" / "helper.py", "VALUE = 1\n")
    # ...and discovery never recurses past one level.
    write_module(extensions / "deep" / "nested" / "__init__.py", MINIMAL_SOURCE)

    paths = resolve_extension_paths(cwd=str(tmp_path), agent_dir=str(agent_dir), project_trusted=False)
    assert paths == [str(good)]


def test_project_config_dir_name_can_be_overridden(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"piConfig": {"configDir": ".custom"}}', encoding="utf-8")
    project_ext = write_module(tmp_path / ".custom" / "extensions" / "project_ext.py", MINIMAL_SOURCE)
    write_module(tmp_path / ".pi" / "extensions" / "ignored.py", MINIMAL_SOURCE)

    paths = resolve_extension_paths(
        cwd=str(tmp_path), agent_dir=str(tmp_path / "agent"), project_trusted=True
    )
    assert paths == [str(project_ext)]


def test_extra_paths_support_files_packages_and_flat_directories(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    explicit = write_module(tmp_path / "explicit.py", MINIMAL_SOURCE)
    package = tmp_path / "pkg"
    package.mkdir()
    package_init = write_module(package / "__init__.py", MINIMAL_SOURCE)
    write_module(package / "sibling.py", MINIMAL_SOURCE)
    flat = tmp_path / "flat"
    write_module(flat / "a.py", MINIMAL_SOURCE)
    write_module(flat / "b.py", MINIMAL_SOURCE)
    (flat / "notes.txt").write_text("ignored", encoding="utf-8")

    paths = resolve_extension_paths(
        cwd=str(cwd),
        agent_dir=str(tmp_path / "agent"),
        project_trusted=False,
        extra_paths=[str(explicit), str(package), str(flat), str(tmp_path / "missing.py"), "relative_missing.py"],
    )

    # A package path means the package entry point only, not its siblings.
    assert paths == [str(explicit), str(package_init), str(flat / "a.py"), str(flat / "b.py")]


def test_resolution_never_imports_discovered_files(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    write_module(
        agent_dir / "extensions" / "explodes.py",
        "raise RuntimeError('must not be executed during discovery')\n",
    )

    paths = resolve_extension_paths(cwd=str(tmp_path), agent_dir=str(agent_dir), project_trusted=False)
    assert len(paths) == 1


# --------------------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------------------


def test_flag_values_reach_the_extension_api(tmp_path: Path) -> None:
    ext = write_module(
        tmp_path / "flags.py",
        """
        def setup(pi):
            pi.register_flag("mode", "Run mode", default="slow")
        """,
    )

    result = load_extensions(
        [str(ext)], flag_values={"mode": "fast", "unknown": "ignored"}, discover_entry_points=False
    )

    assert result.diagnostics == []
    assert result.apis[0].get_flag("mode") == "fast"
    assert result.apis[0].get_flag("unknown") is None


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------


@dataclass
class FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    name: str
    value: str


def patch_entry_points(monkeypatch: pytest.MonkeyPatch, entry_points: list[FakeEntryPoint], calls: list) -> None:
    def fake_entry_points(*, group: str | None = None):
        calls.append(group)
        assert group == ENTRY_POINT_GROUP
        return list(entry_points)

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


def test_entry_point_factory_is_imported_and_called(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_module(
        tmp_path / "ep_module_alpha.py",
        """
        def setup(pi):
            pi.register_flag("from_entry_point", "loaded via entry point", default="yes")
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    calls: list = []
    patch_entry_points(monkeypatch, [FakeEntryPoint("alpha", "ep_module_alpha:setup")], calls)

    result = load_extensions([], discover_entry_points=True)

    assert calls == [ENTRY_POINT_GROUP]
    assert result.diagnostics == []
    assert result.paths == ["<entry-point:alpha>"]
    assert len(result.apis) == 1
    assert result.apis[0].path == "<entry-point:alpha>"
    assert result.apis[0].get_flag("from_entry_point") == "yes"


def test_entry_point_discovery_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def exploding_entry_points(*, group: str | None = None):  # pragma: no cover - must not run
        raise AssertionError("entry points must not be queried when discovery is disabled")

    monkeypatch.setattr(importlib.metadata, "entry_points", exploding_entry_points)

    result = load_extensions([], discover_entry_points=False)

    assert result.apis == []
    assert result.paths == []
    assert result.diagnostics == []


def test_entry_point_failures_become_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_module(
        tmp_path / "ep_module_beta.py",
        """
        NOT_CALLABLE = 3


        def setup(pi):
            pi.register_flag("unused", "never registered", default=True)
        """,
    )
    write_module(tmp_path / "ep_module_gamma.py", "VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    calls: list = []
    patch_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("missing_module", "ep_module_does_not_exist_zzz:setup"),
            FakeEntryPoint("missing_attr", "ep_module_beta:nope"),
            FakeEntryPoint("not_callable", "ep_module_beta:NOT_CALLABLE"),
            FakeEntryPoint("no_factory", "ep_module_gamma"),
            FakeEntryPoint("no_target", ""),
        ],
        calls,
    )

    result = load_extensions([], discover_entry_points=True)

    assert result.apis == []
    assert len(result.diagnostics) == 5
    by_path = {diagnostic.path: diagnostic.message for diagnostic in result.diagnostics}
    assert "ep_module_does_not_exist_zzz" in by_path["<entry-point:missing_module>"]
    assert "no attribute" in by_path["<entry-point:missing_attr>"]
    assert "not callable" in by_path["<entry-point:not_callable>"]
    assert "does not define a callable" in by_path["<entry-point:no_factory>"]
    assert "no module target" in by_path["<entry-point:no_target>"]


def test_entry_point_metadata_errors_are_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_entry_points(*, group: str | None = None):
        raise RuntimeError("metadata broken")

    monkeypatch.setattr(importlib.metadata, "entry_points", broken_entry_points)

    result = load_extensions([], discover_entry_points=True)

    assert result.apis == []
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].severity == "warning"
    assert "metadata broken" in result.diagnostics[0].message


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------


def test_diagnostic_and_result_defaults() -> None:
    diagnostic = ExtensionDiagnostic(path="p", message="m")
    assert diagnostic.severity == "warning"

    result = ExtensionLoadResult()
    assert result.apis == [] and result.paths == [] and result.diagnostics == []


def test_extension_api_instances_are_fresh_per_extension(tmp_path: Path) -> None:
    first = write_module(tmp_path / "first.py", MINIMAL_SOURCE)
    second = write_module(tmp_path / "second.py", MINIMAL_SOURCE)

    result = load_extensions([str(first), str(second)], discover_entry_points=False)

    assert all(isinstance(api, ExtensionAPI) for api in result.apis)
    assert result.apis[0] is not result.apis[1]
    assert result.apis[0].flags is not result.apis[1].flags
    assert sys.modules  # sanity: importing extensions left the interpreter usable
