"""A runnable, fully offline example of the python-pi framework.

It drives the real agent loop with the real built-in tools, but swaps the LLM for
the deterministic faux provider, so it needs no network and no API key.

    uv run python examples/offline_agent.py

What it demonstrates:

* registering a provider and addressing it through the normal ``model.api`` path;
* the ``AgentTool`` contract and validation;
* ``convert_to_llm`` projecting agent-level messages to LLM messages;
* streaming events, including tool lifecycle events;
* persisting the transcript to a real pi v3 JSONL session file.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from pi_ai import Model, ModelCost, ToolCall
from pi_ai.providers.faux import FauxTurn, register_faux_api
from pi_agent import Agent
from pi_coding_agent.messages import convert_to_llm
from pi_coding_agent.session_manager import SessionManager
from pi_coding_agent.system_prompt import BuildSystemPromptOptions, build_system_prompt
from pi_coding_agent.tools import create_builtin_tools

API_NAME = "faux-example"


def build_model() -> Model:
    return Model(
        id="faux-model",
        name="Faux Model",
        api=API_NAME,
        provider="faux",
        base_url="http://localhost",
        reasoning=False,
        input=["text"],
        cost=ModelCost(input=1.0, output=2.0),
        context_window=100_000,
        max_tokens=4096,
    )


async def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="pi-py-example-"))
    agent_dir = workdir / ".agent"
    print(f"working directory: {workdir}\n")

    # Two scripted assistant turns: one tool call, then a closing answer.
    register_faux_api(
        API_NAME,
        [
            FauxTurn(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write",
                        arguments={"path": "greeting.txt", "content": "hello from python-pi"},
                    )
                ],
                stop_reason="toolUse",
            ),
            FauxTurn(text="Wrote greeting.txt."),
        ],
    )

    tools = create_builtin_tools(str(workdir), ("write", "read", "ls"))
    system_prompt = build_system_prompt(
        BuildSystemPromptOptions(
            cwd=str(workdir),
            selected_tools=[tool.name for tool in tools],
            tool_snippets={
                tool.name: (tool.prompt_snippet or tool.description) for tool in tools
            },
        )
    )

    agent = Agent(
        model=build_model(),
        system_prompt=system_prompt,
        tools=tools,
        convert_to_llm=convert_to_llm,
    )

    def on_event(event) -> None:
        if event.type == "tool_execution_start":
            print(f"  [tool] {event.tool_name}({event.args})")
        elif event.type == "tool_execution_end":
            status = "error" if event.is_error else "ok"
            print(f"  [tool] {event.tool_name} -> {status}")
        elif event.type == "message_update":
            inner = event.assistant_message_event
            if inner.type == "text_delta":
                print(inner.delta, end="", flush=True)
        elif event.type == "message_end" and getattr(event.message, "role", None) == "assistant":
            print()

    agent.subscribe(on_event)

    print("prompt: create greeting.txt and tell me you did")
    await agent.prompt("create greeting.txt and tell me you did")

    print("\n--- transcript ---")
    for message in agent.state.messages:
        role = message.role
        if role == "toolResult":
            body = "".join(block.text for block in message.content if block.type == "text")
            print(f"{role}: {body.strip()}")
        else:
            blocks = message.content
            if isinstance(blocks, str):
                print(f"{role}: {blocks}")
            else:
                text = "".join(
                    getattr(block, "text", getattr(block, "name", "")) for block in blocks
                )
                print(f"{role}: {text.strip()}")

    print("\n--- created files ---")
    for path in sorted(workdir.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(workdir)}")

    # Persist exactly as the coding agent does: one entry per message.
    session = SessionManager.create(str(workdir), str(agent_dir))
    for message in agent.state.messages:
        await session.append_message(message)

    session_file = session.get_session_file()
    print(f"\nsession file: {session_file}")
    if session_file and Path(session_file).exists():
        print(f"session lines: {len(Path(session_file).read_text().splitlines())}")


if __name__ == "__main__":
    asyncio.run(main())
