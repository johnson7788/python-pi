"""Learn how the provider counts tokens, from the provider's own arithmetic.

Why not a tokenizer
-------------------
``compaction`` estimates with ``chars / 4``.  That is calibrated for English; for
Chinese, cl100k counts roughly one token per character while Qwen and DeepSeek spend
about 0.6.  Shipping a real tokenizer would fix one family and be wrong for the next,
and would add a multi-megabyte dependency to a process that only needs an estimate
good enough to decide *when* to compact.

The measurement available for free
----------------------------------
Two consecutive requests share their system prompt, their tool schemas and all
earlier messages.  So the growth of the provider's prompt-token count between them
is the cost of exactly the messages appended in between — the previous reply, the
tool results, any injected text.  Dividing that by the estimate of those same
messages prices conversation alone, without also measuring how the provider renders
tool schemas.

Deliberately narrow
-------------------
One scalar, bounded, learned per session.  A wrong scale only makes compaction run
early or late; it never changes what the model sees.  Samples that cannot mean what
the ratio assumes are dropped rather than blended in:

* an estimate below :data:`MIN_SAMPLE_TOKENS` (mostly per-message overhead);
* a ratio outside ``[0.3, 3.0]`` (the provider counted only uncached input, or the
  prefix changed underneath us);
* any sample containing an image (billed by tile, not by text).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Where the learned scale is clamped.  ``chars/4`` is never off by more than this for
#: a text conversation; a ratio outside it says something about the *provider's* usage
#: report, not about its tokenizer.
MIN_SCALE = 0.5
MAX_SCALE = 1.5

#: A sample smaller than this is dominated by per-message overhead and says little.
MIN_SAMPLE_TOKENS = 256

#: A single sample outside this band is discarded instead of pulling the average.
MIN_SAMPLE_RATIO = 0.3
MAX_SAMPLE_RATIO = 3.0


@dataclass
class TokenScale:
    """A bounded, running estimate of ``provider tokens / estimated tokens``."""

    value: float = 1.0
    #: Sums over accepted samples, not a mean of means: a long conversation should
    #: weigh more than a two-message exchange.
    _provider: float = 0.0
    _estimated: float = 0.0
    accepted: int = 0
    rejected: int = 0

    def observe(
        self, provider_delta: int, estimated_delta: int, *, has_image: bool = False
    ) -> bool:
        """Record one conversation increment; ``True`` when it moved the scale.

        ``estimated_delta`` must be the **unscaled** estimate, or the feedback loop
        would chase its own output.
        """
        if has_image or estimated_delta < MIN_SAMPLE_TOKENS or provider_delta <= 0:
            self.rejected += 1
            return False
        ratio = provider_delta / estimated_delta
        if not (MIN_SAMPLE_RATIO <= ratio <= MAX_SAMPLE_RATIO):
            self.rejected += 1
            return False

        self._provider += provider_delta
        self._estimated += estimated_delta
        self.accepted += 1
        if self._estimated < MIN_SAMPLE_TOKENS:
            return False
        self.value = min(MAX_SCALE, max(MIN_SCALE, self._provider / self._estimated))
        return True

    def reset(self) -> None:
        self.value = 1.0
        self._provider = 0.0
        self._estimated = 0.0
        self.accepted = 0
        self.rejected = 0

