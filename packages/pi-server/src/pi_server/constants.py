"""Values shared by the router and the session runtime.

They live apart from both because ``app.py`` and ``worker.py`` need the same names and
neither should import the other: the router owns HTTP, the runtime owns sessions.
"""

from __future__ import annotations

#: Shortest password the account endpoints will accept.  Length beats composition rules.
MIN_PASSWORD_LENGTH = 8

#: The single implicit tenant in ``single`` mode.  Team mode never uses it.
LOCAL_UID = "local"

#: A user has at most one live worker, so one sandbox slot per user is enough.  Naming it
#: (rather than hashing something dynamic) is what makes the container id stable across an
#: idle eviction and restart, so a user's installed packages survive a quiet afternoon.
DEFAULT_SANDBOX_SLOT = "session"

#: Dialog methods that block a handler until a client answers.
BLOCKING_DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})
