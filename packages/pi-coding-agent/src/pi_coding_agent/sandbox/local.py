"""Runs commands in this process — the behaviour a personal install has always had.

It stays the default, and it stays first-class: a sandbox that is only reachable with
Docker would mean the common case is the untested one.
"""

from __future__ import annotations

import os
from typing import Callable

from .types import ExecResult


class LocalBackend:
    """The process itself, behind the sandbox interface."""

    name = "local"

    def __init__(self, *, scrub_secrets: bool = False) -> None:
        self.scrub_secrets = scrub_secrets

    async def exec(
        self,
        command: str,
        *,
        cwd: str,
        metadata: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        signal: object | None = None,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> ExecResult:
        # Imported lazily: `tools.bash` constructs this class, so a module-level import back
        # would be a cycle.
        from ..tools.bash import CommandAborted, CommandTimedOut, build_shell_env, run_command

        env = build_shell_env(cwd, metadata, scrub_secrets=self.scrub_secrets)
        try:
            result = await run_command(command, cwd, env, timeout_seconds, signal, on_chunk)
        except CommandTimedOut as timeout:
            return ExecResult(
                output=timeout.result.text, raw=timeout.result.raw, exit_code=None, timed_out=True
            )
        except CommandAborted as abort:
            return ExecResult(
                output=abort.result.text, raw=abort.result.raw, exit_code=None, aborted=True
            )
        return ExecResult(output=result.text, raw=result.raw, exit_code=result.exit_code)

    def read_text(self, path: str, *, limit: int | None = None) -> str:
        with open(path, "rb") as handle:
            raw = handle.read(limit) if limit is not None else handle.read()
        return raw.decode("utf-8", errors="replace")

    def write_bytes(self, path: str, data: bytes) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def list_dir(self, path: str) -> list[str]:
        return sorted(os.listdir(path))

    def exists(self, path: str) -> bool:
        return os.path.exists(path)


__all__ = ["LocalBackend"]
