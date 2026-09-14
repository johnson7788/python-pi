"""CLI run modes for the coding agent.

v0.1 provides the two non-interactive modes:

* :mod:`~pi_coding_agent.modes.print_mode` -- stream assistant text to stdout;
* :mod:`~pi_coding_agent.modes.json_mode` -- one JSON event object per line.

Both return a process exit code and keep stdout free of diagnostics.
"""

from __future__ import annotations

from .json_mode import run_json_mode
from .print_mode import run_print_mode

__all__ = ["run_json_mode", "run_print_mode"]
