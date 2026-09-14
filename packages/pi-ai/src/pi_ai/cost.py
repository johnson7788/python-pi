"""Cost accounting — a byte-for-byte port of the TypeScript ``calculateCost``.

Pricing is expressed in USD per **million** tokens.  The rate dataclasses live in
:mod:`pi_ai.types` because they are part of the ``Model`` descriptor.
"""

from __future__ import annotations

from .types import Model, ModelCost, ModelCostRates, ModelCostTier, Usage


def calculate_cost(model: Model, usage: Usage) -> None:
    """Populate ``usage.cost`` in place, exactly like the TypeScript version.

    The tier is selected on ``input + cacheRead + cacheWrite`` using a strict
    ``>`` comparison, and the highest matching threshold wins.  Anthropic bills
    1-hour cache writes at twice the base input rate.
    """

    input_tokens = usage.input + usage.cache_read + usage.cache_write

    rates: ModelCostRates = model.cost
    matched_threshold = -1
    for tier in model.cost.tiers:
        if input_tokens > tier.input_tokens_above and tier.input_tokens_above > matched_threshold:
            rates = tier
            matched_threshold = tier.input_tokens_above

    long_write = usage.cache_write_1h or 0
    short_write = usage.cache_write - long_write

    usage.cost.input = (rates.input / 1_000_000) * usage.input
    usage.cost.output = (rates.output / 1_000_000) * usage.output
    usage.cost.cache_read = (rates.cache_read / 1_000_000) * usage.cache_read
    usage.cost.cache_write = (rates.cache_write * short_write + rates.input * 2 * long_write) / 1_000_000
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cache_read + usage.cost.cache_write


__all__ = ["ModelCost", "ModelCostRates", "ModelCostTier", "calculate_cost"]
