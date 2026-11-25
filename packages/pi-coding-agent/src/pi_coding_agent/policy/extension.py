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
    GRANT_REASON_PREFIX,
    Decision,
    PolicyConfig,
    Risk,
    classify_tool_call,
    risk_of,
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
        risk = risk_of(event.tool_name, event.input)

        if verdict.decision is Decision.DENY:
            _record(config, event, "blocked", verdict.reason, summary, risk=risk)
            return ToolCallEventResult(block=True, reason=verdict.reason)

        if verdict.decision is Decision.ALLOW:
            granted_by = (
                "session" if verdict.reason.startswith(GRANT_REASON_PREFIX) else "policy"
            )
            _record(
                config, event, "allow", verdict.reason, summary,
                risk=risk, granted_by=granted_by, granted_for_session=granted_by == "session",
            )
            return None

        # CONFIRM.  A deployment can explicitly opt into auto-approval (the default
        # for the interactive workbench); hard denies above still apply.
        if config.auto_approve:
            _record(
                config, event, "approved", f"{verdict.reason} (auto-approved)", summary,
                risk=risk, granted_by="auto",
            )
            return None

        # No UI, a refusal and a timeout all mean "no": a shared deployment must
        # fail closed when nobody is there to answer, and an unanswered dialog would
        # otherwise leave the turn hanging forever.
        approved = False
        if ctx.has_ui:
            detail = f"{verdict.reason}\n\n{summary}" if summary else verdict.reason
            approved = await ctx.ui.confirm(
                f"允许执行 {event.tool_name}？", detail, timeout=config.confirm_timeout
            )
        remembered = False
        if approved and config.grant_after_confirm:
            # Only writes (and not destructive ones) may be remembered; ``grant``
            # refuses the rest.  The dialog copy promises this, which is why it is
            # opt-in rather than a surprising default.
            remembered = config.session_grants.grant(risk)
        _record(
            config, event, "approved" if approved else "denied", verdict.reason, summary,
            risk=risk, granted_by="confirm" if approved else "", granted_for_session=remembered,
        )
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


def _record(
    config: PolicyConfig,
    event: Any,
    decision: str,
    reason: str,
    summary: str,
    *,
    risk: Risk | None = None,
    granted_by: str = "",
    granted_for_session: bool = False,
) -> None:
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
                # What the call could have changed, and who allowed it.  Without
                # these two, an audit row cannot answer "did anyone approve this?".
                "risk": risk.value if risk is not None else "",
                "granted_by": granted_by,
                # Whether this approval was remembered for the rest of the session,
                # as opposed to answering one dialog.
                "granted_for_session": granted_for_session,
            }
        )
    except Exception:  # noqa: BLE001 - auditing must never break the agent loop
        pass
