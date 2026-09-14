"""The stream function boundary.

The agent core never talks to a provider directly: it calls a ``StreamFn``
``(model, context, options) -> EventStream``.  This indirection is what lets the
test suite drive the loop with a deterministic faux provider.
"""

from __future__ import annotations

from pi_ai import Context, EventStream, Model, StreamOptions, stream_simple

from .types import StreamFn


def default_stream_fn(model: Model, context: Context, options: StreamOptions) -> EventStream:
    """Default transport: the ``streamSimple`` path, which clamps thinking levels."""
    return stream_simple(model, context, options)


def get_default_stream_fn() -> StreamFn:
    return default_stream_fn


__all__ = ["default_stream_fn", "get_default_stream_fn"]
