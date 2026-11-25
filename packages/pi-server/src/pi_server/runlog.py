"""A bounded, per-session log of everything a tab is sent, with sequence numbers.

Why it exists
-------------
The browser learns about a turn only from the records pushed down its WebSocket.
A tab that reloads mid-turn therefore loses the partial answer: the server keeps
sending into a socket nobody is reading, and ``get_state`` only knows messages the
session already *persisted* — the in-flight assistant message is not one of them.

So every outbound record gets a monotonic ``seq`` and is kept in a ring buffer.
On reconnect the server replays ``seq > floor``, where ``floor`` is the greater of
the client's own position and the last record that was persisted.  That second
term is what makes the replay duplicate-free: anything at or below it is already
in ``get_state``.

What it deliberately is not
---------------------------
*Not* durable.  A process restart loses the log; the session file plus
``get_state`` is the recovery path there.  Keeping a second copy of the stream on
disk would mean two write paths for one fact.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

#: Records kept per session.  A long tool-heavy turn produces a few thousand
#: deltas; past this the oldest are dropped, and a client that fell further behind
#: than the window simply gets ``get_state`` (see :meth:`RunLog.since`).
DEFAULT_CAPACITY = 4000


class RunLog:
    """Monotonic sequence assignment plus an offset-addressed tail.

    Single-threaded by contract: the server's event loop is the only writer, so
    the deque needs no lock.  ``append`` mutates the payload (adding ``seq``)
    before returning it, because the same dict is what the live socket sends.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._next = 1
        self._capacity = capacity

    @property
    def head(self) -> int:
        """Sequence number of the newest record (``0`` when empty)."""
        return self._next - 1

    @property
    def oldest(self) -> int:
        return self._records[0]["seq"] if self._records else self._next

    def append(self, payload: dict[str, Any]) -> int:
        """Assign the next sequence number to ``payload`` and keep a reference."""
        seq = self._next
        self._next += 1
        payload["seq"] = seq
        self._records.append(payload)
        return seq

    def since(self, floor: int) -> list[dict[str, Any]]:
        """Records with ``seq > floor``, oldest first."""
        return [record for record in self._records if record.get("seq", 0) > floor]

    def has_unpersisted_message(self, last_persisted: int) -> bool:
        """Whether a message started streaming that ``get_state`` does not contain.

        This is the only case replay exists for.  Without it a fresh tab would be
        re-sent the bookkeeping records of a turn that already finished
        (``turn_end``, ``agent_end``) for no benefit.
        """
        return any(
            record.get("seq", 0) > last_persisted and record.get("type") == "message_start"
            for record in self._records
        )

    def dropped(self, floor: int) -> bool:
        """Whether a client at ``floor`` fell behind the retained window."""
        return bool(self._records) and floor + 1 < self.oldest

    def reset(self) -> None:
        """Forget the tail and restart numbering (used when a worker is reused)."""
        self._records.clear()
        self._next = 1


def replay_floor(client_since: int, last_persisted: int) -> int:
    """Where replay starts: never re-send something ``get_state`` already covers."""
    return max(int(client_since or 0), int(last_persisted or 0))


def replay_records(log: RunLog, client_since: int, last_persisted: int) -> Iterable[dict[str, Any]]:
    """The records a reconnecting client is missing, oldest first."""
    return log.since(replay_floor(client_since, last_persisted))
