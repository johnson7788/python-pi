"""Error hierarchy for pi-ai.

Mirrors the failure modes of the TypeScript implementation while using Python
exception idioms.
"""

from __future__ import annotations


class PiAiError(Exception):
    """Base class for every error raised by pi-ai."""


class AuthenticationError(PiAiError):
    """No usable credential could be resolved for a provider.

    ``stream_simple()`` raises this *synchronously* (before returning a stream),
    matching the TypeScript contract.  ``stream()`` must instead encode setup
    failures into the returned stream as an ``error`` event.
    """

    def __init__(self, provider: str, message: str | None = None) -> None:
        self.provider = provider
        super().__init__(message or f"No credentials configured for provider {provider!r}")


class ProviderError(PiAiError):
    """A provider request failed (HTTP error, malformed payload, ...)."""

    def __init__(self, message: str, *, provider: str | None = None, status: int | None = None) -> None:
        self.provider = provider
        self.status = status
        super().__init__(message)


class ModelNotFoundError(PiAiError):
    def __init__(self, provider: str, model_id: str) -> None:
        self.provider = provider
        self.model_id = model_id
        super().__init__(f"Model {model_id!r} not found for provider {provider!r}")


class ToolValidationError(PiAiError):
    """``validate_tool_arguments`` rejected the arguments for a tool call."""

    def __init__(self, tool_name: str, message: str) -> None:
        self.tool_name = tool_name
        super().__init__(message)


class AbortedError(PiAiError):
    """The operation was cancelled through an abort signal."""

    def __init__(self, message: str = "Operation aborted") -> None:
        super().__init__(message)


__all__ = [
    "AbortedError",
    "AuthenticationError",
    "ModelNotFoundError",
    "PiAiError",
    "ProviderError",
    "ToolValidationError",
]
