"""pi-server — serve the python-pi browser UI.

The browser speaks the same JSON command protocol as ``pi --mode rpc``; this package is
the transport (WebSocket + a few REST endpoints), the static file host, and — in team
mode — the account, workspace and quota layer that makes one process safe to share.
"""

from __future__ import annotations

from .app import AgentServer, Connection, Hub, create_app, resolve_static_dir
from .config import COOKIE_NAME, ServerConfig
from .db import Database

__version__ = "0.1.0"

__all__ = [
    "AgentServer",
    "COOKIE_NAME",
    "Connection",
    "Database",
    "Hub",
    "ServerConfig",
    "create_app",
    "resolve_static_dir",
]
