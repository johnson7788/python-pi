"""The whole chain: model asks for a command, it runs inside a container.

Every other sandbox test stops one layer short.  The provisioner tests prove a container is
configured and behaves correctly; the backend tests prove the client speaks the protocol.
Neither proves that **the agent's own tool call** ends up in a container, which is the only
thing a user actually cares about — and it is exactly the seam where a wiring mistake hides:
forget to pass the backend and everything still "works", just on the host.

So this test runs the real stack: a provisioner subprocess, a team server configured with
`PI_SANDBOX_BACKEND=provisioner`, a fake model that asks for `bash`, and a WebSocket prompt.
It then checks the output for something that can only be true inside the container.

Needs a Docker daemon; skips without one.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets
from conftest import (
    TEAM_ADMIN_PASSWORD,
    TEAM_USER_PASSWORD,
    cookie_header,
    free_port,
    login,
    reset_fake_model,
    start_team_server,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVISIONER_DIR = REPO_ROOT / "deploy" / "sandbox_provisioner"
TOKEN = "e2e-provisioner-token"
IMAGE = os.environ.get("PI_TEST_SANDBOX_IMAGE", "python:3.12-slim")


def _docker_ready() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as error:  # noqa: BLE001
        return False, str(error)
    if result.returncode != 0:
        return False, (result.stderr or "").strip()[:200]
    return True, result.stdout.strip()


_ready, _why = _docker_ready()
pytestmark = pytest.mark.skipif(not _ready, reason=f"needs a docker daemon: {_why}")


@pytest.fixture()
def provisioner():
    """The provisioner service, as its own process, exactly as it is deployed."""
    port = free_port()
    env = {
        **os.environ,
        "SANDBOX_PROVISIONER_TOKEN": TOKEN,
        "SANDBOX_IMAGE": IMAGE,
        "SANDBOX_NETWORK": "none",
        "SANDBOX_IDLE_TIMEOUT_SECONDS": "120",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(PROVISIONER_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"provisioner exited early:\n{proc.stdout.read() if proc.stdout else ''}")
        try:
            if httpx.get(f"{base}/health", timeout=1.0).status_code == 200:
                break
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.1)
    else:
        proc.kill()
        raise AssertionError("the provisioner never became healthy")

    try:
        yield base
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


@pytest.fixture()
def sandboxed_server(fake_openai, tmp_path: Path, provisioner: str):
    """A team server whose sessions run commands in containers.

    Containers are removed on the way out, filtered by the label the provisioner stamps on
    them — never by a broader pattern, so a developer's own containers are untouched.
    """
    _, base_url = fake_openai
    server = start_team_server(
        tmp_path,
        base_url,
        extra_env={
            "PI_SANDBOX_BACKEND": "provisioner",
            "PI_SANDBOX_PROVISIONER_URL": provisioner,
            "PI_SANDBOX_PROVISIONER_TOKEN": TOKEN,
            "PI_SANDBOX_IMAGE": IMAGE,
            "PI_SANDBOX_NETWORK": "none",
            "PI_SANDBOX_RUN_AS": f"{os.getuid()}:{os.getgid()}",
        },
    )
    try:
        yield server, provisioner
    finally:
        server.stop()
        _remove_sandbox_containers()


def _remove_sandbox_containers() -> None:
    """`docker rm -f` every container the provisioner labelled, and nothing else."""
    listing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=app=pi-sandbox"],
        capture_output=True,
        text=True,
    )
    names = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    if names:
        subprocess.run(["docker", "rm", "-f", *names], capture_output=True, text=True)


def _prompt(server, cookie: str, message: str = "run it") -> str:
    """Drive one turn and return the whole record stream as text, for content assertions."""

    async def scenario() -> list[dict]:
        async with websockets.connect(
            server.ws_url, additional_headers={"Cookie": cookie}
        ) as ws:
            await ws.recv()  # ready
            await ws.recv()  # initial state
            await ws.send(json.dumps({"id": "p", "type": "prompt", "message": message}))
            seen: list[dict] = []
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                record = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                seen.append(record)
                if record.get("type") == "agent_end":
                    return seen
            raise AssertionError(f"no agent_end; saw {[r.get('type') for r in seen]}")

    return json.dumps(asyncio.run(scenario()))


def test_the_agents_bash_call_runs_inside_a_container(sandboxed_server):
    server, provisioner_url = sandboxed_server
    admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        created = admin.post(
            "/api/admin/users", json={"username": "ada", "password": TEAM_USER_PASSWORD}
        )
        assert created.status_code == 200, created.text
        member = login(server, "ada", TEAM_USER_PASSWORD)
        try:
            # Put a file in the user's workspace through the API, so the turn below reads
            # something the container can only see because the workspace is mounted.
            assert member.put(
                "/api/workspace/file",
                params={"path": "/hello.txt"},
                json={"content": "from the host"},
            ).status_code == 200

            # `hostname` is the discriminator that needs no absolute path: inside a container
            # it is the container's id, on the host it is this machine's name.
            reset_fake_model(tool="bash", bash_command="hostname; cat hello.txt")
            transcript = _prompt(server, cookie_header(member))

            assert "from the host" in transcript, "the container could not see the workspace"
            assert socket.gethostname() not in transcript, (
                "the command ran on the host: the output carries this machine's hostname"
            )
            assert "tool_execution_end" in transcript
        finally:
            member.close()

        # And the provisioner really did hand out a container for the conversation.
        listing = httpx.get(
            f"{provisioner_url}/sandboxes", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10
        ).json()
        assert listing["sandboxes"], "no sandbox was created"
        assert listing["sandboxes"][0]["sandboxId"]
    finally:
        admin.close()


def test_sandboxed_commands_still_obey_the_policy_gate(sandboxed_server):
    """Containment does not replace the gate: a refused command must not run at all.

    With a sandbox the consequences of `sudo` are smaller, but the answer to "may I run
    this" must not become "yes, it is contained now" — the policy is about intent, and the
    audit log has to keep recording that intent.
    """
    server, _provisioner_url = sandboxed_server
    admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        admin.post("/api/admin/users", json={"username": "bob", "password": TEAM_USER_PASSWORD})
        member = login(server, "bob", TEAM_USER_PASSWORD)
        try:
            reset_fake_model(tool="bash", bash_command="sudo cat hello.txt")
            transcript = _prompt(server, cookie_header(member))
            assert "refused by policy" in transcript
        finally:
            member.close()

        decisions = [
            row["decision"]
            for row in admin.get("/api/admin/audit").json()["records"]
            if row["tool_name"] == "bash"
        ]
        assert "blocked" in decisions
    finally:
        admin.close()


def _prompt_records(server, cookie: str, message: str) -> list[dict]:
    async def scenario() -> list[dict]:
        async with websockets.connect(
            server.ws_url, additional_headers={"Cookie": cookie}
        ) as ws:
            await ws.recv()
            await ws.recv()
            await ws.send(json.dumps({"id": "p", "type": "prompt", "message": message}))
            seen: list[dict] = []
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                record = json.loads(await asyncio.wait_for(ws.recv(), timeout=150))
                seen.append(record)
                if record.get("type") == "agent_end":
                    return seen
            raise AssertionError(f"no agent_end; saw {[r.get('type') for r in seen]}")

    return asyncio.run(scenario())


def test_bash_works_with_the_same_paths_the_agent_is_told(sandboxed_server):
    """The workspace is mounted at the server's own path, so one string means one directory.

    Mounting at a fixed ``/workspace`` made the model loop: ``read <cwd>`` succeeded while
    ``ls <cwd>`` failed, because the two tools disagreed about where the cwd was.
    """
    server, _provisioner_url = sandboxed_server
    admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        created = admin.post(
            "/api/admin/users", json={"username": "ada", "password": TEAM_USER_PASSWORD}
        )
        uid = created.json()["user"]["uid"]
        member = login(server, "ada", TEAM_USER_PASSWORD)
        try:
            member.put(
                "/api/workspace/file", params={"path": "/hello.txt"}, json={"content": "hi"}
            )
            reset_fake_model(tool="bash", bash_command="pwd && ls && cat hello.txt")
            transcript = _prompt(server, cookie_header(member))

            # `pwd` must print the path the read/write tools use, and `ls` must work there.
            assert str(server.data_root / "workspaces" / uid) in transcript
            assert "hi" in transcript
            assert "No such container" not in transcript
            assert "cannot access" not in transcript
        finally:
            member.close()
    finally:
        admin.close()


def test_a_sandbox_that_vanished_is_recreated_for_the_next_command(sandboxed_server):
    """Your bug, pinned: the idle reaper removes a container, and the next command 502s forever.

    Deleting the container out from under the session reproduces a routine eviction; the next
    command must transparently get a new one.
    """
    server, _provisioner_url = sandboxed_server
    admin = login(server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        created = admin.post(
            "/api/admin/users", json={"username": "bob", "password": TEAM_USER_PASSWORD}
        )
        assert created.status_code == 200, created.text
        member = login(server, "bob", TEAM_USER_PASSWORD)
        try:
            reset_fake_model(tool="bash", bash_command="echo first-run")
            assert "first-run" in _prompt(server, cookie_header(member))

            # Take the sandbox away, exactly as the reaper would.
            names = subprocess.run(
                ["docker", "ps", "-aq", "--filter", "label=app=pi-sandbox"],
                capture_output=True,
                text=True,
            ).stdout.split()
            assert names, "expected a sandbox container to exist"
            subprocess.run(["docker", "rm", "-f", *names], capture_output=True, text=True)

            reset_fake_model(tool="bash", bash_command="echo second-run")
            transcript = _prompt(server, cookie_header(member))

            assert "second-run" in transcript, "the command did not recover after eviction"
            assert "No such container" not in transcript
        finally:
            member.close()
    finally:
        admin.close()
