"""Tests for the CLI entry point.

These cover argument handling, project-trust resolution and model selection
without starting a session, so they stay fast and never touch the network.
"""

from __future__ import annotations

import json
import os

import pytest
from pi_ai import AuthStorage, InMemoryCredentialStore, ModelRegistry
from pi_ai.auth import ApiKeyCredential
from pi_coding_agent.cli import (
    EXIT_OK,
    EXIT_USAGE,
    _resolve_trust,
    _select_model,
    build_parser,
    run,
)
from pi_coding_agent.settings import SettingsManager


def test_parser_defaults_to_print_mode():
    args = build_parser().parse_args(["hello"])
    assert args.prompt == ["hello"]
    assert args.mode == "print"
    assert args.no_session is False
    assert args.resume is None


def test_parser_accepts_multiple_prompt_words_and_json_mode():
    args = build_parser().parse_args(["explain", "this", "--mode", "json"])
    assert args.prompt == ["explain", "this"]
    assert args.mode == "json"


def test_parser_rejects_unknown_mode():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mode", "tui", "hi"])


async def test_list_models_lists_the_catalog(tmp_path, capsys):
    code = await run(["--agent-dir", str(tmp_path / "agent"), "--list-models"])
    assert code == EXIT_OK

    out = capsys.readouterr().out
    assert "anthropic/claude-sonnet-4-5" in out
    # Nothing has credentials, so no line is starred.
    assert all(not line.startswith("*") for line in out.splitlines())


async def test_missing_prompt_is_a_usage_error(tmp_path):
    code = await run(["--agent-dir", str(tmp_path / "agent")])
    assert code == EXIT_USAGE


def test_resolve_trust_uses_a_stored_decision(tmp_path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    canonical = os.path.realpath(str(project))
    (agent_dir / "trust.json").write_text(json.dumps({canonical: True}), encoding="utf-8")

    settings = SettingsManager(str(project), str(agent_dir), project_trusted=False)
    assert _resolve_trust(str(project), str(agent_dir), settings) is True


def test_resolve_trust_honours_default_always(tmp_path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "settings.json").write_text(
        json.dumps({"defaultProjectTrust": "always"}), encoding="utf-8"
    )
    project = tmp_path / "project"
    project.mkdir()

    settings = SettingsManager(str(project), str(agent_dir), project_trusted=False)
    assert _resolve_trust(str(project), str(agent_dir), settings) is True


def test_resolve_trust_fails_closed_on_ask(tmp_path, capsys):
    """Without a UI, ``ask`` must decline rather than silently trust."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    settings = SettingsManager(str(project), str(agent_dir), project_trusted=False)
    assert _resolve_trust(str(project), str(agent_dir), settings) is False
    assert "untrusted" in capsys.readouterr().err


def test_select_model_prefers_configured_credentials(tmp_path):
    store = InMemoryCredentialStore()
    auth = AuthStorage(store)
    store.set("anthropic", ApiKeyCredential(key="sk-test"))
    registry = ModelRegistry(auth)

    settings = SettingsManager(str(tmp_path), str(tmp_path / "agent"), project_trusted=False)
    args = build_parser().parse_args(["hi"])
    resolved = _select_model(registry, args, settings)
    assert resolved.model.provider == "anthropic"


def test_select_model_errors_when_nothing_matches(tmp_path):
    registry = ModelRegistry(AuthStorage(InMemoryCredentialStore()))
    settings = SettingsManager(str(tmp_path), str(tmp_path / "agent"), project_trusted=False)
    args = build_parser().parse_args(["hi", "--model", "nope/nothing"])

    with pytest.raises(SystemExit) as excinfo:
        _select_model(registry, args, settings)
    assert "no model matched" in str(excinfo.value)


def test_select_model_errors_without_any_credentials(tmp_path):
    registry = ModelRegistry(AuthStorage(InMemoryCredentialStore()))
    settings = SettingsManager(str(tmp_path), str(tmp_path / "agent"), project_trusted=False)
    args = build_parser().parse_args(["hi"])

    with pytest.raises(SystemExit) as excinfo:
        _select_model(registry, args, settings)
    assert "no model with configured credentials" in str(excinfo.value)


def test_select_model_prefixes_bare_id_with_provider(tmp_path):
    registry = ModelRegistry(AuthStorage(InMemoryCredentialStore()))
    settings = SettingsManager(str(tmp_path), str(tmp_path / "agent"), project_trusted=False)
    args = build_parser().parse_args(["hi", "--provider", "anthropic", "--model", "claude-sonnet-4-5"])

    resolved = _select_model(registry, args, settings)
    assert resolved.model.provider == "anthropic"
    assert resolved.model.id == "claude-sonnet-4-5"
