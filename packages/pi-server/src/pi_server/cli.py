"""``pi-py-web`` — serve the browser UI for one working directory."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from pi_coding_agent.bootstrap import BootstrapOptions

from .app import __version__, create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-py-web",
        description="Serve the python-pi browser UI (and the pi command protocol over WebSocket).",
    )
    parser.add_argument("--version", action="version", version=f"pi-py-web {__version__}")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8787, help="Port (default: 8787).")
    parser.add_argument("--cwd", help="Working directory for the agent (default: current directory).")
    parser.add_argument("--agent-dir", help="Global config directory (defaults to ~/.pi/agent).")
    parser.add_argument("--model", help="Model id, 'provider/id', or a glob.")
    parser.add_argument("--provider", help="Provider id.")
    parser.add_argument("--thinking", help="Thinking level: off|minimal|low|medium|high|xhigh|max.")
    parser.add_argument("--api-key", help="Runtime API key for this process only (never persisted).")
    parser.add_argument("--no-session", action="store_true", help="Keep the session in memory only.")
    parser.add_argument("--resume", metavar="FILE", help="Resume an existing session file.")
    parser.add_argument("--name", help="Session display name.")
    parser.add_argument(
        "-e", "--extension", action="append", default=[], metavar="PATH",
        help="Load an extension file or directory (repeatable).",
    )
    parser.add_argument("--no-extensions", action="store_true", help="Do not load any extension.")
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help="Do not inject agent skills into the system prompt.",
    )
    parser.add_argument(
        "--reload", action="store_true", help="Auto-reload on code changes (development only)."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args, unknown = parser.parse_known_args(list(argv) if argv is not None else None)

    cwd = os.path.abspath(os.path.expanduser(args.cwd or os.getcwd()))
    options = BootstrapOptions(
        cwd=cwd,
        agent_dir=args.agent_dir,
        model_pattern=args.model,
        provider=args.provider,
        thinking=args.thinking,
        api_key=args.api_key,
        no_session=args.no_session,
        resume=args.resume,
        session_name=args.name,
        extension_paths=tuple(args.extension or ()),
        no_extensions=bool(args.no_extensions),
        no_skills=bool(args.no_skills),
        unknown_flags=tuple(unknown),
        host_mode="web",
        with_runtime=True,
    )

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - dependency is declared
        print("pi-py-web: uvicorn is required (pip install pi-server)", file=sys.stderr)
        raise SystemExit(1) from None

    app = create_app(options, on_warning=lambda message: print(f"pi-py: {message}", file=sys.stderr))

    from .app import resolve_static_dir

    if resolve_static_dir() is None:
        print(
            "pi-py-web: the web UI is not built yet; only /api/* will work.\n"
            "          run `npm install && npm run build` in web/ (or set PI_WEB_DIST)",
            file=sys.stderr,
        )

    print(f"pi-py-web {__version__} serving {cwd} at http://{args.host}:{args.port}", file=sys.stderr)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        reload=bool(args.reload),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
