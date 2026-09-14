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
