"""Discovery and loading for Python-native extensions.

Port of ``packages/coding-agent/src/core/extensions/loader.ts``, adapted to a
single-language world: an extension is an ordinary Python module, so "loading"
is ``importlib`` rather than jiti and there is no cross-language boundary left to
bridge.

The two halves are deliberately separate:

* :func:`resolve_extension_paths` is pure filesystem work and **never imports
  anything**.  A caller can therefore list what *would* load -- including the
  project-local extensions behind the trust gate -- without executing untrusted
  project code.
* :func:`load_extensions` imports modules and runs their factories.  It is a
  startup-time API and **never raises**: a missing file, a syntax error, a
  module without a factory, or a factory that throws all become an
  :class:`ExtensionDiagnostic`, because one broken third-party extension must not
  be able to take down the process.

Why synthetic module names
--------------------------
``index.py`` and ``extension.py`` are common extension filenames, so importing by
basename would make two unrelated extensions collide in ``sys.modules``.  Every
file is instead registered under ``pi_extension_<sha256(realpath)[:16]>``: unique
per file, stable across loads.  Registering the module in ``sys.modules``
*before* ``exec_module`` is also what lets ``@dataclass`` and other
string-annotation machinery resolve ``__module__``/``__qualname__`` lookups.  A
package-shaped extension (``<name>/__init__.py``) additionally gets its directory
as ``submodule_search_locations``, so relative imports of the package's own
submodules work.  Absolute imports of sibling files are *not* supported -- the
extension's directory is never pushed onto ``sys.path`` -- and such a failure is
reported as a diagnostic rather than silently shadowing another extension's
module.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..resource_loader import collect_extension_paths, find_config_dir
from .api import ExtensionAPI

#: Entry-point group that installed distributions may declare extension
#: factories under.  The value is ``module:attr``.
ENTRY_POINT_GROUP = "pi_coding_agent.extensions"

#: Module attributes probed for the extension factory, in precedence order.
#: ``setup`` matches pi's ``export default function``; the other two are accepted
#: so a module written as ``register(pi)`` still loads instead of failing.
FACTORY_ATTRIBUTE_NAMES: tuple[str, ...] = ("setup", "extension", "register")


@dataclass
class ExtensionDiagnostic:
    """A non-fatal problem found while discovering or loading extensions."""

    path: str
    message: str
    severity: str = "warning"


@dataclass
class ExtensionLoadResult:
    """What one :func:`load_extensions` pass produced.

    ``apis`` are the extensions that loaded successfully, in load order.
    ``paths`` lists every source location the pass *considered* (file paths and
    ``<entry-point:NAME>`` descriptors), so a caller can report "loaded 2 of 5";
    anything that did not load is explained in ``diagnostics``.
    """

    apis: list[ExtensionAPI] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    diagnostics: list[ExtensionDiagnostic] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Discovery (no imports)
# --------------------------------------------------------------------------------------


def resolve_extension_paths(
    *,
    cwd: str,
    agent_dir: str,
    project_trusted: bool,
    extra_paths: Sequence[str] = (),
) -> list[str]:
    """List extension entry-point files in pi's discovery order.

    Order is project-local (only when ``project_trusted``), then global, then the
    explicitly configured ``extra_paths``, exactly like upstream
    ``discoverAndLoadExtensions``.  That order is load order, and extensions are
    dispatched in load order, so it *is* precedence: ``tool_call`` stops at the
    first block, ``input`` transforms chain oldest-first, and a
    ``before_agent_start`` system-prompt replacement is overwritten by the last
    loader -- so a project extension must come first to outrank a global one.
    Discovery is one level deep: a top-level ``*.py`` file, or a directory
    holding ``__init__.py``.  Nothing here imports the files, so an untrusted
    project can be described before its code is allowed to run.

    ``extra_paths`` entries may be a file, a directory with ``__init__.py``, or a
    directory of top-level ``*.py`` files; a path that does not exist is skipped
    rather than reported (this function returns paths only).
    """
    resolved_cwd = os.path.abspath(os.path.expanduser(cwd))
    resolved_agent_dir = os.path.abspath(os.path.expanduser(agent_dir))
    config_dir = os.path.join(resolved_cwd, find_config_dir(resolved_cwd))

    candidates: list[str] = []
    if project_trusted:
        # The trust gate: a freshly cloned repository must not contribute code
        # until the user has approved it (pi applies the same rule).  Because
        # de-duplication keeps the first occurrence, a project file that resolves
        # to the same real path as a global one shadows the global listing.
        candidates.extend(_discover_in_dir(os.path.join(config_dir, "extensions")))
    candidates.extend(_discover_in_dir(os.path.join(resolved_agent_dir, "extensions")))
    for raw_path in extra_paths:
        candidates.extend(_resolve_extra_path(raw_path, resolved_cwd))

    return _dedupe_paths(candidates)


def _discover_in_dir(directory: str) -> list[str]:
    """One-level discovery inside a standard ``extensions`` directory.

    The ``*.py``/``index.py`` scan comes from
    :func:`~pi_coding_agent.extensions.resource_loader.collect_extension_paths`
    so discovery stays in one place.  Two adaptations are applied here:

    * ``.ts`` entries from that v0.1 cross-language scan are dropped; this port
      only executes Python.
    * A directory holding ``__init__.py`` is a Python package, and its entry
      point is ``__init__.py`` -- not ``index.py``.  When both exist the package
      form wins, matching pi's "declared entry beats bare index" preference.
    """
    if not os.path.isdir(directory):
        return []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []

    package_dirs: set[str] = set()
    for entry_name in entries:
        if entry_name.startswith(".") or entry_name == "node_modules":
            continue
        candidate = os.path.join(directory, entry_name)
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "__init__.py")):
            package_dirs.add(os.path.realpath(candidate))

    discovered: list[str] = []
    for path in collect_extension_paths(directory):
        if not path.endswith(".py"):
            continue
        if os.path.basename(path) == "index.py" and os.path.realpath(os.path.dirname(path)) in package_dirs:
            continue
        discovered.append(path)
    for package_dir in package_dirs:
        discovered.append(os.path.join(package_dir, "__init__.py"))
    # Sorting keeps load order deterministic across the two discovery sources.
    return sorted(discovered)


def _resolve_extra_path(raw_path: str, cwd: str) -> list[str]:
    """Resolve one configured/CLI path to concrete entry-point files."""
    expanded = os.path.expanduser(str(raw_path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    path = os.path.abspath(expanded)

    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        package_init = os.path.join(path, "__init__.py")
        if os.path.isfile(package_init):
            # An explicit package path means "this package", not "everything in
            # it"; loading the sibling modules too would execute them standalone.
            return [package_init]
        return _top_level_python_files(path)
    return []


def _top_level_python_files(directory: str) -> list[str]:
    """Sorted top-level ``*.py`` files of ``directory`` (no recursion)."""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    files: list[str] = []
    for entry_name in entries:
        if entry_name.startswith("."):
            continue
        candidate = os.path.join(directory, entry_name)
        if os.path.isfile(candidate) and entry_name.endswith(".py"):
            files.append(os.path.abspath(candidate))
    return files


def _dedupe_paths(paths: Sequence[str]) -> list[str]:
    """Order-preserving de-duplication keyed on the canonical path."""
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        canonical = os.path.realpath(path)
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append(path)
    return unique


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def load_extensions(
    paths: Sequence[str],
    *,
    flag_values: Mapping[str, bool | str | None] | None = None,
    discover_entry_points: bool = True,
) -> ExtensionLoadResult:
    """Import ``paths``, run each extension factory, and collect diagnostics.

    Each path is imported under a synthetic name (see the module docstring) so
    same-named files cannot clobber each other.  The factory is the first
    callable of ``setup``, ``extension`` or ``register`` found on the module.

    A factory may be sync or async.  An awaitable result is driven to completion
    with :func:`asyncio.run`, which is only legal while no event loop is running.
    Detected inside a running loop, the loader raises a :class:`RuntimeError`
    explaining that extension loading is startup-time work; that error is caught
    like any other failure and surfaces as a diagnostic, because the public
    promise of this function is that a bad extension degrades to a diagnostic
    instead of an exception.  Load extensions before starting a loop (or make the
    factory synchronous) to load an async extension.

    ``flag_values`` are applied after each factory ran, since factories register
    their flags during ``setup`` and ``ExtensionAPI.set_flag_values`` only updates
    already-registered names.  ``discover_entry_points=False`` skips installed
    distributions entirely, which is what a sandboxed or offline caller wants.
    """
    result = ExtensionLoadResult()
    values = dict(flag_values) if flag_values is not None else None

    seen: set[str] = set()
    for raw_path in paths:
        resolved = os.path.abspath(os.path.expanduser(str(raw_path)))
        canonical = os.path.realpath(resolved)
        if canonical in seen:
            continue
        seen.add(canonical)
        result.paths.append(resolved)
        api = _load_path(resolved, result.diagnostics, values)
        if api is not None:
            result.apis.append(api)

    if discover_entry_points:
        _load_installed_entry_points(result, values)

    return result


def _load_path(
    path: str,
    diagnostics: list[ExtensionDiagnostic],
    flag_values: Mapping[str, bool | str | None] | None,
) -> ExtensionAPI | None:
    """Import and initialise one extension file; ``None`` on any failure."""
    if os.path.isdir(path):
        diagnostics.append(
            ExtensionDiagnostic(
                path,
                "Extension path is a directory, not an entry-point file; "
                "use resolve_extension_paths() to expand directories",
                "error",
            )
        )
        return None
    if not os.path.isfile(path):
        diagnostics.append(
            ExtensionDiagnostic(path, "Extension path does not exist or is not a file", "error")
        )
        return None

    try:
        module = _import_module_from_path(path)
    except Exception as error:  # noqa: BLE001 - any import failure is a diagnostic
        diagnostics.append(
            ExtensionDiagnostic(path, f"Failed to import extension module: {_describe(error)}", "error")
        )
        return None

    factory = _find_factory(module)
    if factory is None:
        diagnostics.append(
            ExtensionDiagnostic(
                path,
                "Extension module does not define a callable 'setup', 'extension' or 'register' factory",
                "error",
            )
        )
        return None

    return _call_factory(factory, path, diagnostics, flag_values)


def _find_factory(module: Any) -> Any:
    """First callable factory attribute on ``module``, or ``None``."""
    for attribute_name in FACTORY_ATTRIBUTE_NAMES:
        candidate = getattr(module, attribute_name, None)
        if callable(candidate):
            return candidate
    return None


def _call_factory(
    factory: Any,
    path: str,
    diagnostics: list[ExtensionDiagnostic],
    flag_values: Mapping[str, bool | str | None] | None,
) -> ExtensionAPI | None:
    """Run one factory against a fresh API; ``None`` if it raised."""
    api = ExtensionAPI(path=path)
    try:
        returned = factory(api)
        if inspect.isawaitable(returned):
            _drive_awaitable(returned, path)
    except Exception as error:  # noqa: BLE001 - a throwing factory must not abort the load
        diagnostics.append(
            ExtensionDiagnostic(path, f"Extension factory failed: {_describe(error)}", "error")
        )
        return None

    if flag_values is not None:
        api.set_flag_values(dict(flag_values))
    return api


def _import_module_from_path(path: str) -> Any:
    """Import ``path`` as a uniquely named top-level (or package) module."""
    module_name = _synthetic_module_name(path)
    # A package-shaped extension keeps its own directory as its search path so
    # ``from .submodule import x`` resolves; a plain module has none.
    search_locations = [os.path.dirname(path)] if os.path.basename(path) == "__init__.py" else None

    spec = importlib.util.spec_from_file_location(
        module_name, path, submodule_search_locations=search_locations
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build an import spec for {path}")

    module = importlib.util.module_from_spec(spec)
    # Register before exec_module: dataclasses and other annotation helpers look
    # ``__module__`` up in sys.modules while the module body is still running.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Do not leave a half-initialised module behind: a second load attempt
        # must start clean rather than see stale attributes.
        sys.modules.pop(module_name, None)
        raise
    return module


def _synthetic_module_name(path: str) -> str:
    """Unique, stable module name for one file (see the module docstring)."""
    digest = hashlib.sha256(os.path.realpath(path).encode("utf-8")).hexdigest()[:16]
    return f"pi_extension_{digest}"


def _drive_awaitable(awaitable: Any, path: str) -> None:
    """Run an async factory to completion, or refuse when a loop owns the thread."""
    if _has_running_loop():
        # asyncio.run() would raise here, and awaiting from sync code would
        # deadlock, so abandon the coroutine (closing it avoids a spurious
        # "coroutine was never awaited" RuntimeWarning) and explain the contract.
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise RuntimeError(
            f"Extension '{path}' has an async factory, but an event loop is already running. "
            "Extension loading is a startup-time operation: call load_extensions() before "
            "starting the loop, or make the factory synchronous."
        )
    asyncio.run(_await_result(awaitable))


async def _await_result(awaitable: Any) -> Any:
    """Adapter so asyncio.run accepts any awaitable, not only a coroutine."""
    return await awaitable


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


# --------------------------------------------------------------------------------------
# Installed entry points
# --------------------------------------------------------------------------------------


def _load_installed_entry_points(
    result: ExtensionLoadResult,
    flag_values: Mapping[str, bool | str | None] | None,
) -> None:
    """Load ``module:attr`` factories declared by installed distributions."""
    group = ENTRY_POINT_GROUP
    try:
        # Resolved through the module (not a from-import) so a caller or test can
        # substitute the discovery function.
        discovered = importlib.metadata.entry_points(group=group)
    except Exception as error:  # noqa: BLE001 - unusable metadata is not fatal
        result.diagnostics.append(
            ExtensionDiagnostic(
                f"<entry-points:{group}>",
                f"Could not read the '{group}' entry points: {_describe(error)}",
                "warning",
            )
        )
        return

    if isinstance(discovered, Mapping):  # tolerate the pre-3.12 dict-shaped API
        discovered = discovered.get(group, ())

    try:
        # Metadata order is arbitrary; sorting keeps extension load order stable.
        ordered = sorted(
            discovered, key=lambda ep: (str(getattr(ep, "name", "")), str(getattr(ep, "value", "")))
        )
    except Exception:  # noqa: BLE001 - a non-iterable of weird objects is still loadable
        ordered = list(discovered)

    for entry_point in ordered:
        try:
            _load_entry_point(entry_point, result, flag_values)
        except Exception as error:  # noqa: BLE001 - metadata objects are third-party code
            # Entry-point objects come from installed distributions, so even
            # reading their attributes can raise; keep the no-exception promise.
            result.diagnostics.append(
                ExtensionDiagnostic(
                    f"<entry-points:{group}>",
                    f"Unexpected failure while loading an entry point: {_describe(error)}",
                    "error",
                )
            )


def _load_entry_point(
    entry_point: Any,
    result: ExtensionLoadResult,
    flag_values: Mapping[str, bool | str | None] | None,
) -> None:
    name = str(getattr(entry_point, "name", "<unnamed>"))
    value = str(getattr(entry_point, "value", ""))
    descriptor = f"<entry-point:{name}>"
    result.paths.append(descriptor)

    module_name, separator, attribute = value.partition(":")
    module_name = module_name.strip()
    attribute = attribute.strip() if separator else ""
    if not module_name:
        result.diagnostics.append(
            ExtensionDiagnostic(descriptor, f"Entry point '{name}' has no module target (value: '{value}')", "error")
        )
        return

    try:
        module = importlib.import_module(module_name)
    except Exception as error:  # noqa: BLE001
        result.diagnostics.append(
            ExtensionDiagnostic(
                descriptor, f"Failed to import entry-point module '{module_name}': {_describe(error)}", "error"
            )
        )
        return

    if attribute:
        try:
            factory: Any = getattr(module, attribute)
        except AttributeError:
            result.diagnostics.append(
                ExtensionDiagnostic(
                    descriptor,
                    f"Entry point '{name}' points at '{value}', but '{module_name}' has no attribute '{attribute}'",
                    "error",
                )
            )
            return
        if not callable(factory):
            result.diagnostics.append(
                ExtensionDiagnostic(descriptor, f"Entry point '{name}' target '{value}' is not callable", "error")
            )
            return
    else:
        # No ``:attr``: fall back to the same attribute precedence used for files.
        factory = _find_factory(module)
        if factory is None:
            result.diagnostics.append(
                ExtensionDiagnostic(
                    descriptor,
                    f"Entry point '{name}' ('{value}') does not define a callable 'setup', "
                    "'extension' or 'register' factory",
                    "error",
                )
            )
            return

    api = _call_factory(factory, descriptor, result.diagnostics, flag_values)
    if api is not None:
        result.apis.append(api)


def _describe(error: BaseException) -> str:
    """One-line-ish ``Type: message`` rendering used inside diagnostics."""
    return f"{type(error).__name__}: {error}"


__all__ = [
    "ENTRY_POINT_GROUP",
    "ExtensionDiagnostic",
    "ExtensionLoadResult",
    "load_extensions",
    "resolve_extension_paths",
]
