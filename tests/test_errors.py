"""Typed errors: one stable code per failure, and the old body shape preserved.

The wire contract matters as much as the codes: existing clients read ``detail.error``
and, for a few endpoints, extra fields such as ``detail.schedules``.  So the new body
is the old one plus ``code`` — not a redesigned envelope.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi_server.errors import (
    DEFAULT_CODES,
    AppError,
    ErrorCode,
    error,
    install_error_handlers,
)


def test_the_status_picks_a_default_code():
    assert error(404, "missing").code is ErrorCode.NOT_FOUND
    assert error(422, "bad").code is ErrorCode.VALIDATION_FAILED
    assert error(409, "conflict").code is ErrorCode.CONFLICT


def test_an_explicit_code_wins():
    failure = error(503, "accounts are disabled", code=ErrorCode.TEAM_MODE_REQUIRED)

    assert failure.code is ErrorCode.TEAM_MODE_REQUIRED
    assert failure.http_status == 503


def test_an_unmapped_status_falls_back_to_internal():
    assert error(418, "teapot").code is ErrorCode.INTERNAL


def test_the_body_keeps_error_and_flattens_details():
    body = error(409, "still in use", details={"schedules": ["nightly"]}).to_payload()

    assert body == {
        "detail": {
            "schedules": ["nightly"],
            "error": "still in use",
            "code": "CONFLICT",
        }
    }


def test_details_cannot_shadow_error_or_code():
    body = error(400, "real message", code=ErrorCode.BAD_REQUEST,
                 details={"error": "spoofed", "code": "SPOOFED"}).to_payload()

    assert body["detail"]["error"] == "real message"
    assert body["detail"]["code"] == "BAD_REQUEST"


def test_it_is_an_exception_with_the_message():
    failure = error(400, "boom")

    assert isinstance(failure, Exception)
    assert str(failure) == "boom"


def test_the_web_socket_form_is_flat():
    record = error(404, "no such session", code=ErrorCode.SESSION_NOT_FOUND).to_ws_error(
        command="prompt", request_id="42"
    )

    assert record == {
        "type": "response",
        "command": "prompt",
        "success": False,
        "error": "no such session",
        "code": "SESSION_NOT_FOUND",
        "id": "42",
    }


def test_every_status_we_use_has_a_code():
    """The migrated call sites rely on these; a missing one silently becomes INTERNAL."""
    for status in (400, 401, 403, 404, 409, 413, 422, 429, 500, 503):
        assert status in DEFAULT_CODES


# --------------------------------------------------------------------------------------
# Through FastAPI
# --------------------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/missing")
    async def missing() -> dict:
        raise error(404, "no such thing", code=ErrorCode.SESSION_NOT_FOUND)

    @app.get("/fine")
    async def fine() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_a_raised_error_becomes_the_typed_response(client: TestClient) -> None:
    response = client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"error": "no such thing", "code": "SESSION_NOT_FOUND"}
    }


def test_successful_routes_are_untouched(client: TestClient) -> None:
    assert client.get("/fine").json() == {"ok": True}


def test_an_app_error_is_not_swallowed_into_a_500() -> None:
    """A handler that raises AppError must not lose its status to the generic path."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/conflict")
    async def conflict() -> dict:
        raise AppError(ErrorCode.CONFLICT, 409, "already exists")

    assert TestClient(app).get("/conflict").status_code == 409
