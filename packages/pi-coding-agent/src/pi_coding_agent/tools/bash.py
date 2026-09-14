"""The ``bash`` tool.

Unlike the other built-in tools, a failing command **raises** instead of
returning an error result.  This mirrors upstream: the agent loop turns a raised
exception into an error tool result, and the exception message carries the
captured output plus the exit code so the model still sees what went wrong.

stdout and stderr share one pipe (``stderr=STDOUT``) because the operating
system then preserves their relative order, which matters when a program
interleaves progress and errors.  The child is started in its own session so a
timeout or abort can kill the whole process group, including grandchildren.
"""

from __future__ import annotations

import asyncio
import codecs
import math
import os
import signal as signal_module
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal, TextContent

from .truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    format_size,
    split_lines_for_counting,
    truncate_tail_result,
    utf8_len,
)

#: Upstream caps the timeout at the largest 32-bit signed millisecond value.
MAX_TIMEOUT_MS = 2_147_483_647
MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000

#: Streaming progress is throttled so a chatty command cannot flood the UI.
UPDATE_THROTTLE_SECONDS = 0.1

#: Bytes read from the pipe per iteration.
READ_CHUNK_BYTES = 65536

DESCRIPTION = (
    "Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated "
    f"to last {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). If "
    "truncated, full output is saved to a temp file. Optionally provide a timeout in seconds."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command to execute"},
        "timeout": {"type": "number", "description": "Timeout in seconds (optional, no default timeout)"},
    },
    "required": ["command"],
}


@dataclass
class CommandResult:
    """Everything a finished (or killed) command produced."""

    text: str
    raw: bytes
    exit_code: int | None


class CommandAborted(Exception):
    """Raised internally when the abort signal killed the process."""

    def __init__(self, result: CommandResult) -> None:
        super().__init__("aborted")
        self.result = result


class CommandTimedOut(Exception):
    """Raised internally when the timeout killed the process."""

    def __init__(self, result: CommandResult) -> None:
        super().__init__("timeout")
        self.result = result


def resolve_timeout_seconds(timeout: Any) -> float | None:
    """Validate the ``timeout`` argument; ``None`` means "no timeout"."""
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise RuntimeError("Invalid timeout: must be a finite number of seconds")
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("Invalid timeout: must be a finite number of seconds")
    if timeout * 1000 > MAX_TIMEOUT_MS:
        raise RuntimeError(f"Invalid timeout: maximum is {MAX_TIMEOUT_SECONDS} seconds")
    return float(timeout)


def format_seconds(seconds: float) -> str:
    """Render a timeout the way the user supplied it (``5`` rather than ``5.0``)."""
    return str(int(seconds)) if float(seconds).is_integer() else str(seconds)


#: Environment variable name segments that mark a value as a credential.  Matched per
#: underscore-separated segment rather than as a substring, so ``GIT_AUTHOR_NAME`` (which
#: contains "AUTH") stays usable while ``PI_API_KEY``, ``AWS_SECRET_ACCESS_KEY`` and
#: ``GITHUB_TOKEN`` do not.
_SECRET_SEGMENTS = frozenset(
    {
        "KEY",
        "KEYS",
        "TOKEN",
        "TOKENS",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "CREDENTIALS",
        "AUTH",
        "SESSION",
    }
)


def _looks_like_a_credential(name: str) -> bool:
    return any(segment in _SECRET_SEGMENTS for segment in name.upper().split("_"))


def build_shell_env(
    cwd: str, metadata: dict[str, str] | None = None, *, scrub_secrets: bool = False
) -> dict[str, str]:
    """Build the child environment, exposing the session through ``PI_*``.

    Session-scoped ``PI_*`` variables inherited from the parent process are
    cleared first so a nested pi session cannot leak stale identity into the
    command it runs.

    ``scrub_secrets`` exists for a shared deployment: the server process holds the
    company model credential, so without this any user could read it out simply by asking
    the agent to run ``env``.  It is off by default because a personal install
    legitimately expects its own tokens (``GITHUB_TOKEN``, cloud CLI credentials) to be
    visible to the commands it runs.
    """
    if scrub_secrets:
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("PI_") and not _looks_like_a_credential(name)
        }
    else:
        env = dict(os.environ)
        for key in ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL"):
            env.pop(key, None)
    env["PI_CWD"] = cwd
    if metadata:
        for key, value in metadata.items():
            env[str(key)] = str(value)
    return env


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the child's process group, falling back to the process itself."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal_module.SIGKILL)
        else:  # pragma: no cover - Windows has no process groups here
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _append_status(text: str, status: str) -> str:
    """Append a status line, keeping the message clean when there is no output."""
    return f"{text}\n\n{status}" if text else status


def _shell_executable() -> str | None:
    return "/bin/bash" if os.path.exists("/bin/bash") else None


async def run_command(
    command: str,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: float | None,
    signal: AbortSignal | None,
    on_chunk: Callable[[bytes], None] | None = None,
) -> CommandResult:
    """Run ``command`` to completion.

    Raises ``CommandTimedOut`` / ``CommandAborted`` carrying the output collected
    so far, so the caller can still report partial results.
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        executable=_shell_executable(),
        start_new_session=(os.name == "posix"),
    )

    loop = asyncio.get_running_loop()
    state = {"timed_out": False, "aborted": False}

    def _kill() -> None:
        _kill_process_tree(proc)

    timeout_handle = None
    if timeout_seconds is not None:

        def _on_timeout() -> None:
            state["timed_out"] = True
            _kill()

        timeout_handle = loop.call_later(timeout_seconds, _on_timeout)

    dispose_abort = None
    if signal is not None:

        def _on_abort() -> None:
            state["aborted"] = True
            _kill()

        dispose_abort = signal.on_abort(_on_abort)

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text_chunks: list[str] = []
    raw = bytearray()

    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            raw.extend(chunk)
            text_chunks.append(decoder.decode(chunk))
            if on_chunk is not None:
                on_chunk(chunk)
        text_chunks.append(decoder.decode(b"", True))
        exit_code = await proc.wait()
    finally:
        if timeout_handle is not None:
            timeout_handle.cancel()
        if dispose_abort is not None:
            dispose_abort()

    result = CommandResult(text="".join(text_chunks), raw=bytes(raw), exit_code=exit_code)
    if state["aborted"]:
        raise CommandAborted(result)
    if state["timed_out"]:
        raise CommandTimedOut(result)
    return result


def write_full_output(raw: bytes) -> str:
    """Persist the full command output and return the temp file path."""
    fd, path = tempfile.mkstemp(prefix="pi-bash-", suffix=".log", dir=tempfile.gettempdir())
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    return path


def create_bash_tool(
    cwd: str,
    metadata: dict[str, str] | None = None,
    *,
    scrub_secrets: bool = False,
    backend: Any = None,
) -> AgentTool:
    """Build the ``bash`` tool bound to ``cwd``.

    ``metadata`` is merged into the child environment, which is how the session layer exposes
    ``PI_SESSION_ID`` / ``PI_MODEL`` and friends.

    ``backend`` is *where the command runs* — this process by default, or a sandbox container
    when the deployment supplies one (see ``pi_coding_agent.sandbox``).  There is deliberately
    only one execution path: a second inline branch would drift from the sandboxed one, and
    the sandboxed path is the one nobody notices is broken.
    """
    if backend is None:
        from ..sandbox import LocalBackend

        backend = LocalBackend(scrub_secrets=scrub_secrets)

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        if not isinstance(args, dict):
            raise RuntimeError("bash requires an object argument with 'command'.")
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise RuntimeError("bash requires a non-empty 'command'.")

        timeout_seconds = resolve_timeout_seconds(args.get("timeout"))

        working_dir = cwd
        if not os.path.isdir(working_dir):
            raise RuntimeError(
                f"Working directory does not exist: {working_dir}\nCannot execute bash commands."
            )

        if on_update is not None:
            on_update(AgentToolResult(content=[], details=None))

        loop = asyncio.get_running_loop()
        update_state = {"last": 0.0, "tail": ""}

        def on_chunk(chunk: bytes) -> None:
            if on_update is None:
                return
            decoded = chunk.decode("utf-8", errors="replace")
            update_state["tail"] = (update_state["tail"] + decoded)[-READ_CHUNK_BYTES:]
            now = loop.time()
            if now - update_state["last"] < UPDATE_THROTTLE_SECONDS:
                return
            update_state["last"] = now
            snapshot = truncate_tail_result(update_state["tail"])
            on_update(AgentToolResult(content=[TextContent(text=snapshot.content)], details=None))

        def format_output(result: CommandResult, empty_text: str = "(no output)") -> tuple[str, dict[str, Any] | None]:
            truncation = truncate_tail_result(result.text)
            text = truncation.content or empty_text
            if not truncation.truncated:
                return text, None

            path = write_full_output(result.raw)
            lines = split_lines_for_counting(result.text)
            last_line_bytes = utf8_len(lines[-1]) if lines else 0
            start_line = truncation.total_lines - truncation.output_lines + 1
            end_line = truncation.total_lines

            if truncation.last_line_partial:
                text += (
                    f"\n\n[Showing last {format_size(truncation.output_bytes)} of line {end_line} "
                    f"(line is {format_size(last_line_bytes)}). Full output: {path}]"
                )
            elif truncation.truncated_by == "lines":
                text += (
                    f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines}. "
                    f"Full output: {path}]"
                )
            else:
                text += (
                    f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines} "
                    f"({format_size(DEFAULT_MAX_BYTES)} limit). Full output: {path}]"
                )
            return text, {"truncation": truncation.to_dict(), "fullOutputPath": path}

        outcome = await backend.exec(
            command,
            cwd=working_dir,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
            signal=signal,
            on_chunk=on_chunk,
        )
        # The backend reports rather than raises (a remote one cannot raise our exception
        # types), so the two failures the tool distinguishes are re-established here.
        result = CommandResult(text=outcome.output, raw=outcome.raw, exit_code=outcome.exit_code)
        if outcome.aborted:
            text, _details = format_output(result, "")
            raise RuntimeError(_append_status(text, "Command aborted"))
        if outcome.timed_out:
            text, _details = format_output(result, "")
            seconds = format_seconds(timeout_seconds) if timeout_seconds is not None else "0"
            raise RuntimeError(_append_status(text, f"Command timed out after {seconds} seconds"))

        text, details = format_output(result)
        if result.exit_code not in (0, None):
            raise RuntimeError(_append_status(text, f"Command exited with code {result.exit_code}"))
        return AgentToolResult(content=[TextContent(text=text)], details=details)

    return AgentTool(
        name="bash",
        label="bash",
        description=DESCRIPTION,
        parameters=_SCHEMA,
        execute=execute,
        prompt_snippet="Execute bash commands (ls, grep, find, etc.)",
        prompt_guidelines=["You can inspect PI_* environment variables for current model and session details."],
    )


__all__ = [
    "CommandAborted",
    "CommandResult",
    "CommandTimedOut",
    "build_shell_env",
    "create_bash_tool",
    "format_seconds",
    "resolve_timeout_seconds",
    "run_command",
    "write_full_output",
]
