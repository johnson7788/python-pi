"""pi-server — serve the python-pi browser UI.

The browser speaks the same JSON command protocol as ``pi --mode rpc``; this package
is only the transport (WebSocket + a few REST endpoints) and the static file host.
"""

from __future__ import annotations

from .app import AgentServer, Connection, Hub, create_app, resolve_static_dir

__version__ = "0.1.0"

__all__ = [
    "AgentServer",
    "Connection",
    "Hub",
    "create_app",
    "resolve_static_dir",
]
