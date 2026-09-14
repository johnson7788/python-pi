"""``pi-py-web`` — serve the browser UI for one working directory."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from pi_coding_agent.bootstrap import BootstrapOptions

from .app import __version__, create_app
from .auth import ensure_admin
from .config import ServerConfig, resolve_data_root
from .db import Database


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
    # -- team mode -----------------------------------------------------------------
    parser.add_argument(
        "--auth-mode",
        choices=("single", "team"),
        help="'single' (default) is the original personal tool; 'team' requires accounts.",
    )
    parser.add_argument(
        "--data-root",
        help="Database, per-user workspaces and per-user agent dirs (default: ~/.pi/team).",
    )
    parser.add_argument(
        "--admin-username", help="Username of the first superadmin (default: admin)."
    )
    parser.add_argument(
        "--admin-password",
        help="Password for the first superadmin; generated and printed once when omitted.",
    )
    parser.add_argument(
        "--cookie-secure",
        action="store_true",
        help="Mark the session cookie Secure. Required when serving over HTTPS.",
    )
    parser.add_argument(
        "--session-ttl-hours", type=int, help="Browser session lifetime (default: 336)."
    )
    return parser


def _server_config(args: argparse.Namespace) -> ServerConfig:
    """Merge explicit flags over the ``PI_*`` environment defaults."""
    overrides: dict[str, object] = {}
    if args.data_root:
        overrides["data_root"] = resolve_data_root(args.data_root)
    if args.auth_mode:
        overrides["auth_mode"] = args.auth_mode
    if args.cookie_secure:
        overrides["cookie_secure"] = True
    if args.admin_username:
        overrides["admin_username"] = args.admin_username
    if args.session_ttl_hours:
        overrides["session_ttl_hours"] = args.session_ttl_hours
    return ServerConfig.from_env(**overrides)


def _check_sandbox_backend(config: ServerConfig) -> None:
    """Refuse to start on a sandbox configuration that cannot contain anything.

    An operator who sets ``PI_SANDBOX_BACKEND=provisioner`` believes commands are contained.
    Starting anyway with a missing URL or token would mean they silently are not, and the
    first sign of it would be a command running on the host.
    """
    from pi_coding_agent.sandbox import SandboxScope, assert_backend_usable, select_backend

    # A throwaway scope: this checks that the *configuration* is complete, not that a
    # particular user's container exists.
    scope = SandboxScope(uid="startup-check", thread="startup-check", workspace=config.data_root)
    try:
        backend = select_backend(
            config.sandbox_backend,
            scope=scope,
            url=config.provisioner_url,
            token=config.provisioner_token,
            image=config.sandbox_image,
            network=config.sandbox_network,
            run_as=config.sandbox_run_as,
        )
        assert_backend_usable(backend)
    except (ValueError, RuntimeError) as error:
        print(f"pi-py-web: {error}", file=sys.stderr)
        print(
            "pi-py-web: unset PI_SANDBOX_BACKEND to run in-process. See docs/DEPLOY.md.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def _bootstrap_first_admin(config: ServerConfig, password: str | None) -> None:
    """Create the first superadmin.

    A ``password`` provided by the operator is used as-is and the account is not forced to
    change it — presetting a deployment's admin password only helps if it stays the one that
    was preset.  With no password, one is generated and printed exactly once, and changing it
    at first login is mandatory.
    """
    config.ensure_layout()
    database = Database(config.db_path)
    try:
        created = ensure_admin(database, config.admin_username, password)
    finally:
        database.close()
    if created is None:
        return
    username, generated = created
    if password is not None:
        print(f"[pi-py-web] created the first superadmin: {username}", file=sys.stderr)
        return
    print(
        f"[pi-py-web] created the first superadmin\n"
        f"[pi-py-web]   username: {username}\n"
        f"[pi-py-web]   password: {generated}\n"
        f"[pi-py-web] This password is shown once and must be changed at first login.",
        file=sys.stderr,
    )


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

    config = _server_config(args)
    _check_sandbox_backend(config)
    if config.requires_auth:
        _bootstrap_first_admin(config, args.admin_password or config.admin_password)

    app = create_app(
        options,
        on_warning=lambda message: print(f"pi-py: {message}", file=sys.stderr),
        config=config,
    )

    from .app import resolve_static_dir

    if resolve_static_dir() is None:
        print(
            "pi-py-web: the web UI is not built yet; only /api/* will work.\n"
            "          run `npm install && npm run build` in web/ (or set PI_WEB_DIST)",
            file=sys.stderr,
        )

    print(f"pi-py-web {__version__} serving {cwd} at http://{args.host}:{args.port}", file=sys.stderr)
    if config.requires_auth:
        print(
            f"pi-py-web: auth mode 'team' — accounts in {config.db_path},\n"
            f"           workspaces under {config.workspace_root} (per-user cwd lands in M1)",
            file=sys.stderr,
        )
    else:
        print("pi-py-web: auth mode 'single' — no accounts, one shared session", file=sys.stderr)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        reload=bool(args.reload),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
