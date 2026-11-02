"""Model-facing delegation tools: ``subagent`` (spawn) and ``subagent_fork`` (fork).

Both tools admit a child and return immediately — the schema deliberately has no
"wait" switch, because a tool call that blocks on a slow child would serialize the
parent and make parallel delegation pointless.  Results arrive as a ``custom``
notice in the parent transcript; the caller keeps working in the meantime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pi_agent import AgentTool, AgentToolResult
from pi_ai import TextContent

from .types import SubagentError, SubagentProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..agent_session import AgentSession
    from .manager import SubagentManager

_PROMPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": (
                "The task for the subagent. Include every fact it needs: it cannot "
                "see the parent conversation unless this tool says so."
            ),
        },
        "label": {
            "type": "string",
            "description": "Short label for the child (shown in the notice and used as its session name).",
        },
    },
    "required": ["prompt"],
}

_SPAWN_DESCRIPTION = (
    "Delegate a self-contained task to a background subagent and continue working. "
    "The child runs in its own new session with the same tools and working directory, "
    "but it starts with an empty conversation, so the prompt must stand alone. "
    "Returns immediately with a child id; the child's result arrives later as a "
    "[subagent spawn] notice in this conversation. Use several of these for "
    "independent, parallelizable work."
)

_FORK_DESCRIPTION = (
    "Delegate a task to a background subagent that inherits this conversation's "
    "completed turns (the in-progress turn is not included). Use it for follow-up "
    "work that needs the existing context — a review, a deeper analysis, or a "
    "continuation — without restating it. Returns immediately; the result arrives "
    "later as a [subagent fork] notice. The seed is a snapshot: later parent turns "
    "never reach the child."
)


def create_subagent_tools(
    manager: "SubagentManager", session: "AgentSession", depth: int
) -> list[AgentTool]:
    """Build both delegation tools bound to one calling session."""
    return [
        _make_tool("spawn", manager, session, depth),
        _make_tool("fork", manager, session, depth),
    ]


def _make_tool(
    provider: SubagentProvider,
    manager: "SubagentManager",
    session: "AgentSession",
    depth: int,
) -> AgentTool:
    spawn = provider == "spawn"
    name = "subagent" if spawn else "subagent_fork"

    async def execute(
        tool_call_id: str,
        params: Any,
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        if not isinstance(params, dict):
            raise SubagentError("SUBAGENT_INVALID_ARGUMENTS", "arguments must be an object")
        record = await manager.start(
            provider,
            session,
            depth,
            params.get("prompt") or "",
            params.get("label"),
        )
        return AgentToolResult(
            content=[
                TextContent(
                    text=(
                        f"started subagent {record.child_id} ({provider}, label {record.label!r}). "
                        "It runs in the background; its result will arrive as a notice in this "
                        "conversation. Continue with other work — do not wait for it."
                    )
                )
            ],
            details={
                "childId": record.child_id,
                "provider": provider,
                "label": record.label,
                "depth": record.depth,
                "sessionFile": record.session_manager.get_session_file()
                if record.session_manager is not None
                else None,
            },
        )

    return AgentTool(
        name=name,
        description=_SPAWN_DESCRIPTION if spawn else _FORK_DESCRIPTION,
        parameters=_PROMPT_SCHEMA,
        execute=execute,
        label="Subagent" if spawn else "Subagent (fork)",
        prompt_snippet=(
            "Delegate a self-contained task to a background subagent"
            if spawn
            else "Delegate a task to a subagent that inherits this conversation"
        ),
        prompt_guidelines=[
            (
                "Use subagent for independent, parallelizable work: it returns immediately "
                "and reports its result later as a notice"
                if spawn
                else "Use subagent_fork when a subtask needs this conversation's completed turns"
            )
        ],
    )


__all__ = ["create_subagent_tools"]
