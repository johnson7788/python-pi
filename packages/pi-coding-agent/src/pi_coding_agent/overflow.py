"""Recognise a provider's "the prompt does not fit" error.

Why a dedicated classifier
--------------------------
Context overflow reaches us as an ordinary 400/413 error string, and the recovery
is specific: compact once and retry.  Treated as a generic failure the turn ends;
treated as transient the same oversized request is retried until the attempts run
out.  So the message has to be classified before either of those happen.

The list is compiled from provider documentation and observed responses, not
guessed: every entry names the provider it came from.  Matching is deliberately
loose (case-insensitive, substring) because providers reword the same condition
between versions, and a false negative costs a dead turn while a false positive
costs one useless compaction.
"""

from __future__ import annotations

import re
from typing import Any

#: ``(pattern, provider)`` pairs.  The provider label is documentation, not
#: behaviour: no caller branches on it, but a future reader needs to know where a
#: pattern came from and whether it can be deleted.
_OVERFLOW_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"prompt is too long", re.I), "Anthropic"),
    (re.compile(r"request_too_large", re.I), "Anthropic (HTTP 413)"),
    (re.compile(r"input is too long for requested model", re.I), "Amazon Bedrock"),
    (re.compile(r"exceeds the context window", re.I), "OpenAI"),
    (re.compile(r"exceeds (the )?(model'?s )?maximum context length", re.I), "OpenAI-compatible"),
    (re.compile(r"input token count.*exceeds the maximum", re.I), "Gemini"),
    (re.compile(r"maximum prompt length is \d+", re.I), "xAI Grok"),
    (re.compile(r"reduce the length of the messages", re.I), "Groq"),
    (re.compile(r"maximum context length is \d+ tokens", re.I), "OpenRouter"),
    (re.compile(r"exceeds (the )?maximum allowed input length", re.I), "OpenRouter / Poolside"),
    (re.compile(r"is longer than the model'?s context length", re.I), "Together AI"),
    (re.compile(r"exceeds the limit of \d+", re.I), "GitHub Copilot"),
    (re.compile(r"exceeds the available context size", re.I), "llama.cpp"),
    (re.compile(r"greater than the context length", re.I), "LM Studio"),
    (re.compile(r"context window exceeds limit", re.I), "MiniMax"),
    (re.compile(r"exceeded model token limit", re.I), "Kimi"),
    (re.compile(r"too large for model with \d+ maximum context length", re.I), "Mistral"),
    (re.compile(r"range of input length should be", re.I), "DashScope / Qwen"),
    (re.compile(r"context[_ ]length[_ ]exceeded", re.I), "generic"),
    (re.compile(r"too many tokens", re.I), "generic"),
    (re.compile(r"token limit exceeded", re.I), "generic"),
    (re.compile(r"prompt too long", re.I), "Ollama / generic"),
)


def classify_overflow(text: str | None) -> str | None:
    """The provider label when ``text`` describes an overflow, else ``None``."""
    if not text:
        return None
    for pattern, provider in _OVERFLOW_PATTERNS:
        if pattern.search(text):
            return provider
    return None


def is_overflow_error(message: Any) -> bool:
    """Whether an assistant message carries a context-overflow error.

    Only ``stop_reason == "error"`` qualifies: a successful reply can legitimately
    contain the phrase (for example a transcript quoting a provider message).
    """
    if getattr(message, "stop_reason", None) != "error":
        return False
    return classify_overflow(str(getattr(message, "error_message", "") or "")) is not None
