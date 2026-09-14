"""The model catalog and its resolution.

Two layers make up the catalog:

* the **builtin data pack** at ``pi_ai/data/models.json``, loaded through
  :mod:`importlib.resources` so it survives being installed as a wheel.  It is a
  regenerable artifact (upstream generates its equivalent from provider
  catalogs) and is intentionally small;
* an optional user **overlay** (``~/.pi/agent/models.json`` by default), merged
  in priority order ``builtin models -> overlay provider -> overlay models``.
  Later layers win, and an overlay model with the same ``id`` replaces the
  builtin one.

Both files share the ``models.json`` shape understood by the TypeScript ``pi``
(``{"providers": {<id>: {"baseUrl": ..., "api": ..., "compat": ..., "models":
[...]}}}``) and both accept JSONC: ``//`` and ``/* */`` comments plus trailing
commas are tolerated.  Upstream achieves that with ``strip-json-comments`` plus
a permissive reader; here a small hand-written scanner does the same so pi-ai
gains no extra dependency.

Remote catalog fetching is deliberately **out of scope for v0.1**: this registry
is offline-first and never touches the network.  Upstream's ``refreshModels`` /
``ModelsStore`` machinery can be layered on later without changing these APIs.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from .auth import AuthStorage
from .errors import PiAiError
from .types import (
    EXTENDED_THINKING_LEVELS,
    Model,
    ModelCost,
    ModelCostTier,
)

__all__ = [
    "BUILTIN_CATALOG",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODELS_PATH",
    "ModelRegistry",
    "ModelsConfigError",
    "ResolvedModel",
    "get_model",
    "load_models_json",
]

#: Where ``pi`` keeps the user model overlay (``~/.pi/agent/models.json``).
DEFAULT_MODELS_PATH = Path.home() / ".pi" / "agent" / "models.json"

#: Upstream defaults for an overlay model that omits the field.
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_TOKENS = 16_384

_INPUT_MODES = ("text", "image")
_COST_RATE_KEYS = (
    ("input", "input"),
    ("output", "output"),
    ("cache_read", "cacheRead"),
    ("cache_write", "cacheWrite"),
)


class ModelsConfigError(PiAiError):
    """The builtin data pack or a models.json overlay is malformed."""


# --------------------------------------------------------------------------------------
# JSONC parsing
# --------------------------------------------------------------------------------------


def _strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments without touching string literals."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length:
            next_char = text[index + 1]
            if next_char == "/":
                index += 2
                while index < length and text[index] not in "\r\n":
                    index += 1
                continue
            if next_char == "*":
                index += 2
                while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                    index += 1
                index += 2  # consume the closing "*/"
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Drop commas that immediately precede ``}`` or ``]`` (JSONC tolerance)."""
    out: list[str] = []
    in_string = False
    escaped = False
    pending_comma = False
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            if pending_comma:
                out.append(",")
                pending_comma = False
            in_string = True
            out.append(char)
        elif char == ",":
            pending_comma = True
        elif char in " \t\r\n":
            out.append(char)
        else:
            if pending_comma:
                if char not in "}]":
                    out.append(",")
                pending_comma = False
            out.append(char)
    if pending_comma:
        out.append(",")
    return "".join(out)


def _parse_jsonc(text: str, *, source: str) -> Any:
    cleaned = _strip_trailing_commas(_strip_json_comments(text.lstrip("\ufeff")))
    try:
        return json.loads(cleaned)
    except ValueError as error:
        raise ModelsConfigError(f"{source}: invalid JSON: {error}") from error


# --------------------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------------------


def _require_object(value: Any, where: str, source: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelsConfigError(f"{source}: {where} must be an object")
    return value


def _optional_str(value: Any, where: str, source: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ModelsConfigError(f"{source}: {where} must be a non-empty string")
    return value


def _optional_bool(value: Any, where: str, source: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ModelsConfigError(f"{source}: {where} must be a boolean")
    return value


def _optional_int(value: Any, where: str, source: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelsConfigError(f"{source}: {where} must be an integer")
    return value


def _optional_number(value: Any, where: str, source: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelsConfigError(f"{source}: {where} must be a number")
    return float(value)


def _optional_str_map(value: Any, where: str, source: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ModelsConfigError(f"{source}: {where} must be an object")
    result: dict[str, str] = {}
    for key, entry in value.items():
        if not isinstance(entry, str):
            raise ModelsConfigError(f"{source}: {where}[{key!r}] must be a string")
        result[str(key)] = entry
    return result


def _optional_object(value: Any, where: str, source: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ModelsConfigError(f"{source}: {where} must be an object")
    return dict(value)


def _optional_input(value: Any, where: str, source: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ModelsConfigError(f"{source}: {where} must be an array")
    modes: list[str] = []
    for mode in value:
        if mode not in _INPUT_MODES:
            raise ModelsConfigError(
                f"{source}: {where} entries must be one of {list(_INPUT_MODES)}, got {mode!r}"
            )
        modes.append(mode)
    return modes


def _optional_thinking_level_map(value: Any, where: str, source: str) -> dict[str, str | None] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ModelsConfigError(f"{source}: {where} must be an object")
    result: dict[str, str | None] = {}
    for level, mapping in value.items():
        if level not in EXTENDED_THINKING_LEVELS:
            raise ModelsConfigError(f"{source}: {where} has unknown thinking level {level!r}")
        if mapping is not None and not isinstance(mapping, str):
            raise ModelsConfigError(f"{source}: {where}[{level!r}] must be a string or null")
        result[str(level)] = mapping
    return result


def _normalize_cost(raw: Any, where: str, source: str) -> dict[str, Any]:
    """Validate a ``cost`` block, keeping only the fields that were specified."""
    cost = _require_object(raw, where, source)
    normalized: dict[str, Any] = {}
    for py_key, raw_key in _COST_RATE_KEYS:
        if raw_key in cost and cost[raw_key] is not None:
            normalized[py_key] = _optional_number(cost[raw_key], f"{where}.{raw_key}", source)
    if cost.get("tiers"):
        if not isinstance(cost["tiers"], list):
            raise ModelsConfigError(f"{source}: {where}.tiers must be an array")
        tiers: list[dict[str, Any]] = []
        for index, tier in enumerate(cost["tiers"]):
            tier_where = f"{where}.tiers[{index}]"
            tier_obj = _require_object(tier, tier_where, source)
            above = _optional_int(tier_obj.get("inputTokensAbove"), f"{tier_where}.inputTokensAbove", source)
            if above is None:
                raise ModelsConfigError(f"{source}: {tier_where}.inputTokensAbove is required")
            entry: dict[str, Any] = {"input_tokens_above": above}
            for py_key, raw_key in _COST_RATE_KEYS:
                value = _optional_number(tier_obj.get(raw_key), f"{tier_where}.{raw_key}", source)
                entry[py_key] = 0.0 if value is None else value
            tiers.append(entry)
        normalized["tiers"] = tiers
    return normalized


# --------------------------------------------------------------------------------------
# models.json normalisation
# --------------------------------------------------------------------------------------


def _normalize_models_config(parsed: Any, *, source: str) -> dict[str, dict[str, Any]]:
    """Turn a models.json-shaped document into a provider-id keyed overlay.

    Only ``id`` is required for a model.  Provider-level and model-level
    ``compat`` are shallow-merged here (model keys win), so downstream code can
    treat ``model["compat"]`` as the effective set of compatibility switches.
    """
    root = _require_object(parsed, "the document root", source)
    raw_providers = root.get("providers") or {}
    providers_obj = _require_object(raw_providers, "'providers'", source)

    providers: dict[str, dict[str, Any]] = {}
    for provider_id, raw_provider in providers_obj.items():
        where = f"provider {provider_id!r}"
        provider = _require_object(raw_provider, where, source)
        provider_compat = _optional_object(provider.get("compat"), f"{where}.compat", source)
        entry: dict[str, Any] = {
            "id": str(provider_id),
            "name": _optional_str(provider.get("name"), f"{where}.name", source),
            "api": _optional_str(provider.get("api"), f"{where}.api", source),
            "base_url": _optional_str(provider.get("baseUrl"), f"{where}.baseUrl", source),
            "headers": _optional_str_map(provider.get("headers"), f"{where}.headers", source),
            "compat": provider_compat,
            "models": [],
        }

        raw_models = provider.get("models") or []
        if not isinstance(raw_models, list):
            raise ModelsConfigError(f"{source}: {where}.models must be an array")
        for index, raw_model in enumerate(raw_models):
            model_where = f"{where}.models[{index}]"
            model = _require_object(raw_model, model_where, source)
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id:
                raise ModelsConfigError(f"{source}: {model_where} is missing a non-empty 'id'")
            model_compat = _optional_object(model.get("compat"), f"{model_where}.compat", source)
            merged_compat: dict[str, Any] | None = None
            if provider_compat or model_compat:
                merged_compat = {**(provider_compat or {}), **(model_compat or {})}

            normalized: dict[str, Any] = {"id": model_id, "provider": str(provider_id)}
            if "name" in model:
                normalized["name"] = _optional_str(model["name"], f"{model_where}.name", source)
            if "api" in model:
                normalized["api"] = _optional_str(model["api"], f"{model_where}.api", source)
            if "baseUrl" in model:
                normalized["base_url"] = _optional_str(model["baseUrl"], f"{model_where}.baseUrl", source)
            if "reasoning" in model:
                normalized["reasoning"] = _optional_bool(
                    model["reasoning"], f"{model_where}.reasoning", source
                )
            if "thinkingLevelMap" in model:
                normalized["thinking_level_map"] = _optional_thinking_level_map(
                    model["thinkingLevelMap"], f"{model_where}.thinkingLevelMap", source
                )
            if "input" in model:
                normalized["input"] = _optional_input(model["input"], f"{model_where}.input", source)
            if "cost" in model:
                normalized["cost"] = _normalize_cost(model["cost"], f"{model_where}.cost", source)
            if "contextWindow" in model:
                normalized["context_window"] = _optional_int(
                    model["contextWindow"], f"{model_where}.contextWindow", source
                )
            if "maxTokens" in model:
                normalized["max_tokens"] = _optional_int(
                    model["maxTokens"], f"{model_where}.maxTokens", source
                )
            if "samplingParams" in model:
                normalized["sampling_params"] = _optional_object(
                    model["samplingParams"], f"{model_where}.samplingParams", source
                )
            if "headers" in model:
                normalized["headers"] = _optional_str_map(model["headers"], f"{model_where}.headers", source)
            if merged_compat is not None:
                normalized["compat"] = merged_compat
            entry["models"].append(normalized)

        providers[str(provider_id)] = entry
    return providers


def load_models_json(path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Parse a ``models.json`` file (JSONC tolerated) into a normalised overlay.

    The returned mapping is keyed by provider id.  Each provider value carries
    ``name`` / ``api`` / ``base_url`` / ``headers`` / ``compat`` / ``models``;
    each model entry keeps only the fields the file actually set, with Python
    snake_case names and ``provider`` filled in from the group key.  Cost fields
    use ``input``/``output``/``cache_read``/``cache_write`` plus ``tiers``.

    Raises :class:`ModelsConfigError` for a malformed document or an entry
    without a non-empty ``id``, and :class:`FileNotFoundError` when the path
    does not exist.
    """
    file_path = Path(path).expanduser()
    text = file_path.read_text(encoding="utf-8-sig")
    parsed = _parse_jsonc(text, source=str(file_path))
    return _normalize_models_config(parsed, source=str(file_path))


# --------------------------------------------------------------------------------------
# Model construction / merging
# --------------------------------------------------------------------------------------


def _merge_maps(*layers: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Shallow-merge mappings; later layers win.  ``None`` when all are empty."""
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer:
            merged.update(layer)
    return merged or None


def _merge_headers(*layers: Mapping[str, str] | None) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for layer in layers:
        if layer:
            merged.update(layer)
    return merged or None


def _merge_cost(existing: ModelCost | None, override: Mapping[str, Any] | None) -> ModelCost:
    base = existing if existing is not None else ModelCost()
    rates = {
        "input": base.input,
        "output": base.output,
        "cache_read": base.cache_read,
        "cache_write": base.cache_write,
    }
    tiers = list(base.tiers)
    if override:
        for key in rates:
            if key in override and override[key] is not None:
                rates[key] = override[key]
        if override.get("tiers"):
            tiers = [
                ModelCostTier(
                    input_tokens_above=tier["input_tokens_above"],
                    input=tier["input"],
                    output=tier["output"],
                    cache_read=tier["cache_read"],
                    cache_write=tier["cache_write"],
                )
                for tier in override["tiers"]
            ]
    return ModelCost(tiers=tiers, **rates)


def _build_model(
    provider_id: str,
    provider_entry: Mapping[str, Any],
    model_entry: Mapping[str, Any],
    *,
    existing: Model | None,
    source: str,
) -> Model:
    """Build one model from an overlay entry, inheriting from ``existing``/provider.

    A same-id overlay entry *replaces* the builtin model, but fields the overlay
    does not mention are inherited rather than reset to defaults; otherwise a
    one-line price tweak would silently wipe the model's modalities and context
    window.  Genuinely new models get upstream's documented defaults
    (``name=id``, ``reasoning=false``, ``input=["text"]``, zero cost,
    128000/16384 windows).
    """
    model_id = str(model_entry["id"])
    where = f"model {provider_id}/{model_id}"

    api = model_entry.get("api") or provider_entry.get("api") or (existing.api if existing else None)
    base_url = (
        model_entry.get("base_url")
        or provider_entry.get("base_url")
        or (existing.base_url if existing else None)
    )
    if not api:
        raise ModelsConfigError(f"{source}: {where} is missing 'api' (and no provider-level api)")
    if not base_url:
        raise ModelsConfigError(f"{source}: {where} is missing 'baseUrl' (and no provider-level baseUrl)")

    name = model_entry.get("name") or (existing.name if existing else None) or model_id
    reasoning = model_entry.get("reasoning")
    if reasoning is None:
        reasoning = existing.reasoning if existing is not None else False
    input_modes = model_entry.get("input")
    if input_modes is None:
        input_modes = list(existing.input) if existing is not None else ["text"]
    cost = _merge_cost(existing.cost if existing is not None else None, model_entry.get("cost"))
    context_window = model_entry.get("context_window")
    if context_window is None:
        context_window = existing.context_window if existing is not None else DEFAULT_CONTEXT_WINDOW
    max_tokens = model_entry.get("max_tokens")
    if max_tokens is None:
        max_tokens = existing.max_tokens if existing is not None else DEFAULT_MAX_TOKENS

    thinking_level_map = model_entry.get("thinking_level_map")
    if thinking_level_map is None and existing is not None:
        thinking_level_map = existing.thinking_level_map
    sampling_params = model_entry.get("sampling_params")
    if sampling_params is None and existing is not None:
        sampling_params = existing.sampling_params

    headers = _merge_headers(
        existing.headers if existing is not None else None,
        provider_entry.get("headers"),
        model_entry.get("headers"),
    )
    compat = _merge_maps(
        existing.compat if existing is not None else None,
        provider_entry.get("compat"),
        model_entry.get("compat"),
    )

    return Model(
        id=model_id,
        name=name,
        api=api,
        provider=provider_id,
        base_url=base_url,
        reasoning=bool(reasoning),
        input=list(input_modes),
        cost=cost,
        context_window=context_window,
        max_tokens=max_tokens,
        thinking_level_map=dict(thinking_level_map) if thinking_level_map else None,
        sampling_params=dict(sampling_params) if sampling_params else None,
        headers=headers,
        compat=compat,
    )


def _patch_provider_layer(model: Model, provider_entry: Mapping[str, Any]) -> Model:
    """Apply models.json provider-level fields to an already-known model."""
    api = provider_entry.get("api") or model.api
    base_url = provider_entry.get("base_url") or model.base_url
    headers = _merge_headers(model.headers, provider_entry.get("headers"))
    compat = _merge_maps(model.compat, provider_entry.get("compat"))
    if api == model.api and base_url == model.base_url and headers == model.headers and compat == model.compat:
        return model
    return replace(model, api=api, base_url=base_url, headers=headers, compat=compat)


# --------------------------------------------------------------------------------------
# Builtin data pack
# --------------------------------------------------------------------------------------

_BUILTIN_PACKAGE = "pi_ai"
_BUILTIN_RESOURCE = ("data", "models.json")


def _load_builtin_config() -> dict[str, dict[str, Any]]:
    resource = files(_BUILTIN_PACKAGE)
    for part in _BUILTIN_RESOURCE:
        resource = resource.joinpath(part)
    text = resource.read_text(encoding="utf-8")
    return _normalize_models_config(_parse_jsonc(text, source=str(resource)), source=str(resource))


def _load_builtin_catalog() -> dict[str, dict[str, Model]]:
    config = _load_builtin_config()
    catalog: dict[str, dict[str, Model]] = {}
    for provider_id, provider_entry in config.items():
        models: dict[str, Model] = {}
        for model_entry in provider_entry["models"]:
            model = _build_model(
                provider_id, provider_entry, model_entry, existing=None, source="builtin catalog"
            )
            models[model.id] = model
        catalog[provider_id] = models
    return catalog


#: The parsed builtin data pack: ``provider id -> model id -> Model``.  Loaded at
#: import time so packaging mistakes surface immediately and lookups are pure.
BUILTIN_CATALOG: dict[str, dict[str, Model]] = _load_builtin_catalog()


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    """A catalog match plus the optional thinking level parsed from the pattern."""

    model: Model
    thinking_level: str | None = None


def _split_thinking_suffix(pattern: str) -> tuple[str, str | None]:
    """Strip a trailing ``:<level>`` when (and only when) it names a real level.

    Model ids may themselves contain ``:`` (OpenRouter-style ids, ollama tags),
    so an unknown suffix is left in the pattern instead of being consumed.
    """
    index = pattern.rfind(":")
    if index == -1:
        return pattern, None
    suffix = pattern[index + 1 :]
    if suffix in EXTENDED_THINKING_LEVELS:
        return pattern[:index], suffix
    return pattern, None


class ModelRegistry:
    """Offline-first model catalog with auth-aware availability.

    ``auth`` powers :meth:`get_available` (and ``resolve(..., available_only=True)``);
    ``models_path`` selects the overlay file.  Passing ``None`` uses
    :data:`DEFAULT_MODELS_PATH` when it exists.  There is intentionally no
    ``refresh()``: remote catalogs are out of scope for v0.1.
    """

    def __init__(
        self,
        auth: AuthStorage | None = None,
        *,
        models_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._auth = auth if auth is not None else AuthStorage()
        self._catalog: dict[str, dict[str, Model]] = {
            provider_id: dict(models) for provider_id, models in BUILTIN_CATALOG.items()
        }
        self.overlay_path = (
            Path(models_path).expanduser() if models_path is not None else DEFAULT_MODELS_PATH
        )
        if self.overlay_path.is_file():
            self.apply_overlay(load_models_json(self.overlay_path))

    # -- catalog access --------------------------------------------------------------

    def get(self, provider: str, model_id: str) -> Model | None:
        """Look up one model, tolerating case differences in provider or id."""
        models = self._catalog.get(provider)
        if models is None:
            lowered = provider.lower()
            for provider_id, group in self._catalog.items():
                if provider_id.lower() == lowered:
                    models = group
                    break
        if models is None:
            return None
        model = models.get(model_id)
        if model is not None:
            return model
        lowered_id = model_id.lower()
        for candidate_id, candidate in models.items():
            if candidate_id.lower() == lowered_id:
                return candidate
        return None

    def get_model(self, provider: str, model_id: str) -> Model | None:
        """Alias for :meth:`get` (upstream spells the runtime lookup this way)."""
        return self.get(provider, model_id)

    def list_models(self) -> list[Model]:
        """Every catalog model, provider ids sorted for deterministic output."""
        return [model for provider in sorted(self._catalog) for model in self._catalog[provider].values()]

    def get_providers(self) -> list[str]:
        return sorted(self._catalog)

    def get_provider_models(self, provider: str) -> list[Model]:
        models = self._catalog.get(provider)
        return list(models.values()) if models is not None else []

    # -- overlay ---------------------------------------------------------------------

    def apply_overlay(self, overlay: Mapping[str, Mapping[str, Any]]) -> None:
        """Merge a normalised overlay (see :func:`load_models_json`) into the catalog."""
        for provider_id, provider_entry in overlay.items():
            group = self._catalog.setdefault(provider_id, {})
            if group:
                for model_id, model in list(group.items()):
                    group[model_id] = _patch_provider_layer(model, provider_entry)
            for model_entry in provider_entry.get("models", ()):
                model_id = model_entry["id"]
                group[model_id] = _build_model(
                    provider_id,
                    provider_entry,
                    model_entry,
                    existing=group.get(model_id),
                    source=str(self.overlay_path),
                )

    # -- auth-aware queries ----------------------------------------------------------

    def has_configured_auth(self, provider: str) -> bool:
        return self._auth.has_auth(provider)

    async def get_available(self) -> list[Model]:
        """Only models whose provider has usable auth (stored, runtime or env)."""
        return [model for model in self.list_models() if self._auth.has_auth(model.provider)]

    # -- pattern resolution ----------------------------------------------------------

    def resolve(self, pattern: str, *, available_only: bool = False) -> list[ResolvedModel]:
        """Resolve ``provider/id``, ``provider/*`` or a bare id, with ``:level``.

        Wildcards use :mod:`fnmatch`, case-insensitively: a pattern containing
        ``/`` matches ``provider/id``, a pattern without one matches the bare id.
        That keeps ``anthropic/*`` scoped to anthropic even when another provider
        (OpenRouter) has ids that start with ``anthropic/``.  A ``:thinking``
        suffix is only consumed when it names a real level, which keeps ids such
        as ``llama3.1:8b`` resolvable.
        """
        base, thinking_level = _split_thinking_suffix(pattern)
        if any(char in base for char in "*?["):
            matches = self._glob_matches(base)
        else:
            matches = self._exact_matches(base)
        if available_only:
            matches = [model for model in matches if self._auth.has_auth(model.provider)]
        return [ResolvedModel(model=model, thinking_level=thinking_level) for model in matches]

    def _exact_matches(self, pattern: str) -> list[Model]:
        if "/" in pattern:
            provider, _, model_id = pattern.partition("/")
            model = self.get(provider, model_id)
            return [model] if model is not None else []
        lowered = pattern.lower()
        matches: list[Model] = []
        for provider in sorted(self._catalog):
            for model_id, model in self._catalog[provider].items():
                if model_id.lower() == lowered:
                    matches.append(model)
        return matches

    def _glob_matches(self, pattern: str) -> list[Model]:
        lowered = pattern.lower()
        scoped = "/" in pattern
        matches: list[Model] = []
        for provider in sorted(self._catalog):
            for model_id, model in self._catalog[provider].items():
                target = f"{provider}/{model_id}" if scoped else model_id
                if fnmatch.fnmatchcase(target.lower(), lowered):
                    matches.append(model)
        return matches


# --------------------------------------------------------------------------------------
# Module-level convenience
# --------------------------------------------------------------------------------------

_DEFAULT_REGISTRY: ModelRegistry | None = None


def _default_registry() -> ModelRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ModelRegistry()
    return _DEFAULT_REGISTRY


def get_model(
    provider: str,
    model_id: str,
    *,
    models_path: str | os.PathLike[str] | None = None,
) -> Model | None:
    """Look up a builtin model, or one from an overlay when ``models_path`` is given.

    Without an explicit path the shared default registry is used: the builtin
    pack merged with ``~/.pi/agent/models.json`` when that file exists.
    """
    if models_path is None:
        return _default_registry().get(provider, model_id)
    return ModelRegistry(models_path=models_path).get(provider, model_id)
