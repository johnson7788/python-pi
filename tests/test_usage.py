"""Usage accounting and monthly quota.

The accounting tests are unit-level against the repository, where the aggregation rules
live.  The quota test is end to end against the real server, because "a user over budget
cannot start a turn" is a property of the command path, not of a function — and the
assertion that makes it meaningful is that the model was **never called**.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import websockets
from conftest import (
    TEAM_ADMIN_PASSWORD,
    TEAM_USER_PASSWORD,
    FakeOpenAIHandler,
    cookie_header,
    login,
    reset_fake_model,
    team_server,  # noqa: F401 - re-exported so pytest can see the fixture
    ws_prompt,
)

from pi_server.db import Database, month_start, utcnow

# --------------------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "pi.db"))
    try:
        yield database
    finally:
        database.close()


def _usage(uid: str, *, tokens: int = 0, cost: float = 0.0, session: str = "s-1") -> dict:
    return {
        "uid": uid,
        "session_id": session,
        "provider": "local",
        "model_id": "local-test",
        "input_tokens": tokens,
        "output_tokens": 0,
        "cost": cost,
    }


def test_usage_summary_sums_only_the_named_user_and_window(db: Database):
    uid = db.create_user(username="ada", password_hash="x")
    other = db.create_user(username="bob", password_hash="x")

    db.record_usage(**_usage(uid, tokens=100, cost=0.5))
    db.record_usage(**_usage(uid, tokens=50, cost=0.25))
    db.record_usage(**_usage(other, tokens=999, cost=9.0))

    summary = db.usage_summary(uid, month_start())
    assert summary["total_tokens"] == 150
    assert summary["cost"] == pytest.approx(0.75)
    assert summary["turns"] == 2

    # A window that starts after the rows were written sees nothing.
    assert db.usage_summary(uid, "2999-01-01T00:00:00+00:00")["total_tokens"] == 0


def test_usage_breaks_down_by_day_and_session(db: Database):
    uid = db.create_user(username="ada", password_hash="x")
    db.record_usage(**_usage(uid, tokens=10, session="alpha"))
    db.record_usage(**_usage(uid, tokens=90, session="beta"))

    since = month_start()
    by_session = {row["session_id"]: row["total_tokens"] for row in db.usage_by_session(uid, since)}
    assert by_session == {"alpha": 10, "beta": 90}

    by_day = db.usage_by_day(uid, since)
    assert sum(row["total_tokens"] for row in by_day) == 100


def test_the_admin_view_lists_accounts_with_no_usage(db: Database):
    uid = db.create_user(username="ada", password_hash="x")
    db.create_user(username="bob", password_hash="x")
    db.record_usage(**_usage(uid, tokens=7))

    rows = {row["username"]: row for row in db.usage_all_users(month_start())}
    assert rows["ada"]["total_tokens"] == 7
    assert rows["bob"]["total_tokens"] == 0  # present, not missing


# --------------------------------------------------------------------------------------
# Quota, end to end
# --------------------------------------------------------------------------------------


def _member(server, admin: httpx.Client, username: str, **quota: object) -> dict:
    payload: dict[str, object] = {"username": username, "password": TEAM_USER_PASSWORD}
    payload.update(quota)
    response = admin.post("/api/admin/users", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["user"]


def test_usage_is_recorded_from_a_real_turn(team_server):
    admin = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        _member(team_server, admin, "ada")
        member = login(team_server, "ada", TEAM_USER_PASSWORD)
        try:
            reset_fake_model()
            ws_prompt(team_server, cookie_header(member))

            usage = member.get("/api/usage/me").json()
            assert usage["summary"]["total_tokens"] > 0
            assert usage["summary"]["turns"] >= 1
            assert usage["bySession"]  # attributed to the session that spent it
            assert usage["quota"] == {"tokens": None, "cost": None}
        finally:
            member.close()
    finally:
        admin.close()


def test_a_user_over_quota_is_refused_before_the_model_runs(team_server):
    admin = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        # One token of budget is already spent the moment the account exists and runs
        # anything, so the second turn must be refused.
        _member(team_server, admin, "ada", monthlyTokenQuota=1)
        member = login(team_server, "ada", TEAM_USER_PASSWORD)
        try:
            reset_fake_model()
            ws_prompt(team_server, cookie_header(member))

            calls_before = FakeOpenAIHandler.calls
            assert calls_before > 0

            # The refusal has to arrive without reaching the model at all.
            async def attempt() -> dict:
                async with websockets.connect(
                    team_server.ws_url, additional_headers={"Cookie": cookie_header(member)}
                ) as ws:
                    await ws.recv()
                    await ws.recv()
                    await ws.send(json.dumps({"id": "over", "type": "prompt", "message": "again"}))
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        record = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if record.get("id") == "over":
                            return record
                    raise AssertionError("no response to the refused prompt")

            refusal = asyncio.run(attempt())
            assert refusal["success"] is False
            assert "额度" in refusal["error"]
            assert FakeOpenAIHandler.calls == calls_before
        finally:
            member.close()
    finally:
        admin.close()


def test_an_administrator_can_raise_a_quota(team_server):
    admin = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        created = _member(team_server, admin, "ada", monthlyTokenQuota=1)
        raised = admin.patch(
            f"/api/admin/users/{created['uid']}", json={"monthlyTokenQuota": 1_000_000}
        )
        assert raised.status_code == 200, raised.text
        assert raised.json()["user"]["monthlyTokenQuota"] == 1_000_000

        # Clearing it back to unlimited is the same call with an explicit null.
        assert admin.patch(
            f"/api/admin/users/{created['uid']}", json={"monthlyTokenQuota": None}
        ).json()["user"]["monthlyTokenQuota"] is None
    finally:
        admin.close()


def test_usage_views_enforce_roles(team_server):
    admin = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        _member(team_server, admin, "ada")
        member = login(team_server, "ada", TEAM_USER_PASSWORD)
        try:
            assert member.get("/api/usage/me").status_code == 200
            assert member.get("/api/admin/usage").status_code == 403
            assert admin.get("/api/admin/usage").status_code == 200
        finally:
            member.close()
    finally:
        admin.close()


def _recorder_worker() -> tuple[object, list[dict]]:
    from pi_coding_agent.bootstrap import BootstrapOptions
    from pi_server.app import SessionWorker

    recorded: list[dict] = []
    worker = SessionWorker("uid-1", BootstrapOptions(cwd="."), on_usage=recorded.append)
    return worker, recorded


def _assistant_message(tokens: int = 10) -> dict:
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "usage": {"input": tokens, "output": 1, "cost": {"total": 0.001}},
        },
    }


def test_the_observer_counts_each_response_exactly_once():
    """`turn_end` re-sends the same message; counting it would double every turn."""
    worker, recorded = _recorder_worker()

    worker._observe(_assistant_message())
    worker._observe({"type": "turn_end", "message": _assistant_message()["message"]})
    # A user message has no usage to bill.
    worker._observe({"type": "message_end", "message": {"role": "user", "content": "hi"}})
    # An empty usage block is not a billable response.
    worker._observe({"type": "message_end", "message": {"role": "assistant", "usage": {}}})

    assert len(recorded) == 1
    assert recorded[0]["request_kind"] == "turn"
    assert recorded[0]["input_tokens"] == 10
    assert recorded[0]["uid"] == "uid-1"


def test_compaction_is_accounted_as_its_own_kind():
    """Compaction is the most expensive call a session makes and emits no message_end."""
    worker, recorded = _recorder_worker()

    worker._observe(
        {
            "type": "response",
            "command": "compact",
            "success": True,
            "data": {"usage": {"input": 50_000, "output": 400, "cost": {"total": 0.12}}},
        }
    )
    assert recorded[0]["request_kind"] == "compaction"
    assert recorded[0]["input_tokens"] == 50_000

    # A failed compaction spent nothing worth recording, and neither did a non-compact
    # response that happens to carry a usage field.
    worker._observe({"type": "response", "command": "compact", "success": False, "data": {}})
    worker._observe({"type": "response", "command": "get_state", "success": True, "data": {}})
    assert len(recorded) == 1
