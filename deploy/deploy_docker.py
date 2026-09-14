#!/usr/bin/env python3
"""Deploy the team server with Docker, from one command.

    python deploy/deploy_docker.py up          # preflight -> config -> build -> start -> verify
    python deploy/deploy_docker.py status
    python deploy/deploy_docker.py logs -f
    python deploy/deploy_docker.py smoke --username admin --password '…'
    python deploy/deploy_docker.py down

Why a script rather than a paragraph of shell in the docs
---------------------------------------------------------

The manual steps have four places where a mistake is silent rather than loud:

* **The host path of the data root.**  The Docker daemon resolves bind mounts on the host,
  while the server container knows the same directory as ``/data``.  If the two disagree the
  sandbox starts with an *empty* workspace and every command still "succeeds".
* **uid/gid.**  Files written inside a sandbox are owned by whatever the container ran as.
  Get it wrong and the server can no longer edit the agent's own output.
* **The session cookie.**  With ``PI_COOKIE_SECURE=1`` on a plain-HTTP trial, login appears
  to work and then bounces back to the login page.
* **Secrets.**  A token written with the wrong permissions, or an API key that ends up in
  argv where ``ps`` shows it to everyone.

So the script computes those from the machine it runs on, refuses to guess where guessing
would be unsafe, and verifies the result instead of printing "done".

Idempotent by design: re-running ``up`` reuses the existing env file, token and API key, and
never deletes data.  Nothing here removes a container it did not create.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_FILE = REPO_ROOT / ".env.team"
SECRETS_DIR = REPO_ROOT / "secrets"
API_KEY_FILE = SECRETS_DIR / "api_key"
SANDBOX_DOCKERFILE = REPO_ROOT / "deploy" / "sandbox_image"
DEFAULT_SANDBOX_IMAGE = "pi-sandbox:latest"
SERVER_SERVICE = "server"

#: Containers this deployment owns. Used to tell "our own stack is already up" (recreate it)
#: from "something else has the port" (refuse and say so).
MANAGED_CONTAINERS = frozenset({"pi-server", "pi-sandbox-provisioner"})

#: Health checks are cheap; give the first boot (image build + migrations) real room.
HEALTH_TIMEOUT_SECONDS = 180.0


class DeployError(RuntimeError):
    """Something the operator has to fix.  The message says what."""


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


class Log:
    """Deliberately plain: this runs before anything is deployed, so it cannot assume a UI."""

    @staticmethod
    def step(message: str) -> None:
        print(f"\n\033[1m==> {message}\033[0m", flush=True)

    @staticmethod
    def info(message: str) -> None:
        print(f"    {message}", flush=True)

    @staticmethod
    def ok(message: str) -> None:
        print(f"    \033[32m✓\033[0m {message}", flush=True)

    @staticmethod
    def warn(message: str) -> None:
        print(f"    \033[33m!\033[0m {message}", flush=True)

    @staticmethod
    def fail(message: str) -> None:
        print(f"    \033[31m✗\033[0m {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass
class Config:
    """Everything the deployment needs, resolved from flags, env and the machine."""

    data_root: Path
    port: int
    uid: int
    gid: int
    sandbox: bool
    cookie_secure: bool
    admin_username: str
    sandbox_image: str
    server_image: str
    api_key: str | None
    token: str
    extras: dict[str, str] = field(default_factory=dict)

    def as_env(self) -> dict[str, str]:
        """The values compose interpolates.  Secrets live in files, not here."""
        values = {
            "PI_UID": str(self.uid),
            "PI_GID": str(self.gid),
            "PI_PORT": str(self.port),
            "PI_SANDBOX_HOST_DATA_ROOT": str(self.data_root),
            "PI_SANDBOX_BACKEND": "provisioner" if self.sandbox else "local",
            "PI_COOKIE_SECURE": "1" if self.cookie_secure else "0",
            "PI_ADMIN_USERNAME": self.admin_username,
            "SANDBOX_PROVISIONER_TOKEN": self.token,
            "SANDBOX_IMAGE": self.sandbox_image,
        }
        values.update(self.extras)
        return values


def default_data_root() -> Path:
    """Where the users' workspaces live on the host.

    ``/srv/pi`` is the documented location and is right on a server.  It is not writable by
    an ordinary account on macOS or on a fresh Linux box, so the fallback is under the home
    directory — a trial deployment that cannot create its own data directory is a bad first
    impression, and a world-writable ``/srv`` would be worse.
    """
    candidate = Path("/srv/pi")
    if os.geteuid() == 0 if hasattr(os, "geteuid") else False:
        return candidate
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except (PermissionError, OSError):
        return Path.home() / "pi-data"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Rewrite the env file, preserving keys we do not manage (an operator's own overrides)."""
    known = {
        "PI_UID",
        "PI_GID",
        "PI_PORT",
        "PI_SANDBOX_HOST_DATA_ROOT",
        "PI_SANDBOX_BACKEND",
        "PI_COOKIE_SECURE",
        "PI_ADMIN_USERNAME",
        "SANDBOX_PROVISIONER_TOKEN",
        "SANDBOX_IMAGE",
    }
    preserved = {k: v for k, v in read_env_file(path).items() if k not in known}
    merged = {**preserved, **values}
    body = [
        "# Written by deploy/deploy_docker.py. Safe to edit; re-running `up` keeps unknown keys.",
        "# Secrets are NOT here: the model key is in secrets/api_key, mounted as a docker secret.",
        "",
    ]
    body.extend(f"{key}={value}" for key, value in merged.items())
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(0o600)


# --------------------------------------------------------------------------------------
# Command runner
# --------------------------------------------------------------------------------------


class Docker:
    """Thin wrapper over the docker and docker-compose CLIs."""

    def __init__(self, env_file: Path, *, verbose: bool = False) -> None:
        self.env_file = env_file
        self.verbose = verbose

    def base_command(self, compose: bool) -> list[str]:
        if not compose:
            return ["docker"]
        return [
            "docker",
            "compose",
            "--project-directory",
            str(REPO_ROOT),
            "--env-file",
            str(self.env_file),
            "-f",
            str(COMPOSE_FILE),
        ]

    def run(
        self,
        args: list[str],
        *,
        compose: bool = False,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        command = self.base_command(compose) + args
        if self.verbose:
            Log.info("$ " + " ".join(command))
        result = subprocess.run(
            command,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise DeployError(f"`{' '.join(command)}` failed:\n{detail[:800]}")
        return result

    # -- queries -----------------------------------------------------------------------

    def daemon_version(self) -> str:
        result = self.run(["version", "--format", "{{.Server.Version}}"], check=False)
        if result.returncode != 0:
            raise DeployError(
                "the docker daemon is not reachable. Start Docker (or fix DOCKER_HOST) and "
                "try again."
            )
        return (result.stdout or "").strip()

    def compose_version(self) -> str:
        result = self.run(["compose", "version", "--short"], check=False)
        if result.returncode != 0:
            raise DeployError(
                "`docker compose` (v2) is not available. Install the compose plugin."
            )
        return (result.stdout or "").strip()

    def image_exists(self, image: str) -> bool:
        return self.run(["image", "inspect", image], check=False).returncode == 0

    def containers(self, *, all_: bool = False) -> list[dict]:
        args = ["ps", "--format", "{{json .}}"]
        if all_:
            args.insert(1, "-a")
        result = self.run(args, check=False)
        rows = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def sandbox_containers(self) -> list[str]:
        """Sandbox containers, matched by the label the provisioner stamps — nothing else."""
        result = self.run(
            ["ps", "-aq", "--filter", "label=app=pi-sandbox"], check=False
        )
        return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


# --------------------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------------------


def port_is_busy(port: int) -> bool:
    """Whether something already listens on the loopback port.

    Checked against the *host*, not just containers: a plain `start.sh` instance left running
    occupies 8787 too, and the only symptom otherwise is a bind failure buried in compose's
    output several minutes later.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def preflight(docker: Docker, config: Config) -> None:
    Log.step("Preflight")
    Log.ok(f"docker daemon {docker.daemon_version()}")
    Log.ok(f"compose {docker.compose_version()}")

    if not COMPOSE_FILE.is_file():
        raise DeployError(f"{COMPOSE_FILE} is missing")
    Log.ok(f"compose file {COMPOSE_FILE.relative_to(REPO_ROOT)}")

    if config.sandbox and not docker.image_exists(config.sandbox_image):
        Log.warn(f"sandbox image {config.sandbox_image} is missing; it will be built")

    busy = [row for row in docker.containers() if f":{config.port}->" in (row.get("Ports") or "")]
    ours = [row for row in busy if (row.get("Names") or "").strip() in MANAGED_CONTAINERS]
    foreign = [row for row in busy if row not in ours]
    if foreign:
        raise DeployError(
            f"port {config.port} is already published by container "
            f"{foreign[0].get('Names')}. Pass --port to use another one."
        )
    if ours:
        # Re-running `up` on an existing deployment must work; compose will recreate it.
        Log.info(f"port {config.port} is held by our own {ours[0].get('Names')}; it will be recreated")
    elif port_is_busy(config.port):
        raise DeployError(
            f"port {config.port} is already in use on this machine (a running `start.sh`, "
            f"another service, or a previous deployment). Pass --port to use another one."
        )
    else:
        Log.ok(f"port {config.port} is free")

    if shutil.which("git") is None:
        Log.warn("git is not on PATH; agent commands inside the sandbox will not have it")


def ensure_directories(config: Config) -> None:
    Log.step("Host directories")
    for path in (config.data_root, config.data_root / "template"):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o750)
        Log.ok(f"{path}")
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.chmod(0o700)

    template = config.data_root / "template"
    if not (template / "models.json").is_file():
        Log.warn(
            f"{template}/models.json is missing. Every user's agent directory is primed from "
            "there; without it a provider that needs a baseUrl override will not resolve. "
            "Copy your ~/.pi/agent/models.json in if you use one."
        )


def write_secret(path: Path, value: str) -> None:
    """Write a secret file, tolerating one a previous run left read-only.

    The file is deliberately 0400 once written, which means a later run cannot simply open it
    for writing — it has to widen the mode first. Doing that explicitly is better than
    deleting the file, because a delete-then-write would lose the old credential if the write
    then failed.
    """
    try:
        if path.exists():
            path.chmod(0o600)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o400)
    except OSError as error:
        raise DeployError(
            f"cannot write {path}: {error}.\n"
            f"  If it is owned by another account (a previous `sudo` run), fix it with:\n"
            f"    sudo rm -f {path}"
        ) from error


def ensure_api_key(config: Config) -> None:
    """Put the model credential in a file the container reads as a docker secret."""
    Log.step("Model credential")
    if config.api_key:
        write_secret(API_KEY_FILE, config.api_key)
        Log.ok(f"wrote {API_KEY_FILE.relative_to(REPO_ROOT)} (mode 0400) from --api-key/PI_API_KEY")
        return

    if API_KEY_FILE.is_file() and API_KEY_FILE.read_text(encoding="utf-8").strip():
        Log.ok(f"reusing {API_KEY_FILE.relative_to(REPO_ROOT)}")
        return

    existing = config.extras.get("PI_API_KEY")
    if existing:
        write_secret(API_KEY_FILE, existing)
        Log.ok("migrated PI_API_KEY from the env file into the secret file")
        return

    raise DeployError(
        "no model credential. Either put yours in secrets/api_key:\n"
        f"    install -m 0400 /dev/null {API_KEY_FILE}\n"
        f"    printf %s 'sk-…' > {API_KEY_FILE}\n"
        "  or pass --api-key once (it is written to that file, never to argv of the server)."
    )


def ensure_token(config: Config) -> str:
    """The shared secret between the server and the provisioner.

    Kept across runs: rotating it would mean the running server can no longer ask for a
    sandbox, and nothing would say so until a user's command failed.
    """
    existing = read_env_file(ENV_FILE).get("SANDBOX_PROVISIONER_TOKEN", "")
    if existing:
        return existing
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------------------
# Build and run
# --------------------------------------------------------------------------------------


def build_sandbox_image(docker: Docker, config: Config, *, force: bool) -> None:
    if not config.sandbox:
        Log.info("sandbox disabled; not building the sandbox image")
        return
    Log.step("Sandbox image")
    if docker.image_exists(config.sandbox_image) and not force:
        Log.ok(f"{config.sandbox_image} already present (use --rebuild-sandbox to refresh)")
        return
    if not SANDBOX_DOCKERFILE.is_dir():
        raise DeployError(f"{SANDBOX_DOCKERFILE} is missing")
    docker.run(["build", "-t", config.sandbox_image, str(SANDBOX_DOCKERFILE)], timeout=1800)
    Log.ok(f"built {config.sandbox_image}")


def compose_up(docker: Docker, config: Config) -> None:
    Log.step("Starting the stack")
    docker.run(["up", "-d", "--build", "--remove-orphans"], compose=True, timeout=3600)
    # `docker compose ps` is scoped to this project. `docker ps` lists every unrelated
    # container on the machine, which buries our two services in other people's stacks.
    result = docker.run(["ps", "--format", "json"], compose=True, check=False)
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        Log.ok(f"{row.get('Service') or row.get('Name')}  {row.get('Status')}")


def wait_for_health(config: Config, *, timeout: float = HEALTH_TIMEOUT_SECONDS) -> dict:
    Log.step("Waiting for the server")
    url = f"http://127.0.0.1:{config.port}/api/health"
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - loopback
                payload = json.loads(response.read().decode("utf-8"))
                Log.ok(f"healthy after {timeout - (deadline - time.monotonic()):.1f}s: {payload.get('authMode')}")
                return payload
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
        except Exception as error:  # noqa: BLE001 - not up yet
            last = str(error)
        time.sleep(1.0)
    raise DeployError(
        f"the server never became healthy at {url} (last error: {last}).\n"
        "  Inspect it with:  python deploy/deploy_docker.py logs --tail 80"
    )


def generated_admin_password(docker: Docker) -> tuple[str, str] | None:
    """The one-time credential from a first boot, if this was one.

    Read out of the logs because that is the only place it exists — the server prints it once
    and stores only a hash.  If it is gone, the operator resets it rather than us inventing a
    way to recover it.
    """
    result = docker.run(["logs", SERVER_SERVICE], compose=True, check=False)
    text = (result.stdout or "") + (result.stderr or "")
    match = re.search(r"username:\s*(\S+)\s*\n.*?password:\s*(\S+)", text)
    if match:
        return match.group(1), match.group(2)
    return None


# --------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------


def public_checks(config: Config) -> list[str]:
    """What can be checked without credentials — and the check that matters most."""
    base = f"http://127.0.0.1:{config.port}"
    failures: list[str] = []

    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:  # noqa: S310
            health = json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001
        return [f"/api/health unreachable: {error}"]

    if health.get("authMode") != "team":
        failures.append(
            f"authMode is {health.get('authMode')!r}, not 'team' — accounts are disabled, so "
            "this deployment has no isolation at all"
        )

    for path in ("/api/state", "/api/workspace/tree", "/api/admin/users"):
        try:
            urllib.request.urlopen(f"{base}{path}", timeout=5)  # noqa: S310
            failures.append(f"anonymous {path} was allowed — the auth gate is not in front of it")
        except urllib.error.HTTPError as error:
            if error.code != 401:
                failures.append(f"anonymous {path} returned HTTP {error.code}, expected 401")
        except Exception as error:  # noqa: BLE001
            failures.append(f"anonymous {path} could not be checked: {error}")
    return failures


def run_smoke(config: Config, username: str, password: str, docker: Docker) -> int:
    """Run deploy/smoke.py inside the server container, where httpx already exists.

    The password goes in over stdin, not as an argument: `docker exec … --password …` would
    publish it to every account on the host through `ps`, for the second or so the check runs.
    """
    command = docker.base_command(compose=True) + [
        "exec",
        "-i",
        SERVER_SERVICE,
        "python",
        "deploy/smoke.py",
        "http://127.0.0.1:8787",
        "--username",
        username,
        "--password-stdin",
    ]
    if docker.verbose:
        Log.info("$ " + " ".join(command) + "   (password via stdin)")
    result = subprocess.run(
        command, input=password + "\n", text=True, capture_output=False
    )
    return result.returncode


# --------------------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------------------


def resolve_config(args: argparse.Namespace) -> Config:
    """Work out the configuration from flags, then the env file, then the machine.

    Only ``up`` defines every flag, so the other subcommands read what is actually deployed.
    That way ``status`` and ``smoke`` describe the running stack rather than the defaults they
    would have used if the env file were ignored.
    """
    persisted = read_env_file(ENV_FILE)
    data_root = Path(
        getattr(args, "data_root", None)
        or persisted.get("PI_SANDBOX_HOST_DATA_ROOT")
        or default_data_root()
    ).expanduser().resolve()
    port = int(getattr(args, "port", None) or persisted.get("PI_PORT") or 8787)

    sandbox = True
    if getattr(args, "no_sandbox", False):
        sandbox = False
    elif persisted.get("PI_SANDBOX_BACKEND") == "local":
        sandbox = False
    if getattr(args, "sandbox", False):
        sandbox = True

    if getattr(args, "insecure_cookies", False):
        cookie_secure = False
    elif persisted.get("PI_COOKIE_SECURE") is not None:
        cookie_secure = persisted.get("PI_COOKIE_SECURE") == "1"
    else:
        cookie_secure = True

    return Config(
        data_root=data_root,
        port=port,
        uid=os.getuid() if hasattr(os, "getuid") else 1000,
        gid=os.getgid() if hasattr(os, "getgid") else 1000,
        sandbox=sandbox,
        cookie_secure=cookie_secure,
        admin_username=getattr(args, "admin_username", None)
        or persisted.get("PI_ADMIN_USERNAME")
        or "admin",
        sandbox_image=getattr(args, "sandbox_image", None)
        or persisted.get("SANDBOX_IMAGE")
        or DEFAULT_SANDBOX_IMAGE,
        server_image=getattr(args, "server_image", None) or "",
        api_key=getattr(args, "api_key", None) or os.environ.get("PI_API_KEY"),
        token="",
    )


def command_doctor(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    docker = Docker(ENV_FILE, verbose=args.verbose)
    Log.step("Resolved configuration")
    Log.info(f"data root     : {config.data_root}")
    Log.info(f"published port: 127.0.0.1:{config.port}")
    Log.info(f"uid:gid       : {config.uid}:{config.gid}")
    Log.info(f"sandbox       : {'provisioner (commands run in containers)' if config.sandbox else 'local (commands run in the server process)'}")
    Log.info(f"cookies       : {'Secure (needs HTTPS)' if config.cookie_secure else 'insecure (local trial only)'}")
    preflight(docker, config)

    Log.step("Existing state")
    Log.info(f"env file      : {'present' if ENV_FILE.is_file() else 'absent (will be created)'}")
    Log.info(f"api key file  : {'present' if API_KEY_FILE.is_file() else 'absent'}")
    sandboxes = docker.sandbox_containers()
    Log.info(f"sandbox containers: {len(sandboxes)}")
    if args.verbose:
        for row in docker.containers(all_=True):
            Log.info(f"  {row.get('Names')}  {row.get('Status')}")
    return 0


def command_up(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    existing = read_env_file(ENV_FILE)
    config.token = ensure_token(config)
    # An env file may carry a key from an older layout; keep it rather than losing it.
    if "PI_API_KEY" in existing:
        config.extras["PI_API_KEY"] = existing["PI_API_KEY"]

    docker = Docker(ENV_FILE, verbose=args.verbose)
    preflight(docker, config)
    ensure_directories(config)
    write_env_file(ENV_FILE, config.as_env())
    Log.step("Configuration")
    Log.ok(f"{ENV_FILE.relative_to(REPO_ROOT)} (mode 0600)")
    ensure_api_key(config)
    build_sandbox_image(docker, config, force=args.rebuild_sandbox)

    compose_up(docker, config)
    health = wait_for_health(config)

    Log.step("Verification")
    first_boot = health.get("activeUsers") == 0 and config.admin_username
    credentials = generated_admin_password(docker)
    failures = public_checks(config)
    if failures:
        for failure in failures:
            Log.fail(failure)
    else:
        Log.ok("health reports team mode")
        Log.ok("anonymous access to data endpoints is refused (401)")

    print()
    Log.step("Next")
    if credentials is not None:
        username, password = credentials
        print(f"    首次启动的管理员账号：{username} / {password}")
        print("    这个密码只在这里出现过一次，登录后必须修改。")
        if not failures:
            code = run_smoke(config, username, password, docker)
            Log.info(f"deploy/smoke.py exited {code}")
    elif args.username and args.password:
        run_smoke(config, args.username, args.password, docker)
    else:
        print("    用 deploy/deploy_docker.py smoke --username … --password … 跑完整自检。")

    print(f"    打开 http://127.0.0.1:{config.port}"
          + ("" if config.cookie_secure else "   （本机试用模式，cookie 未要求 HTTPS）"))
    print("    日志：python deploy/deploy_docker.py logs -f")
    if config.cookie_secure:
        print("    注意：cookie 带 Secure，必须经 HTTPS 访问，否则登录会跳回登录页。")

    return 1 if failures else 0


def command_down(args: argparse.Namespace) -> int:
    docker = Docker(ENV_FILE, verbose=args.verbose)
    Log.step("Stopping the stack")
    # `stop` and `down` leave the data root alone; only volumes are removed, and there are none.
    docker.run(["down", "--remove-orphans"], compose=True)

    leftovers = docker.sandbox_containers()
    if leftovers:
        Log.info(f"removing {len(leftovers)} sandbox container(s) left by the provisioner")
        docker.run(["rm", "-f", *leftovers])
    Log.ok("stopped. Data is untouched; `up` brings it back.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    docker = Docker(ENV_FILE, verbose=args.verbose)
    Log.step("Containers")
    rows = docker.containers(all_=True)
    if not rows:
        Log.info("none")
    for row in rows:
        if str(row.get("Names", "")).startswith("pi-"):
            Log.info(f"{row.get('Names')}  {row.get('Status')}  {row.get('Ports') or ''}")

    Log.step("Sandboxes")
    sandboxes = docker.sandbox_containers()
    Log.info(f"{len(sandboxes)} container(s)")

    Log.step("Health")
    try:
        with urllib.request.urlopen(  # noqa: S310
            f"http://127.0.0.1:{config.port}/api/health", timeout=5
        ) as response:
            Log.ok(json.dumps(json.loads(response.read().decode("utf-8")), ensure_ascii=False))
    except Exception as error:  # noqa: BLE001
        Log.fail(f"unreachable: {error}")
        return 1
    return 0


def command_logs(args: argparse.Namespace) -> int:
    docker = Docker(ENV_FILE, verbose=args.verbose)
    tail = ["--tail", str(args.tail), "-f"] if args.follow else ["--tail", str(args.tail)]
    service = [] if args.all else [SERVER_SERVICE]
    return docker.run(["logs", *tail, *service], compose=True, check=False, capture=False).returncode


def command_smoke(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    docker = Docker(ENV_FILE, verbose=args.verbose)
    failures = public_checks(config)
    for failure in failures:
        Log.fail(failure)
    if not failures:
        Log.ok("public checks pass")
    if not (args.username and args.password):
        Log.warn("pass --username and --password for the full 20-check smoke test")
        return 1 if failures else 0
    return run_smoke(config, args.username, args.password, docker) or (1 if failures else 0)


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy_docker.py",
        description="Deploy the python-pi team server with Docker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "typical run:\n"
            "  python deploy/deploy_docker.py up --api-key sk-…\n"
            "  python deploy/deploy_docker.py smoke --username admin --password '…'\n"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="print every docker command")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--data-root", help="host directory holding the data (default /srv/pi)")
        target.add_argument("--port", help="loopback port to publish (default 8787)")
        target.add_argument("--sandbox-image", help=f"container image for sandboxes (default {DEFAULT_SANDBOX_IMAGE})")
        target.add_argument("--server-image", help="tag for the built server image")
        target.add_argument(
            "--no-sandbox",
            action="store_true",
            help="run commands in the server process instead of a container",
        )
        target.add_argument(
            "--sandbox",
            action="store_true",
            help="force the sandbox on even if the env file says local",
        )

    up = sub.add_parser("up", help="configure, build, start and verify")
    add_common(up)
    up.add_argument("--api-key", help="model credential; written to secrets/api_key, never to argv of the server")
    up.add_argument("--admin-username", help="name of the first superadmin (default admin)")
    up.add_argument(
        "--insecure-cookies",
        action="store_true",
        help="clear the cookie's Secure flag — for a plain-HTTP local trial only",
    )
    up.add_argument("--rebuild-sandbox", action="store_true", help="rebuild the sandbox image")
    up.add_argument("--username", help="existing account, for a smoke test after startup")
    up.add_argument("--password", help="its password")
    up.set_defaults(func=command_up)

    down = sub.add_parser("down", help="stop the stack (data is kept)")
    down.set_defaults(func=command_down)

    status = sub.add_parser("status", help="show containers, sandboxes and health")
    add_common(status)
    status.set_defaults(func=command_status)

    logs = sub.add_parser("logs", help="tail the server log")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--tail", type=int, default=40)
    logs.add_argument("--all", action="store_true", help="include the provisioner")
    logs.set_defaults(func=command_logs)

    smoke = sub.add_parser("smoke", help="verify the deployment is actually intercepting")
    add_common(smoke)
    smoke.add_argument("--username")
    smoke.add_argument("--password")
    smoke.set_defaults(func=command_smoke)

    doctor = sub.add_parser("doctor", help="check the machine and print the resolved config")
    add_common(doctor)
    doctor.set_defaults(func=command_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DeployError as error:
        Log.fail(str(error))
        return 2
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
