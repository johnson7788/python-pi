"""Typed application errors: one code per failure the client can act on.

Why not ``HTTPException`` directly
----------------------------------
``raise HTTPException(status_code=409, detail={"error": "..."})`` appears ~120 times
across the server.  A client can only tell failures apart by matching the message
text, which means every wording change is a breaking change and the browser cannot
implement "re-login" versus "retry" without parsing Chinese.

:class:`AppError` keeps the same wire shape — ``{"detail": {"error": ...}}`` — and
adds ``code``.  Codes are stable strings, grouped by band:

* ``1xxx`` input and identity (``BAD_REQUEST`` … ``FORBIDDEN``)
* ``2xxx`` things that do not exist (``NOT_FOUND``, ``SESSION_NOT_FOUND`` …)
* ``3xxx`` state conflicts (``CONFLICT``, ``RATE_LIMITED``, ``UNAVAILABLE``)
* ``9xxx`` internal (``INTERNAL``, ``UPSTREAM_ERROR``)

The HTTP status is carried on the error, so a handler never chooses a status and a
message at the same time — the two cannot drift apart.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    # -- 1xxx: the request or the caller -------------------------------------------
    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    TEAM_MODE_REQUIRED = "TEAM_MODE_REQUIRED"
    # -- 2xxx: the thing asked for -------------------------------------------------
    NOT_FOUND = "NOT_FOUND"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    # -- 3xxx: state conflicts -----------------------------------------------------
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    # -- 9xxx: ours ----------------------------------------------------------------
    INTERNAL = "INTERNAL"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"


#: The default code for a status, so a migrated call site gets a useful code even
#: before anyone chooses a domain-specific one.
DEFAULT_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL,
    502: ErrorCode.UPSTREAM_ERROR,
    503: ErrorCode.UNAVAILABLE,
}


class AppError(Exception):
    """A failure with a stable code, a status and a human message."""

    def __init__(
        self,
        code: ErrorCode | str,
        http_status: int,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code if isinstance(code, ErrorCode) else ErrorCode(code)
        self.http_status = int(http_status)
        self.message = str(message)
        self.details = dict(details) if details else {}

    def to_payload(self) -> dict[str, Any]:
        """The response body, keeping the shape existing clients already read.

        ``detail.error`` stays the message, ``detail.code`` is new, and extra fields
        (``schedules``, ``owner``) are flattened next to them exactly where they used
        to be — a client that read ``detail.schedules`` keeps working.
        """
        body: dict[str, Any] = {**self.details, "error": self.message, "code": self.code.value}
        return {"detail": body}

    def to_ws_error(self, command: str = "server", request_id: str | None = None) -> dict[str, Any]:
        """The same failure on the WebSocket, where records are flat."""
        record: dict[str, Any] = {
            "type": "response",
            "command": command,
            "success": False,
            "error": self.message,
            "code": self.code.value,
        }
        if request_id:
            record["id"] = request_id
        if self.details:
            record["details"] = self.details
        return record

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AppError({self.code.value}, {self.http_status}, {self.message!r})"


def error(
    http_status: int,
    message: str,
    *,
    code: ErrorCode | str | None = None,
    details: Mapping[str, Any] | None = None,
) -> AppError:
    """Build an :class:`AppError`, defaulting the code from the status."""
    resolved = code if code is not None else DEFAULT_CODES.get(http_status, ErrorCode.INTERNAL)
    return AppError(resolved, http_status, message, details)


def install_error_handlers(app: Any) -> None:
    """Register the one place an :class:`AppError` becomes an HTTP response.

    Imported lazily by the caller (it needs FastAPI); registering here keeps the
    shape in one place instead of once per router.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())


__all__ = [
    "AppError",
    "DEFAULT_CODES",
    "ErrorCode",
    "error",
    "install_error_handlers",
]
