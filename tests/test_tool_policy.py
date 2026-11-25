"""The permission posture is one switch, and it fails loudly when mistyped.

The deployment ships with ``allow-all`` (the behaviour this project has always had),
but an operator must be able to tighten it from the environment without editing code:
``PI_TOOL_POLICY=confirm`` or ``confirm-session``.  An unrecognised value raises at
startup — silently running with a *different* permission posture than requested is the
one failure this switch must not have.
"""

from __future__ import annotations

import pytest

from pi_server.config import ServerConfig, tool_policy_flags


def test_the_three_modes_map_to_the_documented_flags():
    assert tool_policy_flags("allow-all") == (True, False)
    assert tool_policy_flags("confirm") == (False, False)
    assert tool_policy_flags("confirm-session") == (False, True)


def test_absent_means_allow_all():
    assert tool_policy_flags(None) == (True, False)


def test_the_mode_name_is_case_and_space_tolerant():
    assert tool_policy_flags("  CONFIRM ") == (False, False)


@pytest.mark.parametrize("policy", ["strict", "", "allow_all", "true"])
def test_an_unknown_mode_is_rejected(policy: str) -> None:
    """An empty string is rejected too: ``PI_TOOL_POLICY=`` in a .env is a typo."""
    with pytest.raises(ValueError, match="PI_TOOL_POLICY"):
        tool_policy_flags(policy)


def test_the_old_flags_still_win_over_the_mode():
    """Backwards compatibility: a deployment pinning the old boolean keeps working."""
    assert tool_policy_flags("allow-all", explicit_auto="0") == (False, False)
    assert tool_policy_flags("confirm", explicit_auto="1") == (True, False)
    assert tool_policy_flags("confirm", explicit_grant="1") == (False, True)


def test_from_env_applies_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_TOOL_POLICY", "confirm-session")
    monkeypatch.delenv("PI_AUTO_APPROVE_TOOLS", raising=False)
    monkeypatch.delenv("PI_POLICY_SESSION_GRANTS", raising=False)

    config = ServerConfig.from_env(data_root="/tmp/pi-policy-test")

    assert config.tool_policy == "confirm-session"
    assert config.auto_approve_tools is False
    assert config.grant_after_confirm is True


def test_from_env_defaults_to_allow_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PI_TOOL_POLICY", "PI_AUTO_APPROVE_TOOLS", "PI_POLICY_SESSION_GRANTS"):
        monkeypatch.delenv(name, raising=False)

    config = ServerConfig.from_env(data_root="/tmp/pi-policy-test")

    assert config.auto_approve_tools is True
    assert config.grant_after_confirm is False


def test_from_env_rejects_a_typo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_TOOL_POLICY", "allow_everything")

    with pytest.raises(ValueError, match="PI_TOOL_POLICY"):
        ServerConfig.from_env(data_root="/tmp/pi-policy-test")
