"""Risk, session grants and tool visibility.

The existing suite pins *whether* a call is allowed.  This one pins the second axis:
what a permission is worth remembering for, and which tools are worth offering to the
model at all.  The security property to keep in sight is one line — a grant never
widens, and a hard denial is never grantable.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pi_coding_agent.policy.extension import setup as setup_policy_extension
from pi_coding_agent.policy.rules import (
    Decision,
    PolicyConfig,
    Risk,
    SessionGrants,
    classify_tool_call,
    risk_of,
)

# --------------------------------------------------------------------------------------
# Risk classification
# --------------------------------------------------------------------------------------

RISKS = [
    ("read", {"path": "a.txt"}, Risk.READ),
    ("ls", {"path": "."}, Risk.READ),
    ("grep", {"pattern": "x", "path": "."}, Risk.READ),
    ("write", {"path": "a.txt"}, Risk.WRITE),
    ("edit", {"path": "a.txt"}, Risk.WRITE),
    ("ask_user_question", {}, Risk.READ),
    ("bash", {"command": "ls -la"}, Risk.PROBE),
    ("bash", {"command": "python script.py"}, Risk.PROBE),
    ("bash", {"command": "echo hi > out.txt"}, Risk.WRITE),
    ("bash", {"command": "mkdir build"}, Risk.WRITE),
    ("bash", {"command": "sed -i 's/a/b/' f"}, Risk.WRITE),
    ("bash", {"command": "rm -rf build"}, Risk.DESTRUCTIVE),
    ("bash", {"command": "git reset --hard HEAD~1"}, Risk.DESTRUCTIVE),
    ("bash", {"command": "drop table users"}, Risk.DESTRUCTIVE),
    ("some_plugin_tool", {"x": 1}, Risk.PROBE),
]


@pytest.mark.parametrize(("tool", "args", "expected"), RISKS, ids=[f"{t}:{a}" for t, a, _ in RISKS])
def test_risk_classification(tool: str, args: dict, expected: Risk) -> None:
    assert risk_of(tool, args) is expected


def test_an_unknown_tool_is_never_treated_as_a_read():
    """A plugin tool that only reads would be over-classified; the safe direction."""
    assert risk_of("mystery", {}) is Risk.PROBE


# --------------------------------------------------------------------------------------
# Grants
# --------------------------------------------------------------------------------------


def test_a_write_grant_covers_reads_and_writes_but_not_destruction():
    grants = SessionGrants()
    assert grants.grant(Risk.WRITE) is True

    assert grants.covers(Risk.READ)
    assert grants.covers(Risk.PROBE)
    assert grants.covers(Risk.WRITE)
    assert not grants.covers(Risk.DESTRUCTIVE)


def test_a_read_grant_does_not_cover_a_write():
    grants = SessionGrants()
    grants.grant(Risk.READ)

    assert grants.covers(Risk.READ)
    assert not grants.covers(Risk.WRITE)


def test_destruction_can_never_be_granted():
    grants = SessionGrants()

    assert grants.grant(Risk.DESTRUCTIVE) is False
    assert not grants.covers(Risk.DESTRUCTIVE)


def test_clear_forgets_everything():
    grants = SessionGrants()
    grants.grant(Risk.WRITE)
    grants.clear()

    assert grants.snapshot() == ()
    assert not grants.covers(Risk.READ)


# --------------------------------------------------------------------------------------
# The verdict, with grants applied
# --------------------------------------------------------------------------------------


def policy(**overrides) -> PolicyConfig:
    defaults = dict(workspace_root="/work", allow_tools=frozenset({"read", "write", "bash"}),
                    confirm_tools=frozenset({"bash"}))
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def test_a_confirmed_tool_becomes_allowed_once_the_session_granted_that_risk():
    config = policy(session_grants=SessionGrants())
    # ``chmod`` is a write (it mutates the file) and a confirm (it changes
    # permissions), which is exactly the pair a session grant is meant to collapse.
    command = {"command": "chmod 755 /work/a.sh"}

    assert classify_tool_call(config, "bash", command).decision is Decision.CONFIRM

    config.session_grants.grant(Risk.WRITE)
    verdict = classify_tool_call(config, "bash", command)

    assert verdict.decision is Decision.ALLOW
    assert verdict.reason.startswith("granted for this session")


def test_a_hard_denial_stays_denied_even_with_a_grant():
    """The grant table must never touch DENY."""
    config = policy(session_grants=SessionGrants())
    config.session_grants.grant(Risk.WRITE)

    verdict = classify_tool_call(config, "bash", {"command": "sudo rm -rf /"})

    assert verdict.decision is Decision.DENY


def test_a_tool_outside_the_policy_is_denied_regardless_of_grants():
    config = policy(session_grants=SessionGrants())
    config.session_grants.grant(Risk.WRITE)

    assert classify_tool_call(config, "write", {"path": "/etc/passwd"}).decision is Decision.DENY


# --------------------------------------------------------------------------------------
# What the model is offered
# --------------------------------------------------------------------------------------


def test_only_tools_the_policy_can_allow_are_offered():
    config = PolicyConfig(
        workspace_root="/work",
        allow_tools=frozenset({"read", "write"}),
        confirm_tools=frozenset({"bash"}),
    )

    assert config.offered_tools(["read", "bash", "find", "mystery"]) == ["read", "bash"]


def test_the_default_policy_offers_every_builtin_tool():
    from pi_coding_agent.policy.rules import DEFAULT_ALLOW_TOOLS

    config = PolicyConfig(workspace_root="/work")
    names = sorted(DEFAULT_ALLOW_TOOLS)

    assert config.offered_tools(names) == names


# --------------------------------------------------------------------------------------
# The extension: audit fields and remembering an approval
# --------------------------------------------------------------------------------------


class FakeUI:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.prompts: list[tuple[str, str]] = []

    async def confirm(self, title: str, detail: str = "", timeout: float | None = None) -> bool:
        self.prompts.append((title, detail))
        return self.approved


def gate_for(config: PolicyConfig, *, approved: bool, has_ui: bool = True):
    handlers: dict[str, object] = {}

    class FakePi:
        def on(self, name: str, fn) -> None:  # noqa: ANN001 - the extension API shape
            handlers[name] = fn

    setup_policy_extension(FakePi())
    ui = FakeUI(approved)
    ctx = SimpleNamespace(policy=config, has_ui=has_ui, ui=ui)
    return handlers["tool_call"], ctx, ui


def event_for(tool: str, payload: dict):
    return SimpleNamespace(tool_name=tool, tool_call_id="call-1", input=payload)


def test_an_approved_write_is_remembered_for_the_session():
    records: list[dict] = []
    config = policy(record=records.append, grant_after_confirm=True)
    gate, ctx, _ui = gate_for(config, approved=True)

    asyncio.run(gate(event_for("bash", {"command": "chmod 755 /work/a.sh"}), ctx))

    assert config.session_grants.covers(Risk.WRITE)
    assert records[-1]["risk"] == "write"
    assert records[-1]["granted_by"] == "confirm"
    assert records[-1]["granted_for_session"] is True


def test_a_refused_call_grants_nothing():
    records: list[dict] = []
    config = policy(record=records.append, grant_after_confirm=True)
    gate, ctx, _ui = gate_for(config, approved=False)

    result = asyncio.run(gate(event_for("bash", {"command": "chmod 755 /work/a.sh"}), ctx))

    assert result is not None and result.block is True
    assert not config.session_grants.covers(Risk.WRITE)
    assert records[-1]["granted_by"] == ""


def test_a_destructive_call_is_never_remembered_even_when_approved():
    records: list[dict] = []
    # Approve everything that reaches the dialog, and ask about destructive commands.
    config = policy(record=records.append, grant_after_confirm=True, auto_approve=False)
    gate, ctx, _ui = gate_for(config, approved=True)

    # A destructive command that reaches the dialog (writing to a device) must not be
    # remembered, however it was answered.
    asyncio.run(gate(event_for("bash", {"command": "dd if=/dev/zero of=/dev/sda"}), ctx))

    assert not config.session_grants.covers(Risk.DESTRUCTIVE)
    assert records[-1]["risk"] == "destructive"
    assert records[-1]["granted_by"] == "confirm"
    assert records[-1]["granted_for_session"] is False, "approved once, remembered never"


def test_the_second_write_of_the_session_does_not_ask_again():
    config = policy(grant_after_confirm=True)
    gate, ctx, ui = gate_for(config, approved=True)
    first = event_for("bash", {"command": "chmod 755 /work/a.sh"})
    second = event_for("bash", {"command": "chmod 644 /work/b.sh"})

    asyncio.run(gate(first, ctx))
    assert len(ui.prompts) == 1

    asyncio.run(gate(second, ctx))
    assert len(ui.prompts) == 1, "the session grant answered the second one"
