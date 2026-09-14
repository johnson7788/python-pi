"""The execution seam: local process, or a container per conversation.

The interesting assertions are not "the protocol has five methods".  They are:

* the **bash tool really runs through the backend** (a stub backend proves it, with no
  Docker anywhere near the test);
* a remote backend **refuses a working directory outside the workspace** instead of
  quietly remapping it — the container only has one directory mounted, so a path outside it
  has no meaning and guessing would be worse than failing;
* a **misconfigured sandbox fails at startup**, because an operator who asked for
  containment must not get silence.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pi_coding_agent.sandbox import (
    CONTAINER_WORKSPACE,
    ExecResult,
    LocalBackend,
    ProvisionerBackend,
    ProvisionerError,
    SandboxBackend,
    SandboxScope,
    assert_backend_usable,
    host_path_for,
    select_backend,
)
from pi_coding_agent.tools import create_bash_tool

# --------------------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------------------


def _scope(workspace: str, *, host: str | None = None, uid: str = "u1", thread: str = "s1"):
    return SandboxScope(uid=uid, thread=thread, workspace=workspace, host_workspace=host)


def test_both_backends_satisfy_the_protocol(tmp_path: Path):
    assert isinstance(LocalBackend(), SandboxBackend)
    assert isinstance(ProvisionerBackend(_scope(str(tmp_path)), url="http://x", token="t"), SandboxBackend)


def test_a_sandbox_id_is_stable_per_conversation_and_unique_per_user(tmp_path: Path):
    first = _scope(str(tmp_path), uid="ada", thread="s1")
    again = _scope(str(tmp_path), uid="ada", thread="s1")
    other_thread = _scope(str(tmp_path), uid="ada", thread="s2")
    other_user = _scope(str(tmp_path), uid="bob", thread="s1")

    assert first.sandbox_id() == again.sandbox_id()
    assert len({first.sandbox_id(), other_thread.sandbox_id(), other_user.sandbox_id()}) == 3
    # Safe to use as a container name.
    assert first.sandbox_id().isalnum()


def test_host_path_translation(tmp_path: Path):
    app_root = tmp_path / "data"
    host_root = tmp_path / "host-data"
    (app_root / "workspaces" / "u1").mkdir(parents=True)

    assert host_path_for(
        str(app_root / "workspaces" / "u1"), app_root=str(app_root), host_root=str(host_root)
    ) == str(host_root / "workspaces" / "u1")
    # No mapping configured means "this process is already on the host".
    assert host_path_for(str(app_root), app_root=None, host_root=None) is None
    # A path outside the data root has no translation, which the caller must not guess at.
    assert host_path_for("/etc", app_root=str(app_root), host_root=str(host_root)) is None


# --------------------------------------------------------------------------------------
# The local backend
# --------------------------------------------------------------------------------------


async def test_local_backend_executes_and_reports(tmp_path: Path):
    ok = await LocalBackend().exec("printf 'hello'", cwd=str(tmp_path))
    assert ok.output == "hello" and ok.exit_code == 0

    failed = await LocalBackend().exec("exit 3", cwd=str(tmp_path))
    assert failed.exit_code == 3


async def test_local_backend_reports_timeout_and_abort_rather_than_raising(tmp_path: Path):
    """A remote backend cannot raise our exception types, so the result carries the outcome."""
    timed_out = await LocalBackend().exec("sleep 5", cwd=str(tmp_path), timeout_seconds=0.2)
    assert timed_out.timed_out is True and timed_out.exit_code is None


def test_local_backend_file_operations(tmp_path: Path):
    backend = LocalBackend()
    target = tmp_path / "nested" / "note.txt"

    backend.write_bytes(str(target), "hi".encode())
    assert backend.exists(str(target))
    assert backend.read_text(str(target)) == "hi"
    assert backend.read_text(str(target), limit=1) == "h"
    assert backend.list_dir(str(tmp_path)) == ["nested"]


async def test_local_backend_honours_the_scrub_setting(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PI_API_KEY", "shared-company-key")
    result = await LocalBackend(scrub_secrets=True).exec("printenv PI_API_KEY || echo ABSENT", cwd=str(tmp_path))
    assert "shared-company-key" not in result.output
    assert "ABSENT" in result.output


# --------------------------------------------------------------------------------------
# The bash tool really goes through the backend
# --------------------------------------------------------------------------------------


class RecordingBackend:
    """A backend that records what it was asked and answers with whatever it is told to."""

    name = "recording"

    def __init__(self, result: ExecResult | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or ExecResult(output="from the backend", exit_code=0)

    async def exec(self, command, *, cwd, metadata=None, timeout_seconds=None, signal=None, on_chunk=None):
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "metadata": metadata,
                "timeout_seconds": timeout_seconds,
                "signal": signal,
            }
        )
        return self.result

    read_text = staticmethod(lambda path, limit=None: "")
    write_bytes = staticmethod(lambda path, data: None)
    list_dir = staticmethod(lambda path: [])
    exists = staticmethod(lambda path: False)


def _text_of(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


async def test_the_bash_tool_runs_through_its_backend(tmp_path: Path):
    """The point of the seam: the tool must not quietly keep using a real shell."""
    backend = RecordingBackend()
    tool = create_bash_tool(str(tmp_path), backend=backend)

    result = await tool.execute("call-1", {"command": "echo hi"})

    assert backend.calls, "the tool never consulted its backend"
    assert backend.calls[0]["command"] == "echo hi"
    assert backend.calls[0]["cwd"] == str(tmp_path)
    assert _text_of(result) == "from the backend"


async def test_backend_failures_reach_the_model_as_errors(tmp_path: Path):
    backend = RecordingBackend(ExecResult(output="boom", exit_code=7))
    tool = create_bash_tool(str(tmp_path), backend=backend)

    with pytest.raises(RuntimeError) as info:
        await tool.execute("call-1", {"command": "exit 7"})

    assert "Command exited with code 7" in str(info.value)
    assert "boom" in str(info.value)


async def test_a_timeout_from_the_backend_reads_like_a_local_timeout(tmp_path: Path):
    """The tool's contract with the model must not depend on which backend answered."""
    backend = RecordingBackend(ExecResult(output="partial", timed_out=True))
    tool = create_bash_tool(str(tmp_path), backend=backend)

    with pytest.raises(RuntimeError) as info:
        await tool.execute("call-1", {"command": "sleep 5", "timeout": 5})

    assert "timed out after 5 seconds" in str(info.value)
    assert "partial" in str(info.value)


async def test_the_abort_signal_reaches_the_backend(tmp_path: Path):
    """Cancelling a turn must be able to stop the command, wherever it runs."""
    backend = RecordingBackend()

    class Signal:
        aborted = False

        def on_abort(self, callback):
            return lambda: None

    signal = Signal()
    tool = create_bash_tool(str(tmp_path), backend=backend)
    await tool.execute("call-1", {"command": "sleep 1"}, signal=signal)

    assert backend.calls[0]["signal"] is signal


# --------------------------------------------------------------------------------------
# The provisioner client
# --------------------------------------------------------------------------------------


def _provisioner(handler, workspace: Path, **kwargs) -> tuple[ProvisionerBackend, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped), base_url="http://provisioner")
    backend = ProvisionerBackend(
        _scope(str(workspace), host="/host" + str(workspace)),
        url="http://provisioner",
        token="secret-token",
        client=client,
        **kwargs,
    )
    return backend, seen


async def test_exec_creates_the_container_once_and_runs_the_command(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.startswith("/sandboxes/") and not request.url.path.endswith("/exec"):
            return httpx.Response(200, json={"sandboxId": "x", "status": "running"})
        assert request.url.path.endswith("/exec")
        return httpx.Response(200, json={"output": "42\n", "exitCode": 0, "timedOut": False})

    backend, seen = _provisioner(handler, tmp_path)

    first = await backend.exec("echo 42", cwd=str(tmp_path))
    second = await backend.exec("echo 43", cwd=str(tmp_path))

    assert first.output == "42\n" and first.exit_code == 0
    assert second.exit_code == 0
    # One create, two execs: the container is per conversation, not per command.
    creates = [r for r in seen if not r.url.path.endswith("/exec")]
    assert len(creates) == 1
    assert len(seen) == 3

    create_body = json.loads(creates[0].content)
    # The daemon resolves the bind mount on the host, so it must receive the host path.
    assert create_body["workspace"] == "/host" + str(tmp_path)
    assert creates[0].headers["authorization"] == "Bearer secret-token"


async def test_exec_translates_the_working_directory_into_the_container(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, json={"output": "", "exitCode": 0})
        return httpx.Response(200, json={"sandboxId": "x"})

    backend, seen = _provisioner(handler, tmp_path)
    (tmp_path / "src").mkdir()

    await backend.exec("ls", cwd=str(tmp_path / "src"))

    body = json.loads([r for r in seen if r.url.path.endswith("/exec")][0].content)
    assert body["cwd"] == f"{CONTAINER_WORKSPACE}/src"


async def test_exec_refuses_a_directory_outside_the_workspace(tmp_path: Path):
    """The container has one directory mounted; a path outside it has no meaning."""
    backend, seen = _provisioner(lambda request: httpx.Response(200, json={}), tmp_path)

    with pytest.raises(ProvisionerError) as info:
        await backend.exec("ls", cwd="/etc")

    assert "outside the workspace" in str(info.value)
    assert not [r for r in seen if r.url.path.endswith("/exec")], "nothing should have been sent"


async def test_only_session_variables_travel_to_the_container(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, json={"output": "", "exitCode": 0})
        return httpx.Response(200, json={"sandboxId": "x"})

    backend, seen = _provisioner(handler, tmp_path)
    await backend.exec("env", cwd=str(tmp_path), metadata={"PI_SESSION_ID": "s-1"})

    body = json.loads([r for r in seen if r.url.path.endswith("/exec")][0].content)
    assert body["env"] == {"PI_SESSION_ID": "s-1"}


async def test_a_rejected_token_is_reported_as_such(tmp_path: Path):
    backend, _ = _provisioner(lambda request: httpx.Response(401, json={"error": "nope"}), tmp_path)

    with pytest.raises(ProvisionerError) as info:
        await backend.exec("ls", cwd=str(tmp_path))

    assert "token" in str(info.value)


async def test_timeout_and_exit_code_survive_the_round_trip(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, json={"output": "partial", "exitCode": None, "timedOut": True})
        return httpx.Response(200, json={"sandboxId": "x"})

    backend, _ = _provisioner(handler, tmp_path)
    result = await backend.exec("sleep 99", cwd=str(tmp_path), timeout_seconds=1.5)

    assert result.timed_out is True
    assert result.output == "partial"


async def test_release_drops_the_container_and_swallows_a_missing_one(tmp_path: Path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "DELETE":
            return httpx.Response(404, json={"error": "no such sandbox"})
        return httpx.Response(200, json={"sandboxId": "x"})

    backend, _ = _provisioner(handler, tmp_path)
    await backend.exec("ls", cwd=str(tmp_path))
    await backend.release()  # a container that is already gone must not raise

    assert any(call.startswith("DELETE") for call in calls)


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_selecting_an_unknown_backend_is_an_error_not_a_fallback():
    with pytest.raises(ValueError):
        select_backend("quantum")


def test_the_provisioner_needs_a_scope(tmp_path: Path):
    """A container belongs to a conversation; a process-wide one would be shared by everyone."""
    with pytest.raises(ValueError) as info:
        select_backend("provisioner", url="http://x", token="t")
    assert "SandboxScope" in str(info.value)


def test_an_incomplete_provisioner_config_fails_at_startup(tmp_path: Path):
    scope = _scope(str(tmp_path))
    with pytest.raises(ProvisionerError) as info:
        assert_backend_usable(select_backend("provisioner", scope=scope, url="http://x"))
    assert "PI_SANDBOX_PROVISIONER_TOKEN" in str(info.value)

    # And a complete one passes.
    assert_backend_usable(
        select_backend("provisioner", scope=scope, url="http://x", token="t")
    )


def test_local_is_the_default_and_needs_no_docker(monkeypatch):
    monkeypatch.delenv("PI_SANDBOX_BACKEND", raising=False)
    backend = select_backend()
    assert backend.name == "local"
    assert_backend_usable(backend)
