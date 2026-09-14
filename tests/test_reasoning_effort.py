"""Regression tests for ``reasoning_effort`` resolution.

Sending the literal string ``"off"`` is a real-world failure, not a theoretical one:
DashScope (Alibaba Bailian) answered a live request with

    'reasoning_effort' must be one of: 'low', 'medium', 'high', 'xhigh', 'max'

Upstream omits the field entirely for ``"off"``, and the model's
``thinkingLevelMap`` is the only way to turn it into a provider-specific value.
"""

from __future__ import annotations

import pytest
from pi_ai import Model, ModelCost, StreamOptions
from pi_ai.providers.openai_completions import _resolve_reasoning_effort


def _model(*, reasoning: bool = True, level_map: dict | None = None) -> Model:
    return Model(
        id="test-model",
        name="Test",
        api="openai-completions",
        provider="test",
        base_url="http://localhost",
        reasoning=reasoning,
        input=["text"],
        cost=ModelCost(),
        context_window=1000,
        max_tokens=100,
        thinking_level_map=level_map,
    )


def _options(level: str | None) -> StreamOptions:
    return StreamOptions(reasoning=level)


@pytest.mark.parametrize("level", [None, "off"])
def test_off_and_none_omit_the_field(level):
    """``off`` must not be sent as a value — that is what broke against DashScope."""
    assert _resolve_reasoning_effort(_model(), _options(level), {}) is None


def test_unmapped_level_is_passed_through():
    assert _resolve_reasoning_effort(_model(), _options("high"), {}) == "high"


def test_thinking_level_map_wins():
    model = _model(level_map={"low": "minimal", "high": "high", "off": None})
    assert _resolve_reasoning_effort(model, _options("low"), {}) == "minimal"
    assert _resolve_reasoning_effort(model, _options("high"), {}) == "high"


def test_explicit_off_mapping_is_honoured():
    """The escape hatch: a provider that spells "off" as its own value."""
    model = _model(level_map={"off": "none"})
    assert _resolve_reasoning_effort(model, _options("off"), {}) == "none"


def test_level_mapped_to_null_is_omitted():
    model = _model(level_map={"medium": None})
    assert _resolve_reasoning_effort(model, _options("medium"), {}) is None


def test_compat_can_disable_the_field_entirely():
    assert _resolve_reasoning_effort(_model(), _options("high"), {"supports_reasoning_effort": False}) is None
    # ...but an explicit "off" mapping is still an explicit instruction.
    model = _model(level_map={"off": "none"})
    assert _resolve_reasoning_effort(model, _options("off"), {"supports_reasoning_effort": False}) == "none"
