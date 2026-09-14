"""pi-coding-agent — the coding-agent harness layer.

Python port of ``@earendil-works/pi-coding-agent``.  This package sits on top of
``pi-ai`` (provider/message layer) and ``pi-agent-core`` (loop/runtime) and adds
what makes it an actual coding agent: sessions, built-in tools, resources and the
system prompt.

Names that depend on optional modules are resolved lazily (:pep:`562`), so
``import pi_coding_agent`` stays cheap and never fails because of a module that a
particular deployment does not need.
"""

from __future__ import annotations

from .messages import (
    AgentMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    agent_message_from_json,
    agent_message_to_json,
    convert_to_llm,
)
from .resource_loader import ResourceLoader, find_config_dir, get_agent_dir
from .session_manager import (
    CURRENT_SESSION_VERSION,
    SessionManager,
    build_session_context,
    read_session_header,
    session_dir_name,
)
from .settings import ProjectNotTrustedError, SettingsManager, SettingsValidationError
from .skills import Skill, discover_skills, format_skills_for_prompt
from .system_prompt import BuildSystemPromptOptions, build_system_prompt
from .tools import (
    BUILTIN_TOOL_NAMES,
    DEFAULT_ACTIVE_TOOL_NAMES,
    create_builtin_tools,
)

#: Names that live in modules which may pull heavier dependencies; resolved on
#: first attribute access rather than at import time.
_LAZY_EXPORTS = {
    "AgentSession": "pi_coding_agent.agent_session",
    "AgentSessionRuntime": "pi_coding_agent.agent_session_runtime",
    "create_agent_session_runtime": "pi_coding_agent.agent_session_runtime",
    "CompactionResult": "pi_coding_agent.compaction",
    "CompactionSettings": "pi_coding_agent.compaction",
    "compact_session": "pi_coding_agent.compaction",
    "estimate_tokens": "pi_coding_agent.compaction",
    "should_compact": "pi_coding_agent.compaction",
    "run_print_mode": "pi_coding_agent.modes.print_mode",
    "run_json_mode": "pi_coding_agent.modes.json_mode",
    "run_rpc_mode": "pi_coding_agent.modes.rpc_mode",
    "RpcMode": "pi_coding_agent.modes.rpc_mode",
}

__version__ = "0.1.0"

__all__ = [
    "BUILTIN_TOOL_NAMES",
    "CURRENT_SESSION_VERSION",
    "DEFAULT_ACTIVE_TOOL_NAMES",
    "AgentMessage",
    "BashExecutionMessage",
    "BranchSummaryMessage",
    "BuildSystemPromptOptions",
    "CompactionSummaryMessage",
    "CustomMessage",
    "ProjectNotTrustedError",
    "ResourceLoader",
    "SessionManager",
    "SettingsManager",
    "SettingsValidationError",
    "Skill",
    "agent_message_from_json",
    "agent_message_to_json",
    "build_session_context",
    "build_system_prompt",
    "convert_to_llm",
    "create_builtin_tools",
    "discover_skills",
    "find_config_dir",
    "format_skills_for_prompt",
    "get_agent_dir",
    "read_session_header",
    "session_dir_name",
]


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)
