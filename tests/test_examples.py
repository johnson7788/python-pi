"""Guards the runnable example.

Executable documentation rots silently.  This runs ``examples/offline_agent.py``
in a subprocess exactly as a reader would and asserts the promised effects: the
agent drove a real tool, created a real file, and persisted a real session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "examples" / "offline_agent.py"


def test_offline_example_runs_and_produces_a_session():
    result = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": ""},
    )

    assert result.returncode == 0, f"example failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    out = result.stdout
    assert "[tool] write(" in out
    assert "[tool] write -> ok" in out
    assert "Wrote greeting.txt." in out
    assert "greeting.txt" in out
    # header + user + assistant + toolResult + assistant
    assert "session lines: 5" in out
