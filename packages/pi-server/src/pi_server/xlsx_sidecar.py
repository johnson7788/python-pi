"""Async driver for the Rust xlsx-sidecar (calamine + IronCalc).

The sidecar speaks NDJSON over stdin/stdout: one request line in, one response
line out, matched by requestId (mirrors genoffice's XlsxSidecarClient). Sessions
(open→sessionId) live in-process, so ALL requests share ONE long-lived process —
a session opened on one process can't be read from another.

ponytail: single process + a background reader resolving futures by requestId.
Concurrent requests pipeline fine (responses carry requestId). Restart on death;
scale to a process pool keyed by session only if one process can't keep up.
"""
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any


_PROTOCOL_VERSION = 1
_TIMEOUT_SEC = 180.0


class SidecarError(RuntimeError):
    pass


class _Sidecar:
    def __init__(self, binary: str) -> None:
        self._binary = binary
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        async with self._lock:
            loop = asyncio.get_running_loop()
            alive = self._proc is not None and self._proc.returncode is None
            # A live process whose reader is bound to a *different* (e.g. closed
            # test) loop can never resolve our futures — respawn on the new loop.
            same_loop = self._loop is loop and self._reader is not None and not self._reader.done()
            if alive and same_loop:
                return self._proc  # type: ignore[return-value]
            if alive:
                self._proc.kill()  # type: ignore[union-attr]
                self._pending.clear()
            self._proc = await asyncio.create_subprocess_exec(
                self._binary,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=256 * 1024 * 1024,  # NDJSON lines can be large (entry bytes)
            )
            self._loop = loop
            self._reader = asyncio.create_task(self._read_loop(self._proc))
            return self._proc

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fut = self._pending.pop(resp.get("requestId", ""), None)
                if fut is not None and not fut.done():
                    fut.set_result(resp)
        finally:
            err = SidecarError("xlsx sidecar exited")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def request(self, command: str, **fields: Any) -> Any:
        proc = await self._ensure()
        assert proc.stdin is not None
        request_id = uuid.uuid4().hex
        payload = json.dumps(
            {"version": _PROTOCOL_VERSION, "requestId": request_id, "command": command, **fields}
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = fut
        proc.stdin.write((payload + "\n").encode("utf-8"))
        await proc.stdin.drain()
        try:
            resp = await asyncio.wait_for(fut, timeout=_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise SidecarError(f"xlsx sidecar timed out on {command}")
        if not resp.get("ok"):
            msg = (resp.get("error") or {}).get("message", "xlsx sidecar request failed")
            raise SidecarError(msg)
        return resp.get("result")


def _default_binary() -> str:
    override = os.environ.get("PI_XLSX_SIDECAR")
    if override:
        return override
    repo = Path(__file__).resolve().parents[4] / "office" / "xlsx-engine" / "target" / "release" / "xlsx-sidecar"
    return str(repo)


_singleton = _Sidecar(_default_binary())


def sidecar() -> _Sidecar:
    return _singleton
