"""Guards for the deployment artifacts.

These files are not executed by the test suite, which is exactly why they need tests: a
unit that quietly puts the model credential back on the command line, or loses the line
that sandboxes the filesystem, would still pass every other test in this repository and
fail only in production.

So the assertions here are the invariants an operator is relying on, not syntax. Where a
tool is available (`bash -n`, `py_compile`) it is used; where it is not (systemd cannot be
asked to parse a unit on macOS) the important properties are asserted directly.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"


def _read(name: str) -> str:
    path = DEPLOY / name
    assert path.is_file(), f"deploy/{name} is missing"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# systemd unit
# --------------------------------------------------------------------------------------


def test_the_unit_never_puts_the_credential_on_the_command_line():
    """`ps aux` shows argv to every account on the box; the environment does not."""
    unit = _read("pi-server.service")
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))

    assert "--api-key" not in exec_line
    assert "api_key" not in exec_line.replace("PI_API_KEY_FILE", "")


def test_the_unit_reads_the_credential_as_a_systemd_credential():
    unit = _read("pi-server.service")

    assert "LoadCredential=api_key:/etc/pi/api_key" in unit
    # `%d` is the credentials directory systemd creates for the unit.
    assert "PI_API_KEY_FILE=%d/api_key" in unit


def test_the_unit_keeps_its_filesystem_hardening():
    """Without these, "the agent's shell runs as this process" means the whole machine."""
    unit = _read("pi-server.service")

    for required in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "ReadWritePaths=/var/lib/pi",
        "CapabilityBoundingSet=",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "SystemCallFilter=@system-service",
    ):
        assert required in unit, f"{required} is missing from the unit"


def test_the_unit_binds_loopback_and_runs_as_a_dedicated_user():
    unit = _read("pi-server.service")

    assert "--host 127.0.0.1" in unit, "the server must not be exposed directly"
    assert "User=pi" in unit
    assert "MemoryMax=" in unit and "CPUQuota=" in unit


def test_the_unit_does_not_pretend_a_sandbox_exists_yet():
    """Phase two sets this; today it would make the server refuse to start."""
    unit = _read("pi-server.service")
    assert "PI_SANDBOX_BACKEND=" not in unit


def test_the_referenced_environment_file_is_documented_where_operators_look():
    assert _read("pi-server.service").count("/etc/pi/pi.env") >= 1
    env_example = _read("team.env.example")
    assert "PI_AUTH_MODE=team" in env_example
    assert "PI_COOKIE_SECURE=1" in env_example
    # The key belongs in a root-only file, which is what the unit loads. A commented-out
    # trial line is allowed; an active assignment is not.
    active = [
        line
        for line in env_example.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any(line.startswith("PI_API_KEY=") for line in active), (
        "the credential must not be set in the environment file; the unit loads it as a "
        "systemd credential"
    )


# --------------------------------------------------------------------------------------
# Caddy
# --------------------------------------------------------------------------------------


def test_the_proxy_streams_instead_of_buffering():
    """The transcript is a token stream; a buffering proxy turns it into chunks."""
    caddyfile = _read("Caddyfile")

    assert "reverse_proxy 127.0.0.1:8787" in caddyfile
    assert "flush_interval -1" in caddyfile
    assert "Strict-Transport-Security" in caddyfile


# --------------------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------------------


def test_backup_script_is_executable_and_syntactically_valid():
    path = DEPLOY / "backup.sh"
    assert os.access(path, os.X_OK), "deploy/backup.sh is not executable"
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_backup_uses_sqlite_backup_not_a_file_copy():
    """The database is in WAL mode; copying the file can lose the newest transactions."""
    script = _read("backup.sh")

    assert ".backup" in script
    assert "sqlite3" in script


def test_smoke_script_compiles_and_covers_the_anonymous_gate():
    path = DEPLOY / "smoke.py"
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

    source = path.read_text(encoding="utf-8")
    assert "/api/workspace/tree" in source
    assert "/api/admin/users" in source
    assert "authMode" in source


def test_the_operator_guide_exists_and_states_the_limits():
    """A deployment doc that omits the limits is how false confidence gets installed."""
    guide = (REPO_ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")

    assert "没有沙盒" in guide
    assert "单进程是硬约束" in guide
    assert "PI_TEMPLATE_AGENT_DIR" in guide


@pytest.mark.parametrize("script", ["backup.sh"])
def test_deploy_scripts_do_not_echo_secrets(script: str):
    """`set -x` would put a credential in the logs the moment a step fails."""
    assert "set -x" not in _read(script)


# --------------------------------------------------------------------------------------
# The sandbox provisioner
# --------------------------------------------------------------------------------------


def _compose_service(name: str) -> str:
    """The text of one service block, so assertions cannot match another service's config."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"  {name}:" in compose, f"service {name} is missing from docker-compose.yml"
    block = compose.split(f"  {name}:", 1)[1]
    # Services are indented two spaces; the next one (or the top-level keys) ends this block.
    lines: list[str] = []
    for line in block.splitlines()[1:]:
        if line.strip() and not line.startswith("    ") and re.match(r"^  [a-z]", line):
            break
        lines.append(line)
    return "\n".join(lines)


def test_the_provisioner_is_the_only_service_with_the_docker_socket():
    """The process that runs the agent's code must not be able to start containers."""
    assert "/var/run/docker.sock" in _compose_service("sandbox-provisioner")
    assert "/var/run/docker.sock" not in _compose_service("server")


def test_the_compose_file_maps_host_paths_for_bind_mounts():
    """The daemon resolves bind mounts on the host; the app only knows its own /data."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "PI_SANDBOX_HOST_DATA_ROOT" in compose
    assert "PI_DATA_ROOT: /data" in compose
    # The default must be the sandboxed one; the script may override it to `local` for a
    # deployment that has no provisioner.
    assert "PI_SANDBOX_BACKEND: ${PI_SANDBOX_BACKEND:-provisioner}" in compose


def test_the_compose_file_keeps_the_credential_out_of_argv():
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "PI_API_KEY_FILE" in compose
    assert "--api-key" not in compose


def test_the_provisioner_refuses_to_run_without_a_token():
    """An unauthenticated provisioner would let anything on the network start containers."""
    source = (DEPLOY / "sandbox_provisioner" / "app.py").read_text(encoding="utf-8")

    assert "SANDBOX_PROVISIONER_TOKEN is not set; refusing every request" in source
    assert "def require_token" in source


def test_the_provisioner_drops_privileges_on_every_container():
    source = (DEPLOY / "sandbox_provisioner" / "app.py").read_text(encoding="utf-8")

    for required in (
        '"cap_drop": ["ALL"]',
        '"no-new-privileges:true"',
        '"pids_limit"',
        '"mem_limit"',
        '"nano_cpus"',
    ):
        assert required in source, f"{required} is missing from the container configuration"


def test_the_sandbox_image_pins_the_tools_the_provisioner_assumes():
    """`timeout` is what bounds every command; the image must be able to prove it has it."""
    dockerfile = (DEPLOY / "sandbox_image" / "Dockerfile").read_text(encoding="utf-8")

    assert "coreutils" in dockerfile or "timeout" in dockerfile

    # No ENTRYPOINT *instruction*: the provisioner starts the container with `sleep infinity`
    # and execs each command, so an entrypoint would replace that and the container would
    # exit immediately. (Comments mentioning the word are fine.)
    instructions = [
        line.strip().split()[0].upper()
        for line in dockerfile.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "ENTRYPOINT" not in instructions


# --------------------------------------------------------------------------------------
# deploy_docker.py
# --------------------------------------------------------------------------------------

DEPLOY_SCRIPT = DEPLOY / "deploy_docker.py"


def _deploy_module():
    """Import the deployment script as a module, the way its own tests need it."""
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location("pi_deploy_docker", DEPLOY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_deploy_script_compiles_and_is_executable():
    assert os.access(DEPLOY_SCRIPT, os.X_OK), "deploy/deploy_docker.py is not executable"
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(DEPLOY_SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_every_subcommand_an_operator_needs_exists():
    """These are the verbs the docs use; renaming one silently breaks the guide."""
    module = _deploy_module()
    parser = module.build_parser()
    actions = [
        action for action in parser._actions if isinstance(action, type(parser._subparsers))
    ]
    names: set[str] = set()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            names |= set(choices)
    assert {"up", "down", "status", "logs", "smoke", "doctor"} <= names


def test_the_managed_container_names_match_the_compose_file():
    """The port guard decides "ours vs someone else's" by name; a drift would refuse our own."""
    module = _deploy_module()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    for name in module.MANAGED_CONTAINERS:
        assert f"container_name: {name}" in compose, f"{name} is not a container_name in compose"


def test_the_deploy_script_never_puts_a_secret_on_a_command_line():
    """argv is readable by every account on the host; env files and stdin are not."""
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # The model credential is written to a file that compose mounts as a docker secret.
    assert "write_secret(API_KEY_FILE, config.api_key)" in source
    # `--api-key` appears exactly once: the argparse definition a human uses. It is never an
    # argument to a docker command, which is where `ps` would expose it.
    assert source.count('"--api-key"') == 1
    # The smoke password is piped in: the function that shells out to docker must not name it.
    run_smoke = source.split("def run_smoke(")[1].split("\ndef ")[0]
    assert '"--password-stdin"' in run_smoke
    assert '"--password"' not in run_smoke
    assert "input=password" in run_smoke


def test_down_keeps_the_data():
    """`down -v` would delete volumes; there are none, and adding one must not change that."""
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert '"down", "--remove-orphans"' in source
    assert '"down", "-v"' not in source


def test_secure_cookies_are_the_default(tmp_path, monkeypatch):
    """Turning Secure off has to be an explicit, named decision."""
    module = _deploy_module()
    # Point the script at an env file that does not exist, so nothing is persisted yet.
    monkeypatch.setattr(module, "ENV_FILE", tmp_path / ".env.team")
    monkeypatch.delenv("PI_API_KEY", raising=False)

    default = module.resolve_config(module.build_parser().parse_args(["doctor"]))
    assert default.cookie_secure is True, "Secure must be the default, not the opt-in"

    insecure = module.resolve_config(
        module.build_parser().parse_args(["up", "--insecure-cookies"])
    )
    assert insecure.cookie_secure is False


def test_the_env_file_is_written_private(tmp_path):
    module = _deploy_module()
    target = tmp_path / ".env.team"
    module.write_env_file(target, {"PI_UID": "1000", "SANDBOX_PROVISIONER_TOKEN": "t"})

    assert target.stat().st_mode & 0o777 == 0o600
    # Unmanaged keys survive a rewrite, so an operator's own overrides are not lost.
    target.write_text(target.read_text() + "MY_OVERRIDE=1\n")
    module.write_env_file(target, {"PI_UID": "1001", "SANDBOX_PROVISIONER_TOKEN": "t"})
    assert "MY_OVERRIDE=1" in target.read_text()
    assert "PI_UID=1001" in target.read_text()


def test_rewriting_a_read_only_secret_works(tmp_path):
    """The file is 0400 by design, so a second run has to widen it before writing."""
    module = _deploy_module()
    secret = tmp_path / "api_key"
    module.write_secret(secret, "first")
    assert secret.read_text() == "first"
    assert secret.stat().st_mode & 0o777 == 0o400

    module.write_secret(secret, "second")
    assert secret.read_text() == "second"
    assert secret.stat().st_mode & 0o777 == 0o400


def test_the_smoke_password_can_come_from_stdin():
    source = (DEPLOY / "smoke.py").read_text(encoding="utf-8")
    assert "--password-stdin" in source
    assert "sys.stdin.readline()" in source
