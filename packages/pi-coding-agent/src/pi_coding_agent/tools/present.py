"""The ``present`` tool: declare finished files as user-facing deliverables.

This mirrors DeepSeek Harness' delivery contract: the model calls ``present``
after creating final files, and the transcript renders those paths as cards at
the end of the turn instead of asking the user to hunt through the workspace.
"""

from __future__ import annotations

import os
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolUpdateCallback
from pi_ai import AbortSignal

from .path_utils import error_result, render_path, resolve_path, text_result

DESCRIPTION = (
    "Declare existing workspace files as final deliverables for the user. "
    "Call this once the files are final, especially for Word documents, spreadsheets, "
    "slide decks and images. The UI shows these files as cards at the end of the turn."
)

MAX_FILES = 8


def create_present_tool(cwd: str) -> AgentTool:
    """Build the ``present`` tool bound to one workspace."""

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": "Usually the 1-2 most important deliverables; at most 8.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Existing file path, relative to the workspace or absolute inside it.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Short description shown on the delivery card.",
                        },
                    },
                    "required": ["path"],
                },
            }
        },
        "required": ["files"],
    }

    async def execute(
        tool_call_id: str,
        args: Any,
        signal: AbortSignal | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        if not isinstance(args, dict):
            return error_result("present requires an object argument with 'files'.")
        files = args.get("files")
        if not isinstance(files, list) or not files or len(files) > MAX_FILES:
            return error_result(f"present requires 1 to {MAX_FILES} files.")
        delivered: list[dict[str, str]] = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                return error_result("each present file needs a string 'path'.")
            raw_path = item["path"].strip()
            if not raw_path:
                return error_result("present file paths cannot be empty.")
            absolute = resolve_path(cwd, raw_path)
            root = os.path.realpath(cwd)
            target = os.path.realpath(absolute)
            if os.path.commonpath([root, target]) != root:
                return error_result(f"present only accepts files inside the workspace: {raw_path}")
            if not os.path.isfile(target):
                return error_result(f"present could not find a regular file: {raw_path}")
            description = item.get("description")
            delivered.append(
                {
                    "path": render_path(target, cwd),
                    "description": description.strip() if isinstance(description, str) else "",
                }
            )
        if signal is not None and signal.aborted:
            return error_result("Operation aborted")
        return text_result(
            "Presented " + ", ".join(item["path"] for item in delivered),
            details={"files": delivered},
        )

    return AgentTool(
        name="present",
        label="present",
        description=DESCRIPTION,
        parameters=parameters,
        execute=execute,
        prompt_snippet="Show finished files as delivery cards",
        prompt_guidelines=[
            "After creating a document, spreadsheet, slide deck, image or other deliverable, call present with the final path(s) and a short description.",
            "Do not present temporary or intermediate files; present only files the user should open.",
        ],
    )


__all__ = ["DESCRIPTION", "MAX_FILES", "create_present_tool"]
