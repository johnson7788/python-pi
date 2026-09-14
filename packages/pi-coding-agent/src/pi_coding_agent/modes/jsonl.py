"""Strict LF-only JSONL framing.

Port of ``packages/coding-agent/src/modes/rpc/jsonl.ts``.

The rule that matters: **split on ``\\n`` only**.  Python's own line iteration
(and Node's ``readline``) also treat U+2028/U+2029 as line breaks, but those are
perfectly valid characters *inside* a JSON string, so a naive reader would tear a
record in half.  Records are read as raw bytes and decoded explicitly, which also
sidesteps universal-newline translation mangling ``\\r`` inside payloads.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any, AsyncIterator, BinaryIO

#: Read granularity; large enough to amortise syscalls, small enough to stay live.
CHUNK_SIZE = 65536


def serialize_json_line(value: Any) -> str:
    """Serialise one record as a single LF-terminated line."""
    return json.dumps(value, ensure_ascii=False, default=str) + "\n"


def write_json_line(stream: Any, value: Any) -> None:
    stream.write(serialize_json_line(value))
    stream.flush()


def strip_record_separator(line: str) -> str:
    """Drop a trailing ``\\r`` so CRLF input behaves like LF input."""
    return line[:-1] if line.endswith("\r") else line


def decode_record(raw: bytes) -> str:
    """Decode one record; invalid UTF-8 becomes replacement characters."""
    return strip_record_separator(raw.decode("utf-8", errors="replace"))


def split_records(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split a byte buffer into complete records plus the unconsumed remainder."""
    if b"\n" not in buffer:
        return [], buffer
    parts = buffer.split(b"\n")
    return parts[:-1], parts[-1]


def read_chunk(stream: BinaryIO) -> bytes:
    """Read whatever is currently available without waiting to fill a buffer.

    ``BufferedReader.read(n)`` blocks until ``n`` bytes or EOF, which would stall
    an interactive stdin pipe indefinitely; ``read1`` returns after at most one
    underlying read.
    """
    read1 = getattr(stream, "read1", None)
    if callable(read1):
        return read1(CHUNK_SIZE)
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is not None:
        try:
            return os.read(fd, CHUNK_SIZE)
        except OSError:
            return b""
    data = stream.read(CHUNK_SIZE)
    return data if isinstance(data, bytes) else str(data).encode("utf-8")


class JsonlLineReader:
    """Async iterator over LF-delimited records read from a binary stream.

    Reading happens on a daemon thread because stdin is a blocking file object;
    records are handed to the event loop with ``call_soon_threadsafe`` so the
    consumer never blocks a thread.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _emit(value: str | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, value)

        def _pump() -> None:
            buffer = b""
            while True:
                chunk = read_chunk(self._stream)
                if not chunk:
                    # EOF: flush whatever is left, even without a trailing newline.
                    if buffer:
                        _emit(decode_record(buffer))
                    _emit(None)
                    return
                buffer += chunk
                records, buffer = split_records(buffer)
                for record in records:
                    _emit(decode_record(record))

        threading.Thread(target=_pump, name="pi-py-jsonl-reader", daemon=True).start()

        while True:
            line = await queue.get()
            if line is None:
                return
            yield line


__all__ = [
    "CHUNK_SIZE",
    "JsonlLineReader",
    "decode_record",
    "read_chunk",
    "serialize_json_line",
    "split_records",
    "strip_record_separator",
    "write_json_line",
]
