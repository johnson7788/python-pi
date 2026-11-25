"""Resuming a tab's stream: sequence numbers, the replay floor, and the wire.

The property under test is narrow and important: a tab that reconnects must not
lose the in-flight turn, and must not receive again what ``get_state`` already
gave it.  The second half is why replay starts at ``lastPersistedSeq`` rather than
at whatever the client claims to have.
"""

from __future__ import annotations

import asyncio
import json
import time


from pi_server.runlog import RunLog, replay_floor

from conftest import (
    TEAM_ADMIN_PASSWORD,
    FakeOpenAIHandler,
    cookie_header,
    login,
    reset_fake_model,
)

# --------------------------------------------------------------------------------------
# The log
# --------------------------------------------------------------------------------------


def test_sequence_numbers_start_at_one_and_are_monotonic():
    log = RunLog()
    assert log.head == 0

    assert log.append({"type": "a"}) == 1
    assert log.append({"type": "b"}) == 2
    assert log.head == 2


def test_since_returns_only_the_records_after_the_floor():
    log = RunLog()
    for index in range(5):
        log.append({"type": "record", "index": index})

    assert [record["index"] for record in log.since(2)] == [2, 3, 4]
    assert log.since(5) == []
    assert len(log.since(0)) == 5


def test_the_log_evicts_the_oldest_records_and_reports_the_gap():
    log = RunLog(capacity=3)
    for index in range(6):
        log.append({"type": "record", "index": index})

    assert [record["index"] for record in log.since(0)] == [3, 4, 5]
    assert log.oldest == 4
    assert log.dropped(0) is True
    assert log.dropped(4) is False


def test_reset_restarts_numbering():
    log = RunLog()
    log.append({"type": "a"})
    log.reset()

    assert log.head == 0
    assert log.append({"type": "b"}) == 1


def test_replay_is_only_worth_it_while_a_message_is_streaming():
    log = RunLog()
    log.append({"type": "message_start", "message": {"role": "assistant"}})
    log.append({"type": "message_end", "message": {"role": "assistant"}})
    log.append({"type": "turn_end"})

    # A message started after the persisted point: there is a partial to restore.
    assert log.has_unpersisted_message(0) is True
    # Everything up to and including that message is persisted: nothing to restore.
    assert log.has_unpersisted_message(2) is False


def test_the_replay_floor_never_goes_below_the_persisted_point():
    # A client that claims to be behind still must not be re-sent persisted data.
    assert replay_floor(0, 12) == 12
    assert replay_floor(20, 12) == 20
    assert replay_floor(-5, 0) == 0


# --------------------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------------------


async def _connect(url: str, cookie: str, *, since: int | None = None):
    import websockets

    target = url if since is None else f"{url}?since={since}"
    ws = await websockets.connect(target, additional_headers={"Cookie": cookie})
    ready = json.loads(await ws.recv())
    state = json.loads(await ws.recv())
    return ws, ready, state


def test_ready_carries_the_stream_position_and_a_completed_turn_has_nothing_to_replay(
    team_server, fake_openai
) -> None:
    reset_fake_model()

    async def scenario() -> tuple[dict, list[dict]]:
        client = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
        cookie = cookie_header(client)
        # One complete turn first.
        async with (await _connect(team_server.ws_url, cookie))[0] as ws:
            await ws.send(json.dumps({"id": "p", "type": "prompt", "message": "hello"}))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                record = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if record.get("type") == "agent_end":
                    break
        # Then a fresh connection: the turn is over, so there is nothing in flight.
        ws2, ready, _state = await _connect(team_server.ws_url, cookie)
        extra: list[dict] = []
        try:
            # Whatever arrives now is *new* activity, not replay.  A finished turn
            # may still emit a late session_activity record; what must never happen
            # is a record the snapshot already covered.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    extra.append(json.loads(await asyncio.wait_for(ws2.recv(), timeout=0.3)))
                except asyncio.TimeoutError:
                    break
        finally:
            await ws2.close()
        return ready, extra

    ready, extra = asyncio.run(scenario())

    assert ready["seq"] > 0
    # The persisted point trails the head by a few bookkeeping records (agent_end,
    # session_activity) that are not messages; it must never be *ahead* of it.
    assert 0 < ready["lastPersistedSeq"] <= ready["seq"]
    # A finished turn has no unpersisted message, so replay is skipped entirely:
    # anything that arrives is new activity, above the snapshot.
    assert [record for record in extra if record.get("seq", 0) <= ready["seq"]] == []


def test_a_reconnect_mid_run_receives_the_unpersisted_tail(team_server, fake_openai) -> None:
    reset_fake_model()
    # Trickle the SSE events: the point is a run that is *mid-message*, which is the
    # only state replay exists for.  A stalled request (delay) would not produce one.
    FakeOpenAIHandler.chunk_delay = 0.25
    try:

        async def scenario() -> tuple[dict, list[int]]:
            client = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
            cookie = cookie_header(client)
            ws1, _ready1, _state1 = await _connect(team_server.ws_url, cookie)
            await ws1.send(json.dumps({"id": "p", "type": "prompt", "message": "long one"}))
            # Long enough for the first delta (and its message_start) to be sent.
            await asyncio.sleep(0.35)

            ws2, ready2, _state2 = await _connect(team_server.ws_url, cookie, since=0)
            seqs: list[int] = []
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and len(seqs) < 5:
                    record = json.loads(await asyncio.wait_for(ws2.recv(), timeout=3))
                    if "seq" in record:
                        seqs.append(record["seq"])
            finally:
                await ws2.close()
                await ws1.close()
            return ready2, seqs

        ready, seqs = asyncio.run(scenario())
    finally:
        FakeOpenAIHandler.chunk_delay = 0.0

    assert ready["seq"] > ready["lastPersistedSeq"], "a run is in flight"
    assert seqs, "the reconnecting tab must receive the in-flight tail"
    # Two properties, and not a third: the tab is caught up to at least the moment
    # it connected, and it is never re-sent anything the snapshot already covered.
    # The exact first number is not asserted because a record can be persisted
    # between the prompt being accepted and the socket being registered.
    assert seqs[0] == ready["lastPersistedSeq"] + 1, (
        "replay starts exactly after what get_state already contains"
    )
    assert max(seqs) >= ready["lastPersistedSeq"] + 2, "more than the first record is replayed"
