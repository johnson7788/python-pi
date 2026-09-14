"""Command-line entry point.

Startup order lives in :mod:`.bootstrap` because the web server needs exactly the
same sequence; this module only owns argument parsing, mode selection and exit
codes.

The interactive TUI is out of scope for this port, so the default mode is
``print``.  Diagnostics always go to stderr; stdout carries only assistant output
(``print``), the JSON event stream (``json``), or the JSONL command protocol
(``rpc``).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Sequence

from pi_ai import AuthenticationError, ModelRegistry

from ._version import __version__
from .bootstrap import BootstrapOptions, bootstrap, resolve_project_trust, select_model

EXIT_OK = 0
EXIT_AGENT_ERROR = 1
EXIT_USAGE = 2

#: Imported lazily inside :func:`run`.  Keeping the session/mode layers out of the
#: module import graph makes ``--version`` and ``--list-models`` cheap and keeps
#: the argument parser unit-testable without a configured environment.
_LAZY_IMPORTS = {
    "run_json_mode": "pi_coding_agent.modes.json_mode",
    "run_print_mode": "pi_coding_agent.modes.print_mode",
    "run_rpc_mode": "pi_coding_agent.modes.rpc_mode",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-py",
        description="A minimal Python coding agent harness, interoperable with pi sessions.",
    )
    parser.add_argument("prompt", nargs="*", help="Prompt to send. Reads stdin when omitted and piped.")
    parser.add_argument("--version", action="version", version=f"pi-py {__version__}")
    parser.add_argument("--list-models", action="store_true", help="List models with usable auth, then exit.")

    parser.add_argument("--provider", help="Provider id (e.g. anthropic).")
    parser.add_argument("--model", help="Model id, 'provider/id', or a glob such as 'anthropic/*'.")
    parser.add_argument("--thinking", help="Thinking level: off|minimal|low|medium|high|xhigh|max.")
    parser.add_argument("--api-key", help="Runtime API key for this process only (never persisted).")

    parser.add_argument("--cwd", help="Working directory (defaults to the current directory).")
    parser.add_argument("--agent-dir", help="Global config directory (defaults to ~/.pi/agent).")
    parser.add_argument("--session-dir", help="Override the session storage directory.")
    parser.add_argument("--no-session", action="store_true", help="Do not persist this session.")
    parser.add_argument("--resume", metavar="FILE", help="Resume an existing session file.")
    parser.add_argument("--name", help="Session display name.")

    parser.add_argument(
        "--mode",
        choices=("print", "json", "rpc"),
        default="print",
        help=(
            "Output mode. 'print' streams assistant text; 'json' emits one JSON event per line; "
            "'rpc' serves the pi JSONL command protocol on stdin/stdout."
        ),
    )
    parser.add_argument(
        "-e",
        "--extension",
        action="append",
        default=[],
        metavar="PATH",
        help="Load an extension file or directory (repeatable).",
    )
    parser.add_argument(
        "--no-extensions",
        action="store_true",
        help="Do not discover or load any extension.",
    )
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help="Do not inject agent skills into the system prompt.",
    )
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    # Unknown ``--flags`` are not errors: pi routes them to extension flags.
    args, unknown = parser.parse_known_args(list(argv) if argv is not None else None)

    cwd = os.path.abspath(os.path.expanduser(args.cwd or os.getcwd()))

    # Cheap early exits that must not touch settings, trust or credentials.
    if args.list_models:
        from pi_ai import AuthStorage, FileCredentialStore, ModelRegistry as Registry

        from .settings import SettingsManager

        settings = SettingsManager(cwd, args.agent_dir, project_trusted=False)
        auth = AuthStorage(FileCredentialStore(os.path.join(settings.agent_dir, "auth.json")))
        registry = Registry(auth, models_path=os.path.join(settings.agent_dir, "models.json"))
        return _list_models(registry)

    # RPC mode owns stdin: reading it here would swallow the command stream.
    prompt = "" if args.mode == "rpc" else _read_prompt(args)
    if args.mode != "rpc" and not prompt:
        parser.print_usage(sys.stderr)
        print("pi-py: no prompt given (pass one as an argument or pipe it on stdin)", file=sys.stderr)
        return EXIT_USAGE

    state = await bootstrap(
        BootstrapOptions(
            cwd=cwd,
            agent_dir=args.agent_dir,
            model_pattern=args.model,
            provider=args.provider,
            thinking=args.thinking,
            api_key=args.api_key,
            session_dir=args.session_dir,
            no_session=args.no_session,
            resume=args.resume,
            session_name=args.name,
            extension_paths=tuple(args.extension or ()),
            no_extensions=bool(args.no_extensions),
            no_skills=bool(args.no_skills),
            unknown_flags=tuple(unknown),
            host_mode=args.mode,
            with_runtime=args.mode == "rpc",
        )
    )

    for warning in state.warnings:
        print(f"pi-py: resource warning: {warning}", file=sys.stderr)

    try:
        if args.mode == "rpc":
            return await _lazy("run_rpc_mode")(
                runtime=state.runtime,
                model_registry=state.registry,
                resource_loader=state.loader,
                extension_runner=state.extension_runner,
            )
        if args.mode == "json":
            return await _lazy("run_json_mode")(state.session, prompt)
        return await _lazy("run_print_mode")(state.session, prompt)
    finally:
        if state.extension_runner is not None:
            for record in state.extension_runner.errors:
                print(
                    f"pi-py: extension error in {record.extension_path}: {record.error}",
                    file=sys.stderr,
                )
        await state.shutdown("quit")


def _lazy(name: str):
    import importlib

    return getattr(importlib.import_module(_LAZY_IMPORTS[name]), name)


def _list_models(registry: ModelRegistry) -> int:
    for model in registry.list_models():
        marker = "*" if registry.has_configured_auth(model.provider) else " "
        reasoning = " reasoning" if model.reasoning else ""
        print(f"{marker} {model.provider}/{model.id}{reasoning}")
    print("\n* = credentials configured", file=sys.stderr)
    return EXIT_OK


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return " ".join(args.prompt)
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read().strip()
    except (OSError, ValueError, AttributeError):
        # No readable stdin (closed pipe, test harness, detached process).
        return ""


# Thin adapters kept for callers that pass the parsed namespace.

def _resolve_trust(cwd: str, agent_dir: str, settings: Any) -> bool:
    return resolve_project_trust(cwd, agent_dir, settings)


def _select_model(registry: ModelRegistry, args: argparse.Namespace, settings: Any):
    return select_model(
        registry, pattern=args.model, provider=args.provider, settings=settings
    )


def main(argv: Sequence[str] | None = None) -> None:
    try:
        code = asyncio.run(run(argv))
    except KeyboardInterrupt:
        code = 130
    except AuthenticationError as error:
        print(f"pi-py: {error}", file=sys.stderr)
        code = EXIT_AGENT_ERROR
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
