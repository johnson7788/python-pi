"""The ``write`` tool plus the per-file mutation queue used by mutating tools.

Two tools (``write`` and ``edit``) can target the same file from one parallel
tool batch.  Without serialisation the second operation would read content the
first one is about to replace, so every mutation acquires an ``asyncio.Lock``
keyed by the canonical path.  Locks for different files stay independent, so
unrelated files are still written in parallel.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal

from .path_utils import error_result, render_path, resolve_path, text_result

DESCRIPTION = (
    "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. "
    "Automatically creates parent directories."
)


class FileMutationQueue:
    """Serialise mutations that target the same file, keyed by real path.

    ``realpath`` is used so ``./a.txt`` and a symlink to it share one lock.  A
    path that does not exist yet falls back to its absolute form, which is the
    key its later mutations will resolve to as well.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    @staticmethod
    def key_for(path: str) -> str:
        """Return the queue key for ``path``."""
        absolute = os.path.abspath(path)
        try:
            return os.path.realpath(absolute)
        except OSError:
            return absolute

    @asynccontextmanager
    async def hold(self, path: str) -> AsyncIterator[None]:
        """Hold the mutation lock for ``path`` for the duration of the block."""
        key = self.key_for(path)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._refs[key] = self._refs.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            # No await happens between releasing the lock and dropping the
            # counters, so a waiter that already incremented the ref count
            # cannot race with this cleanup.
            remaining = self._refs.get(key, 1) - 1
            if remaining <= 0:
                self._refs.pop(key, None)
                self._locks.pop(key, None)
            else:
                self._refs[key] = remaining


_DEFAULT_MUTATION_QUEUE = FileMutationQueue()


def get_file_mutation_queue() -> FileMutationQueue:
    """Return the process-wide queue shared by independently created tools."""
    return _DEFAULT_MUTATION_QUEUE


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


async def _run_write(
    absolute_path: str,
    data: bytes,
    signal: AbortSignal | None,
) -> int:
    """Create parent directories and write ``data``; returns the byte count."""
    if signal is not None and signal.aborted:
        raise _WriteAborted

    parent = os.path.dirname(absolute_path)
    if parent:
        await asyncio.to_thread(os.makedirs, parent, exist_ok=True)
    if signal is not None and signal.aborted:
        raise _WriteAborted

    await asyncio.to_thread(_write_bytes, absolute_path, data)
    if signal is not None and signal.aborted:
        raise _WriteAborted
    return len(data)


class _WriteAborted(Exception):
    """Internal marker so the abort check can unwind out of the queue lock."""


def create_write_tool(cwd: str, mutation_queue: FileMutationQueue | None = None) -> AgentTool:
    """Build the ``write`` tool bound to ``cwd``."""
    queue = mutation_queue or get_file_mutation_queue()

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
            "content": {"type": "string", "description": "Content to write to the file"},
        },
        "required": ["path", "content"],
    }

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        if not isinstance(args, dict):
            return error_result("write requires an object argument with 'path' and 'content'.")
        path = args.get("path")
        content = args.get("content")
        if not isinstance(path, str) or not path:
            return error_result("write requires a non-empty 'path'.")
        if not isinstance(content, str):
            return error_result("write requires 'content' to be a string.")

        absolute_path = resolve_path(cwd, path)
        display_path = render_path(absolute_path, cwd)
        data = content.encode("utf-8")
        try:
            async with queue.hold(absolute_path):
                written = await _run_write(absolute_path, data, signal)
        except _WriteAborted:
            return error_result("Operation aborted")
        except OSError as exc:
            return error_result(f"Could not write file: {display_path}. {exc}.")

        return text_result(
            f"Successfully wrote to {path}",
            details={"path": display_path, "bytesWritten": written},
        )

    return AgentTool(
        name="write",
        label="write",
        description=DESCRIPTION,
        parameters=parameters,
        execute=execute,
        prompt_snippet="Create or overwrite files",
        prompt_guidelines=["Use write only for new files or complete rewrites."],
    )


__all__ = ["FileMutationQueue", "create_write_tool", "get_file_mutation_queue"]
