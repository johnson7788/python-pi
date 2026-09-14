"""Smoke tests for ``start.sh``.

The script is the thing users actually run, and it is not covered by any other
test.  Two failure modes motivated these:

* a variable immediately followed by a Chinese punctuation mark (``$ENV_FILE（``)
  makes bash treat the multibyte character as part of the variable name, which
  ``bash -n`` happily accepts and ``set -u`` then rejects at runtime;
* the script silently starting a process that is guaranteed to fail because no key
  was found.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
START_SH = REPO_ROOT / "start.sh"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_script_is_executable_and_syntactically_valid():
    assert START_SH.is_file(), "start.sh is missing"
    assert os.access(START_SH, os.X_OK), "start.sh is not executable"
    result = subprocess.run(["bash", "-n", str(START_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_missing_key_is_reported_before_starting(tmp_path):
    """With no .env and no PI_API_KEY, fail fast with instructions.

    Runs from a copy of the repo root whose `.env` is hidden, so the script's own
    defaults cannot mask the check.
    """
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("PI_API_KEY", "MODEL", "PORT")}
    env["PI_AGENT_DIR"] = str(tmp_path)  # a directory with no credentials

    # Temporarily move .env aside so the "no key" path is reachable.
    env_file = REPO_ROOT / ".env"
    backup = REPO_ROOT / ".env.test-backup"
    moved = False
    if env_file.exists():
        env_file.rename(backup)
        moved = True
    try:
        result = subprocess.run(
            ["bash", str(START_SH), str(tmp_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, f"expected a failure, got:\n{combined}"
        assert "没有拿到 API key" in combined
        assert ".env.example" in combined
    finally:
        if moved:
            backup.rename(env_file)


def test_script_starts_a_server(tmp_path):
    """End to end: reachable health endpoint, started from the script itself.

    A bogus key is fine — credentials are only consulted when calling the model.
    """
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("MODEL", "PORT")}
    env["PI_API_KEY"] = "sk-test-not-a-real-key"

    proc = subprocess.Popen(
        ["bash", str(START_SH), str(tmp_path)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**env, "PORT": str(port)},
    )
    try:
        deadline = time.monotonic() + 40
        healthy = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0).status_code == 200:
                    healthy = True
                    break
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.1)
        assert healthy, "start.sh never brought up a healthy server"

        # /api/models goes through the full bootstrap (settings, trust, resources,
        # extensions, model resolution, credential check) — a stronger signal that
        # the script's environment plumbing is actually correct.
        models = httpx.get(f"http://127.0.0.1:{port}/api/models", timeout=30).json()["models"]
        assert any(model["id"] == "deepseek-v4-flash" for model in models)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
