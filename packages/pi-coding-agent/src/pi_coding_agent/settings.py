"""Settings and project trust.

Port of ``packages/coding-agent/src/core/settings-manager.ts`` and
``trust-manager.ts`` (upstream pi), reduced to the fields the Python port needs.

Two scopes, one effective view
------------------------------
``<agent_dir>/settings.json`` is global; ``<config_dir>/settings.json`` is the
project scope and is only read when the project is trusted.  The effective view
is ``global <- project <- env <- CLI`` (later wins), merged with
:func:`deep_merge`: nested objects merge recursively, arrays are replaced
wholesale, and ``None`` values never erase a base value.

Writes are field-local
----------------------
A setter mutates the in-memory scope and then merges *only that field* back into
the on-disk JSON, so keys another tool wrote are never clobbered.  All writes go
through one :class:`asyncio.Lock`, mirroring upstream's serialized write queue.
``set_xxx`` writes the global scope; ``set_project_xxx`` writes the project scope
and raises :class:`ProjectNotTrustedError` when the project is untrusted.  Write
I/O failures are collected for :meth:`SettingsManager.drain_errors` instead of
propagating, and a scope whose file could not be parsed is never rewritten.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .resource_loader import DEFAULT_CONFIG_DIR_NAME, find_config_dir, get_agent_dir

#: Scope identifiers.
SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"

# -- defaults documented in docs/research/03-coding-agent-core.md section 6 ----

DEFAULT_COMPACTION_RESERVE_TOKENS = 16384
DEFAULT_COMPACTION_KEEP_RECENT_TOKENS = 20000
DEFAULT_RETRY_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_MS = 2000
DEFAULT_MAX_AGENT_RETRY_DELAY_MS = 60000
DEFAULT_HTTP_IDLE_TIMEOUT_MS = 300000
DEFAULT_PROJECT_TRUST = "ask"
DEFAULT_ENABLE_INSTALL_TELEMETRY = True
DEFAULT_ENABLE_ANALYTICS = False

#: Allowed values for ``defaultProjectTrust``; anything else falls back to "ask".
_PROJECT_TRUST_VALUES = ("ask", "always", "never")

#: Ambient environment variables promoted above the settings files.
ENV_SETTINGS_MAP: dict[str, tuple[str, ...]] = {
    "PI_DEFAULT_MODEL": ("defaultModel",),
    "PI_DEFAULT_PROVIDER": ("defaultProvider",),
    "PI_THEME": ("theme",),
    "PI_DEFAULT_PROJECT_TRUST": ("defaultProjectTrust",),
    "PI_HTTP_IDLE_TIMEOUT_MS": ("httpIdleTimeoutMs",),
    "PI_WEBSOCKET_CONNECT_TIMEOUT_MS": ("websocketConnectTimeoutMs",),
    "PI_BASH_TIMEOUT_MS": ("bashTimeoutMs",),
    "PI_ENABLED_MODELS": ("enabledModels",),
    "PI_AUTO_COMPACTION": ("autoCompaction",),
}

#: Trust-requiring entries directly under the project config directory.
TRUST_REQUIRING_PROJECT_CONFIG_RESOURCES: tuple[str, ...] = (
    "settings.json",
    "extensions",
    "skills",
    "prompts",
    "themes",
    "SYSTEM.md",
    "APPEND_SYSTEM.md",
)


class SettingsValidationError(ValueError):
    """Raised for the settings values upstream rejects (never silently clamps)."""


class ProjectNotTrustedError(RuntimeError):
    """Raised when writing project settings while the project is untrusted."""

    def __init__(self) -> None:
        super().__init__("Project is not trusted; refusing to write project settings")


class TrustStoreError(RuntimeError):
    """Raised when ``trust.json`` is unreadable or malformed."""


@dataclass(frozen=True)
class SettingsError:
    """A failed load or write for one scope."""

    scope: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class TelemetrySettings:
    """Effective telemetry configuration."""

    enabled: bool = DEFAULT_ENABLE_INSTALL_TELEMETRY
    analytics: bool = DEFAULT_ENABLE_ANALYTICS
    tracking_id: str | None = None


def is_mergeable_object(value: Any) -> bool:
    """True for a mapping that participates in a recursive merge."""
    return isinstance(value, Mapping)


def deep_merge(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overrides`` onto ``base``.

    Nested mappings merge key by key; every other value (including lists) is
    replaced wholesale.  ``None`` values act like upstream's ``undefined`` and
    are skipped so a partially written scope cannot erase a base value.
    """
    result: dict[str, Any] = dict(base)
    for key, override_value in overrides.items():
        if override_value is None:
            continue
        base_value = result.get(key)
        if isinstance(base_value, Mapping) and isinstance(override_value, Mapping):
            result[key] = deep_merge(base_value, override_value)
        else:
            result[key] = override_value
    return result


def canonical_path(path: str) -> str:
    """Realpath-normalized absolute path used as the trust store key."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _is_safe_non_negative_int(value: Any) -> bool:
    """Upstream ``Number.isSafeInteger(v) && v >= 0``; rejects bools and floats."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53 - 1


def _parse_timeout_ms(value: Any) -> int | None:
    """Parse an idle/connect timeout the way upstream ``parseHttpIdleTimeoutMs`` does."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.lower() == "disabled":
            return 0
        if not trimmed:
            return None
        try:
            return _parse_timeout_ms(int(trimmed))
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")) or number < 0:
            return None
        return int(number)
    return None


def _read_json_file(path: str) -> tuple[dict[str, Any], str | None]:
    """Read a settings JSON file; returns ``({}, error_message)`` on failure."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError as error:
        return {}, str(error)

    if raw.startswith("\ufeff"):
        raw = raw[1:]
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        return {}, str(error)
    if not isinstance(parsed, dict):
        return {}, "settings file must contain a JSON object"
    return parsed, None


def _write_json_file(path: str, payload: Mapping[str, Any]) -> None:
    """Atomically write indented JSON, creating parent directories."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), indent=2))
    os.replace(temp_path, path)


class SettingsManager:
    """Loads, merges and persists global/project settings.

    Construction reads both scopes once.  Read accessors never touch disk, so
    callers observe a stable snapshot until :meth:`reload` (or a setter) runs.
    """

    def __init__(
        self,
        cwd: str,
        agent_dir: str | None = None,
        *,
        project_trusted: bool = False,
        cli_overrides: Mapping[str, Any] | None = None,
        env_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        self._cwd = os.path.abspath(os.path.expanduser(cwd))
        self._agent_dir = (
            os.path.abspath(os.path.expanduser(agent_dir)) if agent_dir else get_agent_dir()
        )
        self._config_dir_name = find_config_dir(self._cwd)
        self._project_trusted = bool(project_trusted)

        self._global: dict[str, Any] = {}
        self._project: dict[str, Any] = {}
        self._cli: dict[str, Any] = deep_merge({}, dict(cli_overrides or {}))
        self._env: dict[str, Any] = deep_merge(dict(_ambient_env_overrides()), dict(env_overrides or {}))
        self._effective: dict[str, Any] = {}
        self._errors: list[SettingsError] = []
        self._load_errors: dict[str, str] = {}
        self._lock = asyncio.Lock()

        self.load()

    # -- paths -------------------------------------------------------------------

    @property
    def cwd(self) -> str:
        """Absolute working directory."""
        return self._cwd

    @property
    def agent_dir(self) -> str:
        """Absolute user-level agent directory."""
        return self._agent_dir

    @property
    def config_dir(self) -> str:
        """Absolute project config directory path."""
        return os.path.join(self._cwd, self._config_dir_name)

    def settings_path(self, scope: str) -> str:
        """On-disk settings file for ``scope``."""
        if scope == SCOPE_GLOBAL:
            return os.path.join(self._agent_dir, "settings.json")
        return os.path.join(self.config_dir, "settings.json")

    def is_project_trusted(self) -> bool:
        """Whether the project scope is currently loaded and writable."""
        return self._project_trusted

    def set_project_trusted(self, trusted: bool, *, reload: bool = True) -> None:
        """Toggle project trust; the project scope is dropped when untrusted."""
        self._project_trusted = bool(trusted)
        if reload:
            self.load()

    # -- loading -----------------------------------------------------------------

    def load(self) -> None:
        """(Re)read both scopes from disk and recompute the effective view."""
        global_settings, global_error = _read_json_file(self.settings_path(SCOPE_GLOBAL))
        self._global = global_settings
        self._record_load_error(SCOPE_GLOBAL, global_error)

        if self._project_trusted:
            project_settings, project_error = _read_json_file(self.settings_path(SCOPE_PROJECT))
            self._project = project_settings
            self._record_load_error(SCOPE_PROJECT, project_error)
        else:
            self._project = {}
            self._load_errors.pop(SCOPE_PROJECT, None)

        self._recompute()

    async def reload(self) -> None:
        """Serialize behind pending writes, then reload from disk."""
        async with self._lock:
            self.load()

    def _record_load_error(self, scope: str, error: str | None) -> None:
        if error is None:
            self._load_errors.pop(scope, None)
            return
        self._load_errors[scope] = error
        self._errors.append(SettingsError(scope=scope, message=error, path=self.settings_path(scope)))

    def _recompute(self) -> None:
        merged = deep_merge(self._global, self._project)
        merged = deep_merge(merged, self._env)
        self._effective = deep_merge(merged, self._cli)

    # -- overrides ---------------------------------------------------------------

    def set_cli_overrides(self, overrides: Mapping[str, Any]) -> None:
        """Replace the CLI override layer (highest precedence)."""
        self._cli = deep_merge({}, dict(overrides))
        self._recompute()

    def set_env_overrides(self, overrides: Mapping[str, Any]) -> None:
        """Replace the explicit environment override layer."""
        self._env = deep_merge(dict(_ambient_env_overrides()), dict(overrides))
        self._recompute()

    # -- generic pull API --------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Raw effective value for ``key`` (dotted paths supported)."""
        node: Any = self._effective
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def get_global_settings(self) -> dict[str, Any]:
        """Copy of the global scope as loaded from disk."""
        return dict(self._global)

    def get_project_settings(self) -> dict[str, Any]:
        """Copy of the project scope (empty when the project is untrusted)."""
        return dict(self._project)

    def get_effective_settings(self) -> dict[str, Any]:
        """Copy of the merged effective settings."""
        return dict(self._effective)

    def drain_errors(self) -> list[SettingsError]:
        """Return and clear the accumulated load/write errors."""
        drained = list(self._errors)
        self._errors = []
        return drained

    # -- typed getters -----------------------------------------------------------

    def get_default_project_trust(self) -> str:
        """``always`` / ``never`` / ``ask`` (invalid values fall back to ``ask``)."""
        value = self._global.get("defaultProjectTrust")
        return value if value in _PROJECT_TRUST_VALUES else DEFAULT_PROJECT_TRUST

    def get_theme(self) -> str | None:
        """Configured theme name, or ``None`` when unset."""
        value = self._effective.get("theme")
        return value if isinstance(value, str) else None

    def get_default_provider(self) -> str | None:
        """Configured default provider id."""
        value = self._effective.get("defaultProvider")
        return value if isinstance(value, str) else None

    def get_default_model(self) -> str | None:
        """Configured default model id."""
        value = self._effective.get("defaultModel")
        return value if isinstance(value, str) else None

    def get_enabled_models(self) -> list[str] | None:
        """Model patterns for cycling; ``None`` when unset or invalid."""
        value = self._effective.get("enabledModels")
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        return None

    def get_models(self) -> dict[str, Any]:
        """Per-model settings map (``models``); empty when unset or invalid."""
        value = self._effective.get("models")
        return dict(value) if isinstance(value, Mapping) else {}

    def get_model_overrides(self) -> dict[str, dict[str, Any]]:
        """Validated ``modelOverrides`` entries.

        Upstream rejects a non-object entry outright, so this raises
        :class:`SettingsValidationError` rather than clamping.
        """
        value = self._effective.get("modelOverrides")
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise SettingsValidationError(
                f"Invalid modelOverrides setting: {value!r}. Expected an object."
            )
        result: dict[str, dict[str, Any]] = {}
        for key, entry in value.items():
            if not isinstance(entry, Mapping):
                raise SettingsValidationError(
                    f'Invalid modelOverrides["{key}"] setting: {entry!r}. Expected an object.'
                )
            result[str(key)] = dict(entry)
        return result

    def get_auto_compaction(self) -> bool:
        """Whether automatic compaction is enabled (top-level override first)."""
        auto = self._effective.get("autoCompaction")
        if isinstance(auto, bool):
            return auto
        return self.get_compaction_enabled()

    def get_compaction_enabled(self) -> bool:
        """``compaction.enabled`` (default true)."""
        compaction = self._effective.get("compaction")
        if isinstance(compaction, Mapping):
            enabled = compaction.get("enabled")
            if isinstance(enabled, bool):
                return enabled
        return True

    def _compaction_token_setting(self, field_name: str, model_key: str | None) -> int:
        compaction = self._effective.get("compaction")
        compaction = compaction if isinstance(compaction, Mapping) else {}

        ordinary = compaction.get(field_name)
        if ordinary is not None and not _is_safe_non_negative_int(ordinary):
            raise SettingsValidationError(
                f"Invalid compaction.{field_name} setting: {ordinary!r}. "
                "Expected a non-negative safe integer."
            )

        overrides = compaction.get("modelOverrides")
        entry: Any = None
        if model_key is not None and isinstance(overrides, Mapping):
            entry = overrides.get(model_key)
        if entry is not None and not isinstance(entry, Mapping):
            raise SettingsValidationError(
                f'Invalid compaction.modelOverrides["{model_key}"] setting: {entry!r}. Expected an object.'
            )
        override = entry.get(field_name) if isinstance(entry, Mapping) else None
        if override is not None and not _is_safe_non_negative_int(override):
            raise SettingsValidationError(
                f'Invalid compaction.modelOverrides["{model_key}"].{field_name} setting: {override!r}. '
                "Expected a non-negative safe integer."
            )

        if override is not None:
            return int(override)
        if ordinary is not None:
            return int(ordinary)
        if field_name == "reserveTokens":
            return DEFAULT_COMPACTION_RESERVE_TOKENS
        return DEFAULT_COMPACTION_KEEP_RECENT_TOKENS

    def get_compaction_reserve_tokens(self, model_key: str | None = None) -> int:
        """Tokens reserved for prompt + response (raises on invalid values)."""
        return self._compaction_token_setting("reserveTokens", model_key)

    def get_compaction_keep_recent_tokens(self, model_key: str | None = None) -> int:
        """Recent tokens kept after compaction (raises on invalid values)."""
        return self._compaction_token_setting("keepRecentTokens", model_key)

    def get_compaction_settings(self, model_key: str | None = None) -> dict[str, Any]:
        """Resolved ``{enabled, reserveTokens, keepRecentTokens}``."""
        return {
            "enabled": self.get_compaction_enabled(),
            "reserveTokens": self.get_compaction_reserve_tokens(model_key),
            "keepRecentTokens": self.get_compaction_keep_recent_tokens(model_key),
        }

    def get_retry_settings(self) -> dict[str, Any]:
        """Resolved retry policy with upstream defaults."""
        retry = self._effective.get("retry")
        retry = retry if isinstance(retry, Mapping) else {}

        enabled = retry.get("enabled")
        max_retries = retry.get("maxRetries")
        base_delay = retry.get("baseDelayMs")
        max_agent_delay = retry.get("maxAgentDelayMs")
        return {
            "enabled": enabled if isinstance(enabled, bool) else True,
            "maxRetries": max_retries if _is_safe_non_negative_int(max_retries) else DEFAULT_RETRY_MAX_RETRIES,
            "baseDelayMs": base_delay if _is_safe_non_negative_int(base_delay) else DEFAULT_RETRY_BASE_DELAY_MS,
            "maxAgentDelayMs": (
                max_agent_delay
                if _is_safe_non_negative_int(max_agent_delay)
                else DEFAULT_MAX_AGENT_RETRY_DELAY_MS
            ),
        }

    def get_http_idle_timeout_ms(self) -> int:
        """HTTP idle timeout in ms (0 disables); raises on invalid values."""
        value = self._effective.get("httpIdleTimeoutMs")
        if value is None:
            return DEFAULT_HTTP_IDLE_TIMEOUT_MS
        parsed = _parse_timeout_ms(value)
        if parsed is None:
            raise SettingsValidationError(f"Invalid httpIdleTimeoutMs setting: {value!r}")
        return parsed

    def get_websocket_connect_timeout_ms(self) -> int | None:
        """WebSocket handshake timeout in ms (0 disables); raises on invalid values."""
        value = self._effective.get("websocketConnectTimeoutMs")
        if value is None:
            return None
        parsed = _parse_timeout_ms(value)
        if parsed is None:
            raise SettingsValidationError(f"Invalid websocketConnectTimeoutMs setting: {value!r}")
        return parsed

    def get_bash_timeout_ms(self) -> int | None:
        """Optional bash timeout override in ms.

        Upstream has no such setting (its bash tool defaults to no timeout), so
        the default here is ``None`` and invalid values clamp back to ``None``.
        """
        value = self._effective.get("bashTimeoutMs")
        parsed = _parse_timeout_ms(value)
        return parsed

    def get_telemetry(self) -> TelemetrySettings:
        """Effective telemetry settings, accepting a shorthand boolean."""
        telemetry = self._effective.get("telemetry")
        if isinstance(telemetry, bool):
            enabled, analytics, tracking_id = telemetry, None, None
        elif isinstance(telemetry, Mapping):
            raw_enabled = telemetry.get("enabled")
            raw_analytics = telemetry.get("analytics")
            raw_tracking = telemetry.get("trackingId")
            enabled = raw_enabled if isinstance(raw_enabled, bool) else None
            analytics = raw_analytics if isinstance(raw_analytics, bool) else None
            tracking_id = raw_tracking if isinstance(raw_tracking, str) else None
        else:
            enabled, analytics, tracking_id = None, None, None

        if enabled is None:
            legacy_enabled = self._effective.get("enableInstallTelemetry")
            enabled = legacy_enabled if isinstance(legacy_enabled, bool) else DEFAULT_ENABLE_INSTALL_TELEMETRY
        if analytics is None:
            legacy_analytics = self._effective.get("enableAnalytics")
            analytics = legacy_analytics if isinstance(legacy_analytics, bool) else DEFAULT_ENABLE_ANALYTICS
        if tracking_id is None:
            legacy_tracking = self._effective.get("trackingId")
            tracking_id = legacy_tracking if isinstance(legacy_tracking, str) else None
        return TelemetrySettings(enabled=enabled, analytics=analytics, tracking_id=tracking_id)

    def _string_list(self, key: str) -> list[str]:
        value = self._effective.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        return []

    def get_extension_paths(self) -> list[str]:
        """Configured local extension paths."""
        return self._string_list("extensions")

    def get_skill_paths(self) -> list[str]:
        """Configured local skill paths."""
        return self._string_list("skills")

    def get_prompt_paths(self) -> list[str]:
        """Configured local prompt template paths."""
        return self._string_list("prompts")

    def get_theme_paths(self) -> list[str]:
        """Configured local theme paths."""
        return self._string_list("themes")

    # -- writes ------------------------------------------------------------------

    def _scope_settings(self, scope: str) -> dict[str, Any]:
        return self._global if scope == SCOPE_GLOBAL else self._project

    def _assert_writable(self, scope: str) -> None:
        if scope == SCOPE_PROJECT and not self._project_trusted:
            raise ProjectNotTrustedError()
        load_error = self._load_errors.get(scope)
        if load_error is not None:
            # Writing would overwrite a file we could not parse; upstream refuses.
            raise SettingsValidationError(
                f"Refusing to write {scope} settings: file could not be loaded ({load_error})"
            )

    async def _update(self, scope: str, path: tuple[str, ...], value: Any) -> None:
        async with self._lock:
            self._assert_writable(scope)
            target = self._scope_settings(scope)
            node = target
            for part in path[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[path[-1]] = value
            self._recompute()
            try:
                self._persist(scope, path)
            except OSError as error:
                self._errors.append(
                    SettingsError(scope=scope, message=str(error), path=self.settings_path(scope))
                )

    def _persist(self, scope: str, path: tuple[str, ...]) -> None:
        """Merge one modified field into the on-disk JSON, preserving siblings."""
        file_path = self.settings_path(scope)
        disk, error = _read_json_file(file_path)
        if error is not None:
            raise SettingsValidationError(
                f"Refusing to write {scope} settings: file could not be loaded ({error})"
            )
        source = self._scope_settings(scope)
        node = disk
        value: Any = source
        for part in path:
            value = value[part]
            if part != path[-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
        node[path[-1]] = value
        _write_json_file(file_path, disk)

    # Global setters -------------------------------------------------------------

    async def set_default_project_trust(self, value: str) -> None:
        """Persist ``defaultProjectTrust`` (global only, like upstream)."""
        await self._update(SCOPE_GLOBAL, ("defaultProjectTrust",), value)

    async def set_theme(self, theme: str) -> None:
        """Persist the global theme name."""
        await self._update(SCOPE_GLOBAL, ("theme",), theme)

    async def set_default_model(self, model_id: str) -> None:
        """Persist the global default model id."""
        await self._update(SCOPE_GLOBAL, ("defaultModel",), model_id)

    async def set_default_provider(self, provider: str) -> None:
        """Persist the global default provider id."""
        await self._update(SCOPE_GLOBAL, ("defaultProvider",), provider)

    async def set_enabled_models(self, patterns: list[str]) -> None:
        """Persist the model cycling patterns (arrays replace wholesale)."""
        await self._update(SCOPE_GLOBAL, ("enabledModels",), list(patterns))

    async def set_models(self, models: Mapping[str, Any]) -> None:
        """Persist the ``models`` map."""
        await self._update(SCOPE_GLOBAL, ("models",), dict(models))

    async def set_model_overrides(self, overrides: Mapping[str, Any]) -> None:
        """Persist ``modelOverrides`` after validating every entry is an object."""
        for key, entry in overrides.items():
            if not isinstance(entry, Mapping):
                raise SettingsValidationError(
                    f'Invalid modelOverrides["{key}"] setting: {entry!r}. Expected an object.'
                )
        await self._update(SCOPE_GLOBAL, ("modelOverrides",), {k: dict(v) for k, v in overrides.items()})

    async def set_compaction_enabled(self, enabled: bool) -> None:
        """Persist ``compaction.enabled`` without touching sibling keys."""
        await self._update(SCOPE_GLOBAL, ("compaction", "enabled"), bool(enabled))

    async def set_compaction_reserve_tokens(self, tokens: int) -> None:
        """Persist ``compaction.reserveTokens`` (raises on invalid values)."""
        if not _is_safe_non_negative_int(tokens):
            raise SettingsValidationError(
                f"Invalid compaction.reserveTokens setting: {tokens!r}. Expected a non-negative safe integer."
            )
        await self._update(SCOPE_GLOBAL, ("compaction", "reserveTokens"), int(tokens))

    async def set_compaction_keep_recent_tokens(self, tokens: int) -> None:
        """Persist ``compaction.keepRecentTokens`` (raises on invalid values)."""
        if not _is_safe_non_negative_int(tokens):
            raise SettingsValidationError(
                f"Invalid compaction.keepRecentTokens setting: {tokens!r}. "
                "Expected a non-negative safe integer."
            )
        await self._update(SCOPE_GLOBAL, ("compaction", "keepRecentTokens"), int(tokens))

    async def set_auto_compaction(self, enabled: bool) -> None:
        """Persist the top-level ``autoCompaction`` toggle."""
        await self._update(SCOPE_GLOBAL, ("autoCompaction",), bool(enabled))

    async def set_retry_enabled(self, enabled: bool) -> None:
        """Persist ``retry.enabled``."""
        await self._update(SCOPE_GLOBAL, ("retry", "enabled"), bool(enabled))

    async def set_retry_max_retries(self, count: int) -> None:
        """Persist ``retry.maxRetries``."""
        await self._update(SCOPE_GLOBAL, ("retry", "maxRetries"), int(count))

    async def set_bash_timeout_ms(self, timeout_ms: int | None) -> None:
        """Persist the optional bash timeout override."""
        await self._update(SCOPE_GLOBAL, ("bashTimeoutMs",), timeout_ms)

    async def set_http_idle_timeout_ms(self, timeout_ms: int) -> None:
        """Persist ``httpIdleTimeoutMs`` (raises on invalid values, like upstream)."""
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
            raise SettingsValidationError(f"Invalid httpIdleTimeoutMs setting: {timeout_ms!r}")
        if timeout_ms != timeout_ms or timeout_ms in (float("inf"), float("-inf")) or timeout_ms < 0:
            raise SettingsValidationError(f"Invalid httpIdleTimeoutMs setting: {timeout_ms!r}")
        await self._update(SCOPE_GLOBAL, ("httpIdleTimeoutMs",), int(timeout_ms))

    async def set_websocket_connect_timeout_ms(self, timeout_ms: int) -> None:
        """Persist ``websocketConnectTimeoutMs`` (raises on invalid values)."""
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
            raise SettingsValidationError(f"Invalid websocketConnectTimeoutMs setting: {timeout_ms!r}")
        if timeout_ms != timeout_ms or timeout_ms in (float("inf"), float("-inf")) or timeout_ms < 0:
            raise SettingsValidationError(f"Invalid websocketConnectTimeoutMs setting: {timeout_ms!r}")
        await self._update(SCOPE_GLOBAL, ("websocketConnectTimeoutMs",), int(timeout_ms))

    async def set_telemetry_enabled(self, enabled: bool) -> None:
        """Persist telemetry as a shorthand boolean."""
        await self._update(SCOPE_GLOBAL, ("telemetry",), bool(enabled))

    async def set_extension_paths(self, paths: list[str]) -> None:
        """Persist the global extension path list."""
        await self._update(SCOPE_GLOBAL, ("extensions",), list(paths))

    async def set_skill_paths(self, paths: list[str]) -> None:
        """Persist the global skill path list."""
        await self._update(SCOPE_GLOBAL, ("skills",), list(paths))

    async def set_prompt_paths(self, paths: list[str]) -> None:
        """Persist the global prompt template path list."""
        await self._update(SCOPE_GLOBAL, ("prompts",), list(paths))

    async def set_theme_paths(self, paths: list[str]) -> None:
        """Persist the global theme path list."""
        await self._update(SCOPE_GLOBAL, ("themes",), list(paths))

    # Project setters ------------------------------------------------------------

    async def set_project_theme(self, theme: str) -> None:
        """Persist the theme into the project scope (trusted projects only)."""
        await self._update(SCOPE_PROJECT, ("theme",), theme)

    async def set_project_default_model(self, model_id: str) -> None:
        """Persist the default model into the project scope."""
        await self._update(SCOPE_PROJECT, ("defaultModel",), model_id)

    async def set_project_enabled_models(self, patterns: list[str]) -> None:
        """Persist the model cycling patterns into the project scope."""
        await self._update(SCOPE_PROJECT, ("enabledModels",), list(patterns))

    async def set_project_compaction_enabled(self, enabled: bool) -> None:
        """Persist ``compaction.enabled`` into the project scope."""
        await self._update(SCOPE_PROJECT, ("compaction", "enabled"), bool(enabled))

    async def set_project_retry_enabled(self, enabled: bool) -> None:
        """Persist ``retry.enabled`` into the project scope."""
        await self._update(SCOPE_PROJECT, ("retry", "enabled"), bool(enabled))

    async def set_project_extension_paths(self, paths: list[str]) -> None:
        """Persist the extension path list into the project scope."""
        await self._update(SCOPE_PROJECT, ("extensions",), list(paths))

    async def set_project_skill_paths(self, paths: list[str]) -> None:
        """Persist the skill path list into the project scope."""
        await self._update(SCOPE_PROJECT, ("skills",), list(paths))

    async def set_project_prompt_paths(self, paths: list[str]) -> None:
        """Persist the prompt template path list into the project scope."""
        await self._update(SCOPE_PROJECT, ("prompts",), list(paths))

    async def set_project_theme_paths(self, paths: list[str]) -> None:
        """Persist the theme path list into the project scope."""
        await self._update(SCOPE_PROJECT, ("themes",), list(paths))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SettingsManager(cwd={self._cwd!r}, agent_dir={self._agent_dir!r}, "
            f"project_trusted={self._project_trusted})"
        )


def _ambient_env_overrides() -> dict[str, Any]:
    """Promote the documented ``PI_*`` environment variables above the files."""
    overrides: dict[str, Any] = {}
    for env_name, path in ENV_SETTINGS_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw.strip() == "":
            continue
        value: Any = raw.strip()
        if path == ("enabledModels",):
            value = [item.strip() for item in raw.split(",") if item.strip()]
        elif env_name.endswith("_MS"):
            try:
                value = int(raw.strip())
            except ValueError:
                continue
        elif path == ("autoCompaction",):
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        node = overrides
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return overrides


@dataclass(frozen=True)
class TrustEntry:
    """The nearest stored trust decision for a path."""

    path: str
    decision: bool


class ProjectTrustStore:
    """Reads and writes ``<agent_dir>/trust.json``.

    The file maps canonical paths to ``true`` / ``false`` / ``null``; ``null``
    records "asked but not decided" for a child so a parent decision can still
    apply.  Reads walk up from the requested path so a decision on a parent
    folder covers its children.  Writes are atomic (temp file + replace); the
    upstream file lock is deliberately omitted in this single-process port.
    """

    def __init__(self, agent_dir: str | None = None) -> None:
        self._agent_dir = (
            os.path.abspath(os.path.expanduser(agent_dir)) if agent_dir else get_agent_dir()
        )

    @property
    def trust_path(self) -> str:
        """Path of the trust store file."""
        return os.path.join(self._agent_dir, "trust.json")

    def read(self) -> dict[str, bool | None]:
        """Whole trust store as a mapping; raises on malformed content."""
        path = self.trust_path
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                raw = handle.read()
        except OSError as error:
            raise TrustStoreError(f"Failed to read trust store {path}: {error}") from error
        if raw.startswith("\ufeff"):
            raw = raw[1:]
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as error:
            raise TrustStoreError(f"Failed to read trust store {path}: {error}") from error
        if not isinstance(parsed, dict):
            raise TrustStoreError(f"Invalid trust store {path}: expected an object")
        data: dict[str, bool | None] = {}
        for key, value in parsed.items():
            if value is not None and not isinstance(value, bool):
                raise TrustStoreError(
                    f"Invalid trust store {path}: value for {key!r} must be true, false, or null"
                )
            data[str(key)] = value
        return data

    def write(self, data: Mapping[str, bool | None]) -> None:
        """Atomically write the store with sorted keys and a trailing newline."""
        path = self.trust_path
        sorted_data = {
            str(key): value
            for key, value in sorted(data.items())
            if value is None or isinstance(value, bool)
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(sorted_data, indent=2))
            handle.write("\n")
        os.replace(temp_path, path)

    def get_entry(self, cwd: str) -> TrustEntry | None:
        """Nearest ancestor entry (inclusive) for ``cwd``."""
        data = self.read()
        current = canonical_path(cwd)
        while True:
            value = data.get(current)
            if value is True or value is False:
                return TrustEntry(path=current, decision=value)
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent

    def get_trust(self, cwd: str) -> bool | None:
        """Stored decision for ``cwd`` or its nearest ancestor, else ``None``."""
        entry = self.get_entry(cwd)
        return entry.decision if entry is not None else None

    def set_trust(self, cwd: str, decision: bool | None) -> None:
        """Record a decision (``None`` clears it) keyed by canonical path."""
        if decision is not None and not isinstance(decision, bool):
            raise TrustStoreError("trust decision must be true, false, or null")
        data = self.read()
        key = canonical_path(cwd)
        if decision is None:
            data.pop(key, None)
        else:
            data[key] = decision
        self.write(data)


def has_trust_requiring_project_resources(cwd: str, config_dir_name: str | None = None) -> bool:
    """Whether ``cwd`` has project resources that must be gated by trust.

    True when any trust-requiring entry exists under the project config
    directory, or when any ancestor (including cwd) has a ``.agents/skills``
    directory other than the user-level ``$HOME/.agents/skills``.
    """
    resolved_cwd = canonical_path(cwd)
    config_name = config_dir_name or find_config_dir(resolved_cwd) or DEFAULT_CONFIG_DIR_NAME
    home = canonical_path(os.path.expanduser("~"))
    user_agents_skills = os.path.join(home, ".agents", "skills")

    current = resolved_cwd
    while True:
        config_dir = os.path.join(current, config_name)
        for entry in TRUST_REQUIRING_PROJECT_CONFIG_RESOURCES:
            if os.path.exists(os.path.join(config_dir, entry)):
                return True

        agents_skills = os.path.join(current, ".agents", "skills")
        if os.path.realpath(agents_skills) != os.path.realpath(user_agents_skills) and os.path.exists(
            agents_skills
        ):
            return True

        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent
