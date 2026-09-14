"""LLM-facing tool declarations and argument validation.

Tool parameters are plain JSON Schema dicts (the Python stand-in for TypeBox).
:func:`validate_tool_arguments` mirrors the TypeScript pipeline:

``clone → normalizeOptionalNulls → Value.Convert (coerce) → validate``

The clone matters: validated arguments are handed to hooks that may mutate them
in place, and a later re-validation of the same call would otherwise see the
mutation.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import ToolValidationError


class ToolLike(Protocol):
    """Structural type for anything carrying ``name`` and ``parameters``.

    Lets the agent layer validate its own tool objects without a conversion step.
    """

    name: str
    parameters: dict[str, Any]


@dataclass
class Tool:
    """A tool declaration as sent to the model."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


def _raw_json_value(value: Any, schema: dict[str, Any]) -> str:
    return f"{value!r}"


def normalize_optional_nulls(value: Any, schema: dict[str, Any]) -> Any:
    """Drop ``null`` for properties that are neither required nor nullable.

    Models routinely emit ``{"optional_field": null}``; upstream normalises that
    away before validation so optional fields behave as "absent".
    """
    if not isinstance(schema, dict):
        return value

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        result: dict[str, Any] = {}
        for key, item in value.items():
            child = properties.get(key)
            if item is None:
                if key in required:
                    result[key] = None
                    continue
                if child is not None and child.get("nullable") is True:
                    result[key] = None
                    continue
                # Drop the key entirely.
                continue
            result[key] = normalize_optional_nulls(item, child) if child is not None else item
        return result

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            return [normalize_optional_nulls(v, items) for v in value]
        return value

    return value


def _coerce_scalar(value: Any, target: str) -> Any:
    if target == "number":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text == "":
                return value
            try:
                return float(text) if ("." in text or "e" in text.lower()) else int(text)
            except ValueError:
                return value
    elif target == "integer":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            try:
                return int(text)
            except ValueError:
                return value
    elif target == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
        return value
    elif target == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return value


def coerce(value: Any, schema: dict[str, Any]) -> Any:
    """Best-effort ``TypeBox.Value.Convert`` equivalent."""
    if not isinstance(schema, dict):
        return value

    type_spec = schema.get("type")
    if any_of := schema.get("anyOf"):
        for candidate in any_of:
            if isinstance(candidate, dict) and candidate.get("type") == "null" and value is None:
                return value
        for candidate in any_of:
            if not isinstance(candidate, dict):
                continue
            candidate_type = candidate.get("type")
            if candidate_type == "null":
                continue
            if isinstance(candidate_type, str) and _matches_type(value, candidate_type):
                return coerce(value, candidate)
        for candidate in any_of:
            if isinstance(candidate, dict) and candidate.get("type") not in ("null", None):
                return coerce(value, candidate)
        return value

    if type_spec == "object" or (type_spec is None and isinstance(value, dict)):
        if not isinstance(value, dict):
            return value
        properties = schema.get("properties") or {}
        return {
            key: coerce(item, properties[key]) if key in properties else item
            for key, item in value.items()
        }

    if type_spec == "array" or (type_spec is None and isinstance(value, list)):
        if not isinstance(value, list):
            return value
        items = schema.get("items")
        if isinstance(items, dict):
            return [coerce(item, items) for item in value]
        return value

    if isinstance(type_spec, str):
        return _coerce_scalar(value, type_spec)

    return value


def _matches_type(value: Any, type_spec: str) -> bool:
    if type_spec == "string":
        return isinstance(value, str)
    if type_spec == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_spec == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_spec == "boolean":
        return isinstance(value, bool)
    if type_spec == "array":
        return isinstance(value, list)
    if type_spec == "object":
        return isinstance(value, dict)
    if type_spec == "null":
        return value is None
    return True


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict) or not schema:
        return

    if any_of := schema.get("anyOf"):
        for candidate in any_of:
            if not isinstance(candidate, dict):
                continue
            sub_errors: list[str] = []
            _validate(value, candidate, path, sub_errors)
            if not sub_errors:
                return
        errors.append(f"Expected {path or 'value'} to match one of the allowed schemas")
        return

    if enum := schema.get("enum"):
        if value not in enum:
            errors.append(f"Expected {path or 'value'} to be one of {enum!r}")

    type_spec = schema.get("type")
    if isinstance(type_spec, str) and value is not None:
        if not _matches_type(value, type_spec):
            errors.append(f"Expected {type_spec} for {path or 'value'}")

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"Expected required property {key} at {path or 'root'}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"Unexpected property {key} at {path or 'root'}")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{path}.{key}" if path else key, errors)
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate(item, items, f"{path}[{index}]", errors)


def validate_tool_arguments(tool: ToolLike, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalise a tool call's arguments.

    Raises :class:`~pi_ai.errors.ToolValidationError` with a human-readable
    message; the agent loop converts that into an error tool result.
    """
    raw = copy.deepcopy(arguments or {})
    schema = tool.parameters or {"type": "object", "properties": {}}

    normalized = normalize_optional_nulls(raw, schema)
    converted = coerce(normalized, schema)

    if not isinstance(converted, dict):
        raise ToolValidationError(tool.name, f"Expected object for arguments of {tool.name}")

    errors: list[str] = []
    _validate(converted, schema, "", errors)
    if errors:
        raise ToolValidationError(tool.name, errors[0])

    return converted


__all__ = [
    "Tool",
    "ToolLike",
    "coerce",
    "normalize_optional_nulls",
    "validate_tool_arguments",
]
