"""Provider adapter tests.

Every streaming test feeds canned SSE bytes through the real adapter parsing
code by injecting an ``httpx.AsyncClient`` backed by ``httpx.MockTransport``.
Nothing here touches the network, and assertions target observable protocol
behaviour: event order, block assembly, usage/cost mapping, and the two opposite
error contracts (``stream`` never raises, ``stream_simple`` may).

A note on event iteration: the terminal ``done``/``error`` event *is* delivered
to an ``async for`` consumer in this port, and is also what ``result()``
resolves to.  Tests assert on the iterator when they care about ordering and on
``result()`` when they care about the final message.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import pi_ai
from pi_ai import (
    AbortController,
    AssistantMessage,
    Context,
    Cost,
    ImageContent,
    Model,
    ModelCost,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from pi_ai.providers.anthropic_messages import AnthropicMessagesProvider
from pi_ai.providers.faux import FauxProvider, FauxTurn, register_faux_api
from pi_ai.providers.openai_completions import OpenAiCompletionsProvider
from pi_ai.transform import (
    NO_RESULT_PLACEHOLDER,
    NON_VISION_TOOL_IMAGE_PLACEHOLDER,
    NON_VISION_USER_IMAGE_PLACEHOLDER,
    transform_messages,
)

SSE_HEADERS = {"content-type": "text/event-stream"}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def make_model(**overrides: Any) -> Model:
    defaults: dict[str, Any] = {
        "id": "test-model",
        "name": "Test Model",
        "api": "anthropic-messages",
        "provider": "test-provider",
        "base_url": "https://api.test",
        "reasoning": False,
        "input": ["text", "image"],
        "cost": ModelCost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75),
        "context_window": 200_000,
        "max_tokens": 8192,
    }
    defaults.update(overrides)
    return Model(**defaults)


def client_factory(handler: Any) -> Any:
    def factory(model: Model, options: StreamOptions) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory


def anthropic_sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode()


def openai_sse(chunks: list[dict[str, Any]], done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


def sse_response(handler_events: bytes) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=handler_events, headers=SSE_HEADERS)

    return handler


async def collect(stream: Any) -> list[Any]:
    return [event async for event in stream]


def text_events(events: list[Any]) -> list[str]:
    return [event.type for event in events]


def assistant_user_context(text: str = "hi") -> Context:
    return Context(messages=[UserMessage(content=text, timestamp=1)])


# --------------------------------------------------------------------------------------
# transform_messages
# --------------------------------------------------------------------------------------


def test_transform_messages_synthesizes_orphan_tool_results() -> None:
    model = make_model()
    assistant = AssistantMessage(
        content=[ToolCall(id="call_1", name="read", arguments={"path": "a"})],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(cost=Cost()),
        stop_reason="toolUse",
        timestamp=2,
    )

    result = transform_messages([UserMessage(content="hi", timestamp=1), assistant], model)

    assert len(result) == 3
    synthetic = result[-1]
    assert isinstance(synthetic, ToolResultMessage)
    assert synthetic.tool_call_id == "call_1"
    assert synthetic.tool_name == "read"
    assert synthetic.is_error is True
    assert synthetic.content == [TextContent(text=NO_RESULT_PLACEHOLDER)]


def test_transform_messages_keeps_paired_tool_results() -> None:
    model = make_model()
    assistant = AssistantMessage(
        content=[ToolCall(id="call_1", name="read", arguments={})],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(cost=Cost()),
        stop_reason="toolUse",
        timestamp=2,
    )
    result_message = ToolResultMessage(
        tool_call_id="call_1",
        tool_name="read",
        content=[TextContent(text="contents")],
        is_error=False,
        timestamp=3,
    )

    result = transform_messages([assistant, result_message], model)

    assert result[-1] is result_message
    assert not any(
        isinstance(message, ToolResultMessage) and message.is_error for message in result
    )


def test_transform_messages_drops_dangling_tool_results() -> None:
    model = make_model()
    dangling = ToolResultMessage(
        tool_call_id="ghost",
        tool_name="read",
        content=[TextContent(text="orphan")],
        is_error=False,
        timestamp=1,
    )

    assert transform_messages([dangling], model) == []


def test_transform_messages_drops_foreign_thinking_and_keeps_own() -> None:
    model = make_model()
    foreign = AssistantMessage(
        content=[
            ThinkingContent(thinking="secret plan", thinking_signature="foreign-sig"),
            ToolCall(id="call_1", name="read", arguments={}, thought_signature="thought"),
        ],
        api="openai-completions",
        provider="other-provider",
        model="other-model",
        usage=Usage(cost=Cost()),
        stop_reason="toolUse",
        timestamp=1,
    )
    own = AssistantMessage(
        content=[ThinkingContent(thinking="mine", thinking_signature="own-sig")],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(cost=Cost()),
        stop_reason="stop",
        timestamp=2,
    )

    result = transform_messages([foreign, own], model)

    # Cross-model thinking has no portable signature, so its text is salvaged.
    assert result[0].content[0] == TextContent(text="secret plan")
    # The thought signature is Google-specific replay data: stripped cross-model.
    assert result[0].content[1].thought_signature is None
    # The orphaned tool call is answered between the two assistant turns.
    assert isinstance(result[1], ToolResultMessage)
    # Same-model thinking is replayed verbatim for continuity.
    assert result[2].content[0] == ThinkingContent(thinking="mine", thinking_signature="own-sig")


def test_transform_messages_drops_empty_unsigned_thinking() -> None:
    model = make_model()
    assistant = AssistantMessage(
        content=[ThinkingContent(thinking="   "), TextContent(text="answer")],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(cost=Cost()),
        stop_reason="stop",
        timestamp=1,
    )

    result = transform_messages([assistant], model)

    assert result[0].content == [TextContent(text="answer")]


def test_transform_messages_downgrades_images_for_text_only_models() -> None:
    model = make_model(input=["text"])
    image = ImageContent(data="AAAA", mime_type="image/png")
    user = UserMessage(
        content=[image, image, TextContent(text="what is this")],
        timestamp=1,
    )

    result = transform_messages([user], model)

    assert result[0].content == [
        TextContent(text=NON_VISION_USER_IMAGE_PLACEHOLDER),
        TextContent(text="what is this"),
    ]


def test_transform_messages_downgrades_tool_result_images() -> None:
    model = make_model(input=["text"])
    result = ToolResultMessage(
        tool_call_id="call_1",
        tool_name="screenshot",
        content=[ImageContent(data="AAAA", mime_type="image/png")],
        is_error=False,
        timestamp=1,
    )

    # The result is dangling for a text-only model, so pair it with its call.
    assistant = AssistantMessage(
        content=[ToolCall(id="call_1", name="screenshot", arguments={})],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(cost=Cost()),
        stop_reason="toolUse",
        timestamp=0,
    )

    transformed = transform_messages([assistant, result], model)

    tool_result = transformed[-1]
    assert isinstance(tool_result, ToolResultMessage)
    assert tool_result.content == [TextContent(text=NON_VISION_TOOL_IMAGE_PLACEHOLDER)]


def test_transform_messages_keeps_images_for_vision_models() -> None:
    model = make_model(input=["text", "image"])
    image = ImageContent(data="AAAA", mime_type="image/png")

    result = transform_messages([UserMessage(content=[image], timestamp=1)], model)

    assert result[0].content == [image]


# --------------------------------------------------------------------------------------
# Anthropic Messages
# --------------------------------------------------------------------------------------


def anthropic_text_events() -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "model": "test-model",
                    "usage": {
                        "input_tokens": 1_000_000,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 2000,
                        "cache_creation_input_tokens": 5000,
                    },
                },
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 100},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


async def test_anthropic_text_stream_event_order_and_final_message() -> None:
    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(sse_response(anthropic_sse(anthropic_text_events()))))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    events = await collect(stream)

    assert text_events(events) == [
        "start",
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
        "done",
    ]
    partial = events[0].partial
    assert all(
        event.partial is partial for event in events if hasattr(event, "partial")
    )
    assert events[-1].message is partial

    message = await stream.result()
    assert message.stop_reason == "stop"
    assert message.response_id == "msg_1"
    assert message.content == [TextContent(text="Hello world")]
    assert message.error_message is None


async def test_anthropic_usage_maps_tokens_and_cost() -> None:
    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(sse_response(anthropic_sse(anthropic_text_events()))))

    message = await provider.stream(
        model, assistant_user_context(), StreamOptions(api_key="k")
    ).result()

    usage = message.usage
    assert (usage.input, usage.output, usage.cache_read, usage.cache_write) == (
        1_000_000,
        100,
        2000,
        5000,
    )
    assert usage.total_tokens == 1_007_100
    # 1M * 3.0 + 100 * 15.0 + 2000 * 0.3 + 5000 * 3.75 (all per million tokens)
    assert usage.cost.input == pytest.approx(3.0)
    assert usage.cost.output == pytest.approx(0.0015)
    assert usage.cost.cache_read == pytest.approx(0.0006)
    assert usage.cost.cache_write == pytest.approx(0.01875)
    assert usage.cost.total == pytest.approx(3.02085)


async def test_anthropic_tool_call_assembles_across_deltas() -> None:
    events = [
        (
            "message_start",
            {"type": "message_start", "message": {"id": "msg_2", "usage": {"input_tokens": 5}}},
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"pa'},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": 'th": "a.txt"}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(sse_response(anthropic_sse(events))))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    collected = await collect(stream)

    assert text_events(collected) == [
        "start",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_delta",
        "toolcall_end",
        "done",
    ]
    end_event = collected[-2]
    assert end_event.tool_call.id == "toolu_1"
    assert end_event.tool_call.name == "read"
    assert end_event.tool_call.arguments == {"path": "a.txt"}

    message = await stream.result()
    assert message.stop_reason == "toolUse"
    assert message.raw_stop_reason == "tool_use"


async def test_anthropic_thinking_signature_is_accumulated_not_emitted() -> None:
    events = [
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    model = make_model(reasoning=True)
    provider = AnthropicMessagesProvider(client_factory=client_factory(sse_response(anthropic_sse(events))))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    collected = await collect(stream)

    # A signature_delta produces no protocol event of its own.
    assert text_events(collected) == [
        "start",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "done",
    ]
    message = await stream.result()
    assert message.content == [ThinkingContent(thinking="hmm", thinking_signature="sig-1")]


async def test_anthropic_request_body_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = {key.lower(): value for key, value in request.headers.items()}
        captured["url"] = str(request.url)
        return httpx.Response(200, content=anthropic_sse(anthropic_text_events()), headers=SSE_HEADERS)

    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(handler))
    context = Context(
        messages=[UserMessage(content="hi", timestamp=1)],
        system_prompt="be terse",
        tools=[
            pi_ai.Tool(
                name="read",
                description="Read a file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            )
        ],
    )

    await provider.stream(model, context, StreamOptions(api_key="k", max_tokens=512, temperature=0.2)).result()

    body = captured["body"]
    assert captured["url"] == "https://api.test/v1/messages"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["x-api-key"] == "k"
    # There is no system role: the prompt lives in a top-level field.
    assert body["system"] == [{"type": "text", "text": "be terse"}]
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.2
    assert body["stream"] is True
    assert body["tools"] == [
        {
            "name": "read",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]


async def test_anthropic_normalizes_foreign_tool_call_ids() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=anthropic_sse(anthropic_text_events()), headers=SSE_HEADERS)

    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(handler))
    raw_id = "call|weird/id+="
    context = Context(
        messages=[
            AssistantMessage(
                content=[ToolCall(id=raw_id, name="read", arguments={})],
                api="openai-responses",
                provider="openai",
                model="other",
                usage=Usage(cost=Cost()),
                stop_reason="toolUse",
                timestamp=1,
            ),
            ToolResultMessage(
                tool_call_id=raw_id,
                tool_name="read",
                content=[TextContent(text="ok")],
                is_error=False,
                timestamp=2,
            ),
        ]
    )

    await provider.stream(model, context, StreamOptions(api_key="k")).result()

    messages = captured["body"]["messages"]
    assert messages[0]["content"][0]["id"] == "call_weird_id__"
    # The result must reference the same rewritten id or the payload is invalid.
    assert messages[1]["content"][0]["tool_use_id"] == "call_weird_id__"


async def test_anthropic_thinking_budget_is_sent_and_temperature_is_dropped() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=anthropic_sse(anthropic_text_events()), headers=SSE_HEADERS)

    model = make_model(reasoning=True, max_tokens=64_000)
    provider = AnthropicMessagesProvider(client_factory=client_factory(handler))

    await provider.stream(
        model,
        assistant_user_context(),
        StreamOptions(api_key="k", max_tokens=2048, reasoning="medium", temperature=0.7),
    ).result()

    body = captured["body"]
    # 2048 + the medium budget (8192) still fits under the 64k model cap.
    assert body["max_tokens"] == 10_240
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192, "display": "summarized"}
    # Extended thinking rejects a custom temperature.
    assert "temperature" not in body


async def test_anthropic_http_error_becomes_error_event_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"error": {"message": "boom"}}')

    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(handler))

    # stream() must never raise for a request failure.
    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    events = await collect(stream)

    assert text_events(events) == ["error"]
    assert events[0].reason == "error"
    message = await stream.result()
    assert message.stop_reason == "error"
    assert "500" in message.error_message
    assert "boom" in message.error_message
    assert message.content == []


async def test_anthropic_truncated_stream_becomes_error_event() -> None:
    events = anthropic_sse(anthropic_text_events()[:5])  # no message_stop / message_delta
    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(sse_response(events)))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    collected = await collect(stream)

    assert collected[-1].type == "error"
    assert collected[-1].reason == "error"
    message = await stream.result()
    assert message.stop_reason == "error"
    assert "message_stop" in message.error_message
    # Partial content is preserved on the error message.
    assert message.content == [TextContent(text="Hello world")]


async def test_anthropic_abort_mid_stream_yields_aborted_error() -> None:
    controller = AbortController()
    head = anthropic_sse(anthropic_text_events()[:3])  # message_start + block start + "Hello"
    tail = anthropic_sse(anthropic_text_events()[3:])

    async def body() -> Any:
        yield head
        controller.abort("user cancelled")
        yield tail

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(), headers=SSE_HEADERS)

    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(handler))
    stream = provider.stream(
        model, assistant_user_context(), StreamOptions(api_key="k", signal=controller.signal)
    )
    events = await collect(stream)

    assert [event.type for event in events] == ["start", "text_start", "text_delta", "error"]
    assert events[-1].reason == "aborted"
    message = await stream.result()
    assert message.stop_reason == "aborted"
    assert message.error_message == "user cancelled"
    # The partial content generated before the abort is carried on the error.
    assert message.content == [TextContent(text="Hello")]
    # The error message is the same shared partial object.
    assert events[-1].error is events[0].partial


# --------------------------------------------------------------------------------------
# OpenAI Chat Completions
# --------------------------------------------------------------------------------------


def openai_text_chunks() -> list[dict[str, Any]]:
    return [
        {"id": "cc_1", "model": "test-model", "choices": [{"index": 0, "delta": {"content": "Hel"}}]},
        {"id": "cc_1", "model": "test-model", "choices": [{"index": 0, "delta": {"content": "lo"}}]},
        {
            "id": "cc_1",
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "cc_1",
            "model": "test-model",
            "choices": [],
            "usage": {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 2000},
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
        },
    ]


async def test_openai_text_stream_event_order_and_final_message() -> None:
    model = make_model(api="openai-completions", provider="openai", base_url="https://api.openai.com/v1")
    provider = OpenAiCompletionsProvider(client_factory=client_factory(sse_response(openai_sse(openai_text_chunks()))))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    events = await collect(stream)

    assert text_events(events) == [
        "start",
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
        "done",
    ]
    partial = events[0].partial
    assert all(
        event.partial is partial for event in events if hasattr(event, "partial")
    )
    assert events[-1].message is partial

    message = await stream.result()
    assert message is partial
    assert message.stop_reason == "stop"
    assert message.raw_stop_reason == "stop"
    assert message.response_id == "cc_1"
    assert message.content == [TextContent(text="Hello")]

    usage = message.usage
    assert (usage.input, usage.output, usage.cache_read, usage.cache_write) == (998_000, 100, 2000, 0)
    assert usage.reasoning == 7
    assert usage.total_tokens == 1_000_100
    # (998000 * 3.0 + 100 * 15.0 + 2000 * 0.3) / 1e6
    assert usage.cost.total == pytest.approx(2.9961)


async def test_openai_tool_call_assembles_across_deltas() -> None:
    chunks = [
        {
            "id": "cc_2",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"pa'},
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": "cc_2",
            "model": "test-model",
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}]}}
            ],
        },
        {
            "id": "cc_2",
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
    ]
    model = make_model(api="openai-completions", provider="openai", base_url="https://api.openai.com/v1")
    provider = OpenAiCompletionsProvider(client_factory=client_factory(sse_response(openai_sse(chunks))))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    events = await collect(stream)

    assert text_events(events) == [
        "start",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_delta",
        "toolcall_end",
        "done",
    ]
    assert events[-2].tool_call.arguments == {"path": "a.txt"}
    assert events[-2].content_index == 0

    message = await stream.result()
    assert message.stop_reason == "toolUse"
    assert message.content == [ToolCall(id="call_1", name="read", arguments={"path": "a.txt"})]


async def test_openai_malformed_tool_arguments_are_salvaged() -> None:
    chunks = [
        {
            "id": "cc_3",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path": "a.txt", "line":'},
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": "cc_3",
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
    ]
    model = make_model(api="openai-completions", provider="openai", base_url="https://api.openai.com/v1")
    provider = OpenAiCompletionsProvider(client_factory=client_factory(sse_response(openai_sse(chunks))))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    events = await collect(stream)

    # The stream still terminates normally rather than raising.
    assert text_events(events)[-1] == "done"
    message = await stream.result()
    assert message.stop_reason == "toolUse"
    assert message.content[0].arguments == {"path": "a.txt"}


async def test_openai_request_body_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = {key.lower(): value for key, value in request.headers.items()}
        captured["url"] = str(request.url)
        return httpx.Response(200, content=openai_sse(openai_text_chunks()), headers=SSE_HEADERS)

    model = make_model(
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        reasoning=True,
    )
    provider = OpenAiCompletionsProvider(client_factory=client_factory(handler))
    context = Context(
        messages=[UserMessage(content="hi", timestamp=1)],
        system_prompt="be terse",
        tools=[pi_ai.Tool(name="read", description="Read a file")],
    )

    await provider.stream(model, context, StreamOptions(api_key="k", max_tokens=256)).result()

    body = captured["body"]
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer k"
    # A reasoning-capable OpenAI model gets the developer role.
    assert body["messages"][0] == {"role": "developer", "content": "be terse"}
    assert body["max_completion_tokens"] == 256
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


async def test_openai_system_role_for_non_reasoning_gateway() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=openai_sse(openai_text_chunks()), headers=SSE_HEADERS)

    model = make_model(api="openai-completions", provider="some-gateway", base_url="https://gateway.test/v1")
    provider = OpenAiCompletionsProvider(client_factory=client_factory(handler))
    context = Context(messages=[UserMessage(content="hi", timestamp=1)], system_prompt="hi there")

    await provider.stream(model, context, StreamOptions(api_key="k", max_tokens=100)).result()

    body = captured["body"]
    assert body["messages"][0] == {"role": "system", "content": "hi there"}
    # Non-OpenAI gateways keep the portable max_tokens field.
    assert body["max_tokens"] == 100
    assert "max_completion_tokens" not in body


async def test_openai_mid_stream_http_error_yields_error_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"upstream exploded")

    model = make_model(api="openai-completions", provider="openai", base_url="https://api.openai.com/v1")
    provider = OpenAiCompletionsProvider(client_factory=client_factory(handler))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    events = await collect(stream)

    assert text_events(events) == ["error"]
    message = await stream.result()
    assert message.stop_reason == "error"
    assert "502" in message.error_message
    assert "upstream exploded" in message.error_message


async def test_openai_stream_without_finish_reason_errors() -> None:
    chunks = [{"id": "cc_4", "model": "test-model", "choices": [{"index": 0, "delta": {"content": "hi"}}]}]
    model = make_model(api="openai-completions", provider="openai", base_url="https://api.openai.com/v1")
    provider = OpenAiCompletionsProvider(client_factory=client_factory(sse_response(openai_sse(chunks))))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    collected = await collect(stream)

    assert collected[-1].type == "error"
    message = await stream.result()
    assert "finish_reason" in message.error_message
    assert message.content == [TextContent(text="hi")]


async def test_openai_reasoning_deltas_become_thinking_blocks() -> None:
    chunks = [
        {"id": "cc_5", "model": "test-model", "choices": [{"index": 0, "delta": {"reasoning_content": "thin"}}]},
        {"id": "cc_5", "model": "test-model", "choices": [{"index": 0, "delta": {"reasoning_content": "king"}}]},
        {"id": "cc_5", "model": "test-model", "choices": [{"index": 0, "delta": {"content": "answer"}}]},
        {"id": "cc_5", "model": "test-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    model = make_model(api="openai-completions", provider="openai", base_url="https://api.openai.com/v1")
    provider = OpenAiCompletionsProvider(client_factory=client_factory(sse_response(openai_sse(chunks))))

    stream = provider.stream(model, assistant_user_context(), StreamOptions(api_key="k"))
    events = await collect(stream)

    assert text_events(events) == [
        "start",
        "thinking_start",
        "thinking_delta",
        "thinking_delta",
        "text_start",
        "text_delta",
        "thinking_end",
        "text_end",
        "done",
    ]
    message = await stream.result()
    assert message.content == [
        ThinkingContent(thinking="thinking", thinking_signature="reasoning_content"),
        TextContent(text="answer"),
    ]


async def test_stream_simple_may_raise_but_stream_does_not() -> None:
    model = make_model()
    provider = AnthropicMessagesProvider(client_factory=client_factory(sse_response(b"")))

    with pytest.raises(pi_ai.AuthenticationError):
        provider.stream_simple(model, assistant_user_context(), StreamOptions())

    # The same missing credential through stream() is encoded, not raised.
    events = await collect(provider.stream(model, assistant_user_context(), StreamOptions()))
    assert text_events(events) == ["error"]


# --------------------------------------------------------------------------------------
# Faux provider
# --------------------------------------------------------------------------------------


async def test_faux_provider_uses_one_shared_mutated_partial() -> None:
    turn = FauxTurn(
        thinking="pondering",
        text="hello world",
        tool_calls=[ToolCall(id="call_1", name="read", arguments={"path": "a.txt"})],
        stop_reason="toolUse",
        usage=Usage(input=3, output=4, total_tokens=7, cost=Cost()),
    )
    provider = FauxProvider([turn])
    model = make_model(api="faux", provider="faux")

    stream = provider.stream(model, assistant_user_context(), StreamOptions())
    events = await collect(stream)

    # Every event carries the same live message object.
    partial = events[0].partial
    for event in events:
        if hasattr(event, "partial"):
            assert event.partial is partial
    assert events[-1].type == "done"
    assert events[-1].message is partial

    # That object ends up fully populated.
    assert [type(block).__name__ for block in partial.content] == [
        "ThinkingContent",
        "TextContent",
        "ToolCall",
    ]
    assert partial.content[0].thinking == "pondering"
    assert partial.content[1].text == "hello world"
    assert partial.content[2].arguments == {"path": "a.txt"}
    assert partial.stop_reason == "toolUse"
    assert partial.usage.input == 3
    # Usage is priced against the model rates, like a real adapter would.
    assert partial.usage.cost.input == pytest.approx(3 * 3.0 / 1_000_000)
    assert partial.usage.cost.output == pytest.approx(4 * 15.0 / 1_000_000)
    # ... without mutating the scripted turn.
    assert turn.usage is not None and turn.usage.cost.total == 0.0


async def test_faux_provider_event_protocol_and_consumes_one_turn_per_call() -> None:
    provider = FauxProvider([FauxTurn(text="first"), FauxTurn(text="second")])
    model = make_model(api="faux", provider="faux")

    first = await provider.stream(model, assistant_user_context(), StreamOptions()).result()
    second = await provider.stream(model, assistant_user_context(), StreamOptions()).result()

    assert first.content == [TextContent(text="first")]
    assert second.content == [TextContent(text="second")]
    assert provider.call_count == 2


async def test_faux_provider_exhausted_emits_error() -> None:
    provider = FauxProvider([])
    model = make_model(api="faux", provider="faux")

    stream = provider.stream(model, assistant_user_context(), StreamOptions())
    events = await collect(stream)

    assert text_events(events) == ["error"]
    message = await stream.result()
    assert message.stop_reason == "error"
    assert "No more faux responses" in message.error_message


async def test_faux_provider_aborted_signal_emits_aborted_error() -> None:
    controller = AbortController()
    controller.abort("stop now")
    provider = FauxProvider([FauxTurn(text="never")])
    model = make_model(api="faux", provider="faux")

    stream = provider.stream(
        model, assistant_user_context(), StreamOptions(signal=controller.signal)
    )
    events = await collect(stream)

    assert text_events(events) == ["error"]
    assert events[0].reason == "aborted"
    assert (await stream.result()).stop_reason == "aborted"


async def test_register_faux_api_dispatches_through_pi_ai_stream() -> None:
    provider = register_faux_api("faux-provider-test", [FauxTurn(text="scripted")])
    model = make_model(api="faux-provider-test", provider="faux")

    message = await pi_ai.stream(model, assistant_user_context(), StreamOptions()).result()

    assert message.content == [TextContent(text="scripted")]
    assert provider.call_count == 1
    assert "faux-provider-test" in pi_ai.list_apis()


async def test_register_faux_api_emits_text_deltas_in_order() -> None:
    register_faux_api("faux-provider-deltas", [FauxTurn(text="abcdefghij")])
    model = make_model(api="faux-provider-deltas", provider="faux")

    stream = pi_ai.stream(model, assistant_user_context(), StreamOptions())
    events = await collect(stream)

    assert text_events(events) == ["start", "text_start", "text_delta", "text_delta", "text_end", "done"]
    assert [event.delta for event in events if event.type == "text_delta"] == ["abcdefgh", "ij"]
    # Deltas mutate the shared partial as they are emitted.
    assert events[0].partial.content[0].text == "abcdefghij"


async def test_a_stalled_stream_can_still_be_aborted() -> None:
    """`stop` must not have to wait for the read timeout.

    The abort signal used to be checked only *inside* the chunk loop, so a stream that sent
    nothing ignored the user for as long as httpx's read timeout — ten minutes by default,
    which a person experiences as a hung turn they cannot cancel.
    """
    import asyncio

    controller = AbortController()
    first = openai_sse(
        [{"id": "cc", "model": "test-model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}]}],
        done=False,
    )

    async def stalled() -> Any:
        yield first
        await asyncio.sleep(3600)  # the socket stays open and stays silent

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stalled(), headers=SSE_HEADERS)

    model = make_model(api="openai-completions", provider="openai", base_url="https://api.openai.com/v1")
    provider = OpenAiCompletionsProvider(client_factory=client_factory(handler))

    async def abort_soon() -> None:
        await asyncio.sleep(0.2)
        controller.abort("user cancelled")

    waiter = asyncio.create_task(abort_soon())
    try:
        result = await asyncio.wait_for(
            provider.stream(
                model,
                assistant_user_context(),
                StreamOptions(api_key="k", signal=controller.signal),
            ).result(),
            timeout=10,
        )
    finally:
        await waiter

    assert result.stop_reason == "aborted"
    assert result.error_message == "user cancelled"
