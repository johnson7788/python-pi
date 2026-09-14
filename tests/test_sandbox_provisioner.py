"""The sandbox provisioner: the container configuration, and a real container.

Two layers, and the split is the point.

**Configuration is asserted with a fake docker client.**  The isolation *is* the container
configuration — one bind mount, dropped capabilities, no docker socket, resource caps.  Those
kwargs are what a reviewer needs to see, they are cheap to check, and they run everywhere.

**One test creates a real container.**  Configuration that is never exercised is a claim, not
a fact: the test below starts a container, runs commands in it, and checks that the workspace
is shared while the rest of the host is not.  It needs a Docker daemon and skips without one.

What is deliberately *not* here: a test that the sandbox is unbreakable.  It is a container
with a shared directory — the shared directory is the boundary, and that is what these tests
pin down.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVISIONER_APP = REPO_ROOT / "deploy" / "sandbox_provisioner" / "app.py"

TOKEN = "test-provisioner-token"

#: A small image that already exists on most machines, and is pulled on demand otherwise.
TEST_IMAGE = os.environ.get("PI_TEST_SANDBOX_IMAGE", "python:3.12-slim")


# --------------------------------------------------------------------------------------
# Loading the provisioner
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def provisioner() -> Any:
    """Import the provisioner as a module.

    The service is deliberately not a package — it is one file in a container with three
    dependencies — so it is loaded by path, the same way the extension loader loads a plugin.
    Environment is set before import because the module reads its configuration at import
    time, which is correct for a service that is configured by its deployment.
    """
    os.environ.setdefault("SANDBOX_PROVISIONER_TOKEN", TOKEN)
    os.environ.setdefault("SANDBOX_IMAGE", TEST_IMAGE)
    spec = importlib.util.spec_from_file_location("pi_sandbox_provisioner", PROVISIONER_APP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered *before* exec: `@dataclass` resolves `__module__` through sys.modules while
    # the module body is still running, and fails with a confusing AttributeError otherwise.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def client(provisioner: Any) -> TestClient:
    return TestClient(provisioner.app)


def _auth(provisioner: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {provisioner._TOKEN}"}


# --------------------------------------------------------------------------------------
# A fake daemon, to inspect what the provisioner asks for
# --------------------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, **kwargs: Any) -> None:
        self.name = kwargs["name"]
        self.id = f"id-{kwargs['name']}"
        self.status = "running"
        self.kwargs = kwargs
        self.removed = False
        self.execs: list[list[str]] = []

    def reload(self) -> None:
        return None

    def start(self) -> None:
        self.status = "running"

    def exec_run(self, cmd, workdir=None, environment=None, demux=None):  # noqa: ANN001
        self.execs.append(list(cmd))
        if cmd[:2] == ["sh", "-lc"] and "command -v timeout" in cmd[2]:
            return 0, b"/usr/bin/timeout\n"
        return 0, b"ok\n"

    def remove(self, force: bool = False) -> None:  # noqa: ARG002
        self.removed = True


class FakeContainers:
    def __init__(self) -> None:
        self.created: dict[str, FakeContainer] = {}

    def run(self, **kwargs: Any) -> FakeContainer:
        container = FakeContainer(**kwargs)
        self.created[kwargs["name"]] = container
        return container

    def get(self, name: str) -> FakeContainer:
        if name not in self.created:
            from docker.errors import NotFound

            raise NotFound(name)
        return self.created[name]


class FakeDocker:
    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.pinged = False

    def ping(self) -> bool:
        self.pinged = True
        return True


@pytest.fixture()
def fake_daemon(provisioner: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """A provisioner whose docker client is a recorder."""
    daemon = FakeDocker()
    registry = provisioner.Registry()
    monkeypatch.setattr(registry, "client", lambda: daemon)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return registry, daemon, workspace


# --------------------------------------------------------------------------------------
# The container configuration — this is the isolation
# --------------------------------------------------------------------------------------


def test_the_container_gets_exactly_one_bind_mount(fake_daemon: Any):
    registry, daemon, workspace = fake_daemon

    registry.ensure("sb1", "uid-1", "session", str(workspace))
    container = daemon.containers.created["pi-sandbox-sb1"]

    # One mount, at a fixed path, read-write. Everything the container can see of the host
    # is this directory; the data root, other users' workspaces and the transcripts are not
    # reachable, which is the difference between this and the policy extension asking nicely.
    assert container.kwargs["volumes"] == {
        str(workspace): {"bind": "/workspace", "mode": "rw"}
    }


def test_the_container_cannot_be_privileged(fake_daemon: Any):
    registry, daemon, workspace = fake_daemon
    registry.ensure("sb1", "uid-1", "session", str(workspace))
    kwargs = daemon.containers.created["pi-sandbox-sb1"].kwargs

    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in kwargs["security_opt"]
    assert "/var/run/docker.sock" not in json.dumps(kwargs["volumes"])
    assert kwargs["pids_limit"] > 0
    assert kwargs["mem_limit"]
    assert kwargs["nano_cpus"] > 0


def test_the_container_runs_as_the_server_not_root(fake_daemon: Any):
    """Files written in the sandbox must stay owned by the server that manages the workspace."""
    registry, daemon, workspace = fake_daemon
    registry.ensure("sb1", "uid-1", "session", str(workspace), runAs="1000:1000")

    assert daemon.containers.created["pi-sandbox-sb1"].kwargs["user"] == "1000:1000"


def test_the_container_environment_is_built_here_not_inherited(fake_daemon: Any, monkeypatch: pytest.MonkeyPatch):
    """A host credential must not be able to arrive through a container's environment."""
    monkeypatch.setenv("PI_API_KEY", "shared-company-key")
    registry, daemon, workspace = fake_daemon
    registry.ensure("sb1", "uid-1", "session", str(workspace))

    env = daemon.containers.created["pi-sandbox-sb1"].kwargs["environment"]
    assert "PI_API_KEY" not in env
    assert "shared-company-key" not in json.dumps(env)
    assert env["HOME"] == "/workspace"


def test_an_image_without_timeout_is_refused(provisioner: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Losing the per-command bound silently is worse than refusing to start a sandbox."""

    class NoTimeoutContainer(FakeContainer):
        def exec_run(self, cmd, workdir=None, environment=None, demux=None):  # noqa: ANN001
            return 1, b""

    class Daemon(FakeDocker):
        pass

    daemon = Daemon()
    daemon.containers.run = lambda **kwargs: NoTimeoutContainer(**kwargs)
    registry = provisioner.Registry()
    monkeypatch.setattr(registry, "client", lambda: daemon)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(provisioner.ProvisionerError) as info:
        registry.ensure("sb1", "uid-1", "session", str(workspace))
    assert "timeout" in str(info.value)


def test_a_missing_workspace_is_refused(fake_daemon: Any, provisioner: Any, tmp_path: Path):
    registry, _daemon, _workspace = fake_daemon
    with pytest.raises(provisioner.ProvisionerError) as info:
        registry.ensure("sb1", "uid-1", "session", str(tmp_path / "nope"))
    assert "does not exist" in str(info.value)


def test_a_directory_outside_the_workspace_is_refused(fake_daemon: Any, provisioner: Any):
    registry, _daemon, workspace = fake_daemon
    registry.ensure("sb1", "uid-1", "session", str(workspace))

    with pytest.raises(provisioner.ProvisionerError) as info:
        registry.exec("sb1", "ls", cwd="/etc", timeout_seconds=1, env=None)
    assert "/workspace" in str(info.value)


def test_idle_sandboxes_are_reaped(fake_daemon: Any, monkeypatch: pytest.MonkeyPatch):
    """A server that was killed cannot be relied on to have cleaned up after itself."""
    registry, daemon, workspace = fake_daemon
    registry.ensure("sb1", "uid-1", "session", str(workspace))

    # Backdate the sandbox so the reaper considers it stale.
    sandbox = registry._sandboxes["sb1"]
    monkeypatch.setattr(sandbox, "last_used", time.monotonic() - 10_000)

    assert registry.reap_idle(100) == ["sb1"]
    assert daemon.containers.created["pi-sandbox-sb1"].removed is True


# --------------------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------------------


def test_health_is_open_and_reports_the_image(client: TestClient, provisioner: Any):
    body = client.get("/health").json()
    assert body["status"] in ("ok", "degraded")
    assert body["image"] == provisioner._IMAGE
    assert "docker" in body


def test_everything_else_needs_the_token(client: TestClient):
    assert client.get("/sandboxes").status_code == 401
    assert client.get("/sandboxes", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.post("/sandboxes/sb1", json={"workspace": "/tmp"}, headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )


def test_an_unset_token_refuses_every_request(provisioner: Any, monkeypatch: pytest.MonkeyPatch):
    """A provisioner with no token is a misconfiguration, not an open door."""
    monkeypatch.setattr(provisioner, "_TOKEN", "")
    client = TestClient(provisioner.app)
    assert client.get("/sandboxes").status_code == 503
    assert client.post("/sandboxes/sb1", json={"workspace": "/tmp"}).status_code == 503


def test_invalid_ids_and_empty_commands_are_rejected(client: TestClient, provisioner: Any):
    """Validation happens before the daemon is touched, so nothing is left behind."""
    headers = _auth(provisioner)

    # A path-shaped id would otherwise name a container outside the prefix.
    assert client.post("/sandboxes/..%2fescape", json={"workspace": "/tmp"}, headers=headers).status_code in (404, 422)
    # A workspace that does not exist is refused rather than mounted empty.
    assert (
        client.post(
            "/sandboxes/sb-validate", json={"workspace": "/no/such/workspace"}, headers=headers
        ).status_code
        == 502
    )
    # No container was created for either: the daemon list stays empty.
    listing = client.get("/sandboxes", headers=headers).json()["sandboxes"]
    assert listing == []


# --------------------------------------------------------------------------------------
# A real container
# --------------------------------------------------------------------------------------


def _docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "the docker CLI is not installed"
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
        return False, result.stderr.strip()[:200]
    return True, result.stdout.strip()


requires_docker = pytest.mark.skipif(
    not _docker_ready()[0], reason=f"needs a docker daemon: {_docker_ready()[1]}"
)


@pytest.fixture()
def real_sandbox(provisioner: Any, tmp_path: Path):
    """A genuine container, torn down afterwards."""
    registry = provisioner.Registry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("from the host\n", encoding="utf-8")

    sandbox_id = f"test{os.getpid()}"
    # Pull if needed, once. `docker run` would do it too, but a failed pull is easier to
    # read here than as an image-not-found from the daemon.
    subprocess.run(["docker", "image", "inspect", TEST_IMAGE], capture_output=True)
    subprocess.run(["docker", "pull", TEST_IMAGE], capture_output=True, timeout=300)

    sandbox = registry.ensure(
        sandbox_id,
        "test-uid",
        "test-thread",
        str(workspace),
        image=TEST_IMAGE,
        network="none",  # a sandbox that needs no network is the honest default for a test
        runAs=f"{os.getuid()}:{os.getgid()}",
    )
    try:
        yield registry, sandbox, workspace
    finally:
        registry.delete(sandbox_id)


@requires_docker
def test_a_real_container_shares_the_workspace_and_nothing_else(real_sandbox: Any):
    registry, sandbox, workspace = real_sandbox

    # 1. The workspace is genuinely mounted.
    read = registry.exec(sandbox.sandbox_id, "cat hello.txt", "/workspace", 30, None)
    assert read["exitCode"] == 0
    assert "from the host" in read["output"]

    # 2. A command can write, and the host sees it — which is what makes the sandbox usable
    #    rather than a prison the agent's work disappears into.
    write = registry.exec(sandbox.sandbox_id, "echo 'from the sandbox' > written.txt", "/workspace", 30, None)
    assert write["exitCode"] == 0, write
    assert (workspace / "written.txt").read_text(encoding="utf-8").strip() == "from the sandbox"


@requires_docker
def test_a_real_container_cannot_reach_the_deployment(real_sandbox: Any):
    """The paths the policy extension has to reason about are, here, not present at all."""
    registry, sandbox, _workspace = real_sandbox

    # No docker socket: escaping the sandbox by starting another one is not on the table.
    socket = registry.exec(sandbox.sandbox_id, "ls /var/run/docker.sock", "/workspace", 30, None)
    assert socket["exitCode"] != 0

    # Only the workspace is mounted; nothing else from the host appears.
    mount = registry.exec(sandbox.sandbox_id, "cat /proc/mounts", "/workspace", 30, None)
    assert "/workspace" in mount["output"]
    assert "workspaces" not in mount["output"].replace("/workspace", "")


@requires_docker
def test_a_real_container_is_capped(real_sandbox: Any):
    """A runaway command is killed, so one user's loop cannot hold the server open."""
    registry, sandbox, _workspace = real_sandbox

    result = registry.exec(sandbox.sandbox_id, "sleep 30", "/workspace", 1.5, None)
    assert result["timedOut"] is True
    assert result["exitCode"] is None


@requires_docker
def test_the_container_does_not_run_as_root_when_told_not_to(real_sandbox: Any):
    registry, sandbox, _workspace = real_sandbox
    result = registry.exec(sandbox.sandbox_id, "id -u", "/workspace", 30, None)
    assert result["output"].strip() == str(os.getuid())
