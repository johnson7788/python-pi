"""The team policy extension: turns a :class:`PolicyConfig` verdict into an agent-loop effect.

Loaded as a *built-in* extension by a deployment that also injects a ``PolicyConfig``
into the session's extension host.  With no policy present it does nothing at all, so
loading it in a personal install is harmless.

Imports are absolute on purpose.  A plain ``.py`` extension is imported under a synthetic
module name without a package search path, so ``from .rules import ...`` would fail; the
absolute import also guarantees the extension and the server share one ``PolicyConfig``
class object, which is what makes the ``isinstance`` guard meaningful.
"""

from __future__ import annotations

from typing import Any

from pi_coding_agent.extensions.types import ToolCallEventResult
from pi_coding_agent.policy.rules import (
    Decision,
    PolicyConfig,
    classify_tool_call,
    summarize_call,
)


def setup(pi: Any) -> None:
    """Register the tool-call gate."""

    async def gate(event: Any, ctx: Any) -> Any:
        config = getattr(ctx, "policy", None)
        if not isinstance(config, PolicyConfig):
            return None

        summary = summarize_call(event.tool_name, event.input)
        verdict = classify_tool_call(config, event.tool_name, event.input)

        if verdict.decision is Decision.DENY:
            _record(config, event, "blocked", verdict.reason, summary)
            return ToolCallEventResult(block=True, reason=verdict.reason)

        if verdict.decision is Decision.ALLOW:
            _record(config, event, "allow", "", summary)
            return None

        # CONFIRM.  No UI, a refusal and a timeout all mean "no": a shared deployment must
        # fail closed when nobody is there to answer, and an unanswered dialog would
        # otherwise leave the turn hanging forever.
        approved = False
        if ctx.has_ui:
            detail = f"{verdict.reason}\n\n{summary}" if summary else verdict.reason
            approved = await ctx.ui.confirm(
                f"允许执行 {event.tool_name}？", detail, timeout=config.confirm_timeout
            )
        _record(config, event, "approved" if approved else "denied", verdict.reason, summary)
        if approved:
            return None
        return ToolCallEventResult(
            block=True,
            reason=(
                f"{verdict.reason or 'this call needs approval'}; "
                "the operator did not approve it"
            ),
        )

    pi.on("tool_call", gate)


def _record(config: PolicyConfig, event: Any, decision: str, reason: str, summary: str) -> None:
    """Hand one decision to the deployment's audit sink, which is optional."""
    if config.record is None:
        return
    try:
        config.record(
            {
                "tool_name": getattr(event, "tool_name", ""),
                "tool_call_id": getattr(event, "tool_call_id", ""),
                "decision": decision,
                "reason": reason,
                "input_preview": summary,
            }
        )
    except Exception:  # noqa: BLE001 - auditing must never break the agent loop
        pass
