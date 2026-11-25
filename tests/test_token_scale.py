"""Learning the provider's token unit from consecutive requests.

Two layers: the pure accumulator (bounds, sample filters) and the session wiring
(the increment is measured from the previous assistant message, so a shrunken
history resets the baseline instead of producing a nonsense sample).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pi_ai import (
    AssistantMessage,
    AuthStorage,
    Cost,
    InMemoryCredentialStore,
    Model,
    ModelCost,
    TextContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)

from pi_coding_agent.agent_session import AgentSession
from pi_coding_agent.compaction import estimate_tokens
from pi_coding_agent.session_manager import SessionManager
from pi_coding_agent.token_scale import MAX_SCALE, MIN_SCALE, TokenScale

# --------------------------------------------------------------------------------------
# The accumulator
# --------------------------------------------------------------------------------------


def test_enough_good_samples_move_the_scale():
    scale = TokenScale()

    # Chinese text: about 0.6 provider tokens per chars/4 estimate.
    assert scale.observe(600, 1000) is True

    assert scale.value == pytest.approx(0.6)
    assert scale.accepted == 1


def test_a_sample_below_the_threshold_is_rejected():
    """A two-message increment is mostly per-message overhead, not text."""
    scale = TokenScale()

    assert scale.observe(120, 200) is False
    assert scale.observe(60, 100) is False
    assert scale.value == 1.0
    assert scale.accepted == 0


def test_an_implausible_ratio_is_discarded():
    scale = TokenScale()

    assert scale.observe(100_000, 1000) is False  # 100x
    assert scale.observe(1, 1000) is False  # 0.001x
    assert scale.value == 1.0
    assert scale.rejected == 2


def test_a_sample_with_an_image_is_skipped():
    scale = TokenScale()

    assert scale.observe(600, 1000, has_image=True) is False
    assert scale.value == 1.0


def test_the_scale_is_clamped_at_both_ends():
    low = TokenScale()
    low.observe(310, 1000)  # ratio 0.31, inside the sample band
    assert low.value == MIN_SCALE

    high = TokenScale()
    high.observe(2900, 1000)  # ratio 2.9, inside the sample band
    assert high.value == MAX_SCALE


def test_accumulating_samples_is_a_weighted_ratio_not_a_mean_of_ratios():
    """A long conversation must weigh more than a short one."""
    scale = TokenScale()
    scale.observe(800, 1000)  # 0.8
    scale.observe(2100, 3000)  # 0.7, three times as much text

    assert scale.value == pytest.approx(2900 / 4000)
    assert scale.value != pytest.approx((0.8 + 0.7) / 2)


def test_reset_returns_to_one():
    scale = TokenScale()
    scale.observe(600, 1000)
    scale.reset()

    assert scale.value == 1.0
    assert scale.accepted == 0


# --------------------------------------------------------------------------------------
# Compaction honours the scale
# --------------------------------------------------------------------------------------


def test_the_estimate_is_scaled_but_the_raw_value_is_not():
    message = UserMessage(content=[TextContent(text="字" * 400)], timestamp=1)
    raw = estimate_tokens(message)

    assert estimate_tokens(message, 0.6) == pytest.approx(raw * 0.6, abs=1)
    assert estimate_tokens(message, 1.0) == raw


# --------------------------------------------------------------------------------------
# The session wiring
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)


def create_model() -> Model:
    return Model(
        id="test-model",
        name="Test Model",
        api="faux",
        provider="test",
        base_url="http://localhost",
        reasoning=False,
        input=["text"],
        cost=ModelCost(),
        context_window=100_000,
        max_tokens=4096,
    )


def assistant(text: str, prompt_tokens: int) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="faux",
        provider="test",
        model="test-model",
        usage=Usage(input=prompt_tokens, cost=Cost()),
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
    )


def make_session(tmp_path: Path) -> AgentSession:
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    manager = SessionManager.create(str(cwd), session_dir=str(tmp_path / "sessions"))
    storage = AuthStorage(InMemoryCredentialStore(), env={})
    storage.set_api_key("test", "test-key")
    return AgentSession(
        create_model(), str(cwd), manager, auth_storage=storage, tools=[]
    )


def test_the_session_learns_from_the_prompt_growth_between_two_replies(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    messages = session.agent.state.messages

    messages.append(UserMessage(content=[TextContent(text="写一份报告")], timestamp=1))
    first = assistant("先看看资料", prompt_tokens=1000)
    messages.append(first)
    session._observe_token_scale(first)
    assert session.token_scale.value == 1.0, "one sample cannot move the scale yet"

    # The tool result and the first reply are what the second request carries extra.
    tool_result = ToolResultMessage(
        tool_call_id="c1",
        tool_name="read",
        content=[TextContent(text="资料正文" * 300)],
        is_error=False,
        timestamp=2,
    )
    messages.append(tool_result)
    second = assistant("报告如下", prompt_tokens=1360)
    messages.append(second)

    session._observe_token_scale(second)

    appended_estimate = estimate_tokens(first) + estimate_tokens(tool_result)
    assert appended_estimate >= 256, "the fixture must be big enough to be a sample"
    assert session.token_scale.value == pytest.approx(360 / appended_estimate)


def test_a_shrunk_history_resets_the_baseline_instead_of_sampling_it(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    messages = session.agent.state.messages

    messages.append(UserMessage(content=[TextContent(text="hi")], timestamp=1))
    first = assistant("a", prompt_tokens=5000)
    messages.append(first)
    session._observe_token_scale(first)

    # Compaction replaced the transcript: a shorter list, and a smaller prompt.
    session.agent.set_messages([UserMessage(content=[TextContent(text="summary")], timestamp=2)])
    after = assistant("b", prompt_tokens=100)
    session.agent.state.messages.append(after)

    session._observe_token_scale(after)

    assert session.token_scale.value == 1.0
    assert session.token_scale.accepted == 0
