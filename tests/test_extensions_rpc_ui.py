"""Wire-level tests for the RPC extension UI channel.

Responses arrive from a different task than the one parked on a dialog, so every
blocking test follows the same shape: start the coroutine with
``asyncio.ensure_future``, let it reach its ``await``, assert the exact record it
emitted, then ``resolve()`` and await the task.  Requests are asserted as whole
dicts -- key names are the protocol, and a stray ``status_key`` would be invisible
to a per-field check.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from typing import Any

from pi_coding_agent.extensions.api import UIContext
from pi_coding_agent.extensions.rpc_ui import RpcUIContext, stream_emitter

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def make_ctx(**kwargs: Any) -> tuple[RpcUIContext, list[dict[str, Any]]]:
    """An RpcUIContext whose emit appends to a list, plus that list."""
    sent: list[dict[str, Any]] = []
    return RpcUIContext(sent.append, **kwargs), sent


async def wait_for_requests(sent: list[dict[str, Any]], count: int = 1) -> None:
    """Yield to the loop until the coroutine under test has emitted ``count`` records."""
    for _ in range(100):
        if len(sent) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} request(s), saw {len(sent)}")


def ids_of(sent: list[dict[str, Any]]) -> list[str]:
    return [request["id"] for request in sent]


# --------------------------------------------------------------------------------------
# Fire-and-forget: exact wire shape
# --------------------------------------------------------------------------------------


def test_notify_wire_shape() -> None:
    ctx, sent = make_ctx()
    ctx.notify("build failed", "error")

    request = sent[0]
    assert request == {
        "type": "extension_ui_request",
        "id": request["id"],
        "method": "notify",
        "message": "build failed",
        "notifyType": "error",
    }
    assert isinstance(request["id"], str) and request["id"]


def test_notify_defaults_to_info() -> None:
    ctx, sent = make_ctx()
    ctx.notify("hello")
    assert sent[0]["notifyType"] == "info"


def test_set_status_wire_shape() -> None:
    ctx, sent = make_ctx()
    ctx.set_status("build", "compiling")
    ctx.set_status("build", None)

    assert sent[0] == {
        "type": "extension_ui_request",
        "id": sent[0]["id"],
        "method": "setStatus",
        "statusKey": "build",
        "statusText": "compiling",
    }
    # Clearing must travel as an explicit null, not as an omitted key.
    assert sent[1] == {
        "type": "extension_ui_request",
        "id": sent[1]["id"],
        "method": "setStatus",
        "statusKey": "build",
        "statusText": None,
    }
    assert "statusText" in sent[1]


def test_set_widget_wire_shape() -> None:
    ctx, sent = make_ctx()
    ctx.set_widget("todo", ["one", "two"], "belowEditor")
    ctx.set_widget("todo", None)

    assert sent[0] == {
        "type": "extension_ui_request",
        "id": sent[0]["id"],
        "method": "setWidget",
        "widgetKey": "todo",
        "widgetLines": ["one", "two"],
        "widgetPlacement": "belowEditor",
    }
    # No placement given: the key is optional upstream and must be omitted.
    assert sent[1] == {
        "type": "extension_ui_request",
        "id": sent[1]["id"],
        "method": "setWidget",
        "widgetKey": "todo",
        "widgetLines": None,
    }


def test_set_title_wire_shape() -> None:
    ctx, sent = make_ctx()
    ctx.set_title("pi - session")

    assert sent[0] == {
        "type": "extension_ui_request",
        "id": sent[0]["id"],
        "method": "setTitle",
        "title": "pi - session",
    }


def test_set_editor_text_wire_shape() -> None:
    ctx, sent = make_ctx()
    ctx.set_editor_text("draft text")

    assert sent[0] == {
        "type": "extension_ui_request",
        "id": sent[0]["id"],
        "method": "set_editor_text",
        "text": "draft text",
    }


def test_fire_and_forget_never_blocks_and_ids_are_unique() -> None:
    ctx, sent = make_ctx()

    # Called outside any event loop on purpose: these must stay synchronous.
    assert ctx.notify("a") is None
    assert ctx.set_status("k", "v") is None
    assert ctx.set_widget("w", ["l"]) is None
    assert ctx.set_title("t") is None
    assert ctx.set_editor_text("e") is None

    assert len(sent) == 5
    assert len(set(ids_of(sent))) == 5
    assert ctx.pending_count() == 0


# --------------------------------------------------------------------------------------
# Blocking round-trips
# --------------------------------------------------------------------------------------


async def test_select_round_trip_and_wire_shape() -> None:
    ctx, sent = make_ctx()
    task = asyncio.ensure_future(ctx.select("Pick one", ["alpha", "beta"]))
    await wait_for_requests(sent)

    request = sent[0]
    assert request == {
        "type": "extension_ui_request",
        "id": request["id"],
        "method": "select",
        "title": "Pick one",
        "options": ["alpha", "beta"],
    }
    assert ctx.pending_count() == 1

    assert ctx.resolve({"type": "extension_ui_response", "id": request["id"], "value": "beta"}) is True
    assert await task == "beta"
    assert ctx.pending_count() == 0


async def test_confirm_round_trips_both_answers() -> None:
    for confirmed in (True, False):
        ctx, sent = make_ctx()
        task = asyncio.ensure_future(ctx.confirm("Clear session?", "All messages are lost."))
        await wait_for_requests(sent)

        request = sent[0]
        assert request == {
            "type": "extension_ui_request",
            "id": request["id"],
            "method": "confirm",
            "title": "Clear session?",
            "message": "All messages are lost.",
        }
        assert ctx.resolve(
            {"type": "extension_ui_response", "id": request["id"], "confirmed": confirmed}
        )
        assert await task is confirmed


async def test_input_round_trip_and_wire_shape() -> None:
    ctx, sent = make_ctx()
    task = asyncio.ensure_future(ctx.input("Branch name", "feature/", timeout=2.5))
    await wait_for_requests(sent)

    request = sent[0]
    # Python seconds (2.5) must reach the wire as pi's milliseconds (2500).
    assert request == {
        "type": "extension_ui_request",
        "id": request["id"],
        "method": "input",
        "title": "Branch name",
        "placeholder": "feature/",
        "timeout": 2500,
    }
    assert ctx.resolve({"type": "extension_ui_response", "id": request["id"], "value": "main"}) is True
    assert await task == "main"


async def test_input_without_placeholder_emits_null_placeholder() -> None:
    ctx, sent = make_ctx()
    task = asyncio.ensure_future(ctx.input("Name"))
    await wait_for_requests(sent)

    assert sent[0] == {
        "type": "extension_ui_request",
        "id": sent[0]["id"],
        "method": "input",
        "title": "Name",
        "placeholder": None,
    }
    ctx.resolve({"type": "extension_ui_response", "id": sent[0]["id"], "value": "x"})
    assert await task == "x"


async def test_editor_round_trip_and_wire_shape() -> None:
    ctx, sent = make_ctx()
    task = asyncio.ensure_future(ctx.editor("Commit message", "wip: "))
    await wait_for_requests(sent)

    request = sent[0]
    # ``editor`` has no timeout field upstream, even when a default is configured.
    assert request == {
        "type": "extension_ui_request",
        "id": request["id"],
        "method": "editor",
        "title": "Commit message",
        "prefill": "wip: ",
    }
    assert ctx.resolve({"type": "extension_ui_response", "id": request["id"], "value": "fix: bug"}) is True
    assert await task == "fix: bug"


async def test_default_timeout_applies_and_is_sent_in_milliseconds() -> None:
    ctx, sent = make_ctx(default_timeout=0.05)
    task = asyncio.ensure_future(ctx.select("Pick", ["a"]))
    await wait_for_requests(sent)

    assert sent[0]["timeout"] == 50
    assert await task is None
    assert ctx.pending_count() == 0


# --------------------------------------------------------------------------------------
# Cancellation and degradation
# --------------------------------------------------------------------------------------


async def test_cancelled_yields_safe_defaults() -> None:
    cases: list[tuple[str, Callable[[RpcUIContext], Any], Any]] = [
        ("select", lambda ctx: ctx.select("t", ["a"]), None),
        ("confirm", lambda ctx: ctx.confirm("t", "m"), False),
        ("input", lambda ctx: ctx.input("t"), None),
        ("editor", lambda ctx: ctx.editor("t"), None),
    ]
    for label, start, expected in cases:
        ctx, sent = make_ctx()
        task = asyncio.ensure_future(start(ctx))
        await wait_for_requests(sent)

        assert ctx.resolve(
            {"type": "extension_ui_response", "id": sent[0]["id"], "cancelled": True}
        ) is True, label
        assert await task == expected, label
        assert ctx.pending_count() == 0, label


async def test_unknown_id_returns_false_and_leaves_other_requests_pending() -> None:
    ctx, sent = make_ctx()
    select_task = asyncio.ensure_future(ctx.select("Pick", ["a", "b"]))
    await wait_for_requests(sent)
    request_id = sent[0]["id"]

    assert ctx.resolve({"type": "extension_ui_response", "id": "not-a-request", "value": "a"}) is False
    assert ctx.pending_count() == 1
    assert not select_task.done()

    # The real answer still works after the stale one was ignored.
    assert ctx.resolve({"type": "extension_ui_response", "id": request_id, "value": "a"}) is True
    assert await select_task == "a"

    # A response with no usable id, and one after completion, are both unknown.
    assert ctx.resolve({"type": "extension_ui_response", "value": "a"}) is False
    assert ctx.resolve({"type": "extension_ui_response", "id": request_id, "value": "b"}) is False


async def test_concurrent_blocking_requests_resolve_independently() -> None:
    ctx, sent = make_ctx()
    select_task = asyncio.ensure_future(ctx.select("Pick", ["a", "b"]))
    confirm_task = asyncio.ensure_future(ctx.confirm("Sure?", "Really?"))
    await wait_for_requests(sent, 2)

    select_id, confirm_id = ids_of(sent)
    assert select_id != confirm_id
    assert ctx.pending_count() == 2

    # Answer the second request first: correlation must come from the id, not order.
    assert ctx.resolve({"type": "extension_ui_response", "id": confirm_id, "confirmed": True}) is True
    assert await confirm_task is True
    assert not select_task.done()
    assert ctx.pending_count() == 1

    assert ctx.resolve({"type": "extension_ui_response", "id": select_id, "value": "b"}) is True
    assert await select_task == "b"
    assert ctx.pending_count() == 0


async def test_timeout_yields_safe_default_and_does_not_leak() -> None:
    ctx, sent = make_ctx()
    task = asyncio.ensure_future(ctx.select("Pick", ["a"], timeout=0.05))
    await wait_for_requests(sent)
    assert sent[0]["timeout"] == 50

    assert await task is None
    assert ctx.pending_count() == 0

    # A late response for an expired request is stale, not a resurrection.
    assert ctx.resolve({"type": "extension_ui_response", "id": sent[0]["id"], "value": "a"}) is False


async def test_confirm_timeout_yields_false() -> None:
    ctx, sent = make_ctx()
    task = asyncio.ensure_future(ctx.confirm("Sure?", "Really?", timeout=0.05))
    await wait_for_requests(sent)

    assert await task is False
    assert ctx.pending_count() == 0


async def test_cancel_all_unblocks_everything_and_is_idempotent() -> None:
    ctx, sent = make_ctx()
    select_task = asyncio.ensure_future(ctx.select("Pick", ["a"]))
    confirm_task = asyncio.ensure_future(ctx.confirm("Sure?", "Really?"))
    editor_task = asyncio.ensure_future(ctx.editor("Note"))
    await wait_for_requests(sent, 3)
    assert ctx.pending_count() == 3

    ctx.cancel_all()
    assert await select_task is None
    assert await confirm_task is False
    assert await editor_task is None
    assert ctx.pending_count() == 0

    # Idempotent: a second shutdown must not raise or disturb anything.
    ctx.cancel_all("already gone")
    assert ctx.pending_count() == 0

    # And a cancelled request's late answer is stale.
    assert ctx.resolve({"type": "extension_ui_response", "id": sent[0]["id"], "value": "a"}) is False


async def test_pending_count_tracks_requests() -> None:
    ctx, sent = make_ctx()
    assert ctx.pending_count() == 0

    task = asyncio.ensure_future(ctx.input("Name"))
    await wait_for_requests(sent)
    assert ctx.pending_count() == 1

    ctx.resolve({"type": "extension_ui_response", "id": sent[0]["id"], "value": "n"})
    assert await task == "n"
    assert ctx.pending_count() == 0


# --------------------------------------------------------------------------------------
# Framing helper and contract
# --------------------------------------------------------------------------------------


def test_stream_emitter_writes_exactly_one_lf_terminated_record() -> None:
    stream = io.StringIO()
    ctx = RpcUIContext(stream_emitter(stream))
    ctx.notify("hi")

    line = stream.getvalue()
    assert line.count("\n") == 1
    assert line.endswith("\n")
    assert json.loads(line) == {
        "type": "extension_ui_request",
        "id": json.loads(line)["id"],
        "method": "notify",
        "message": "hi",
        "notifyType": "info",
    }


def test_rpc_ui_context_is_a_ui_context() -> None:
    ctx, _ = make_ctx()
    assert isinstance(ctx, UIContext)
