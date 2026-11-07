"""SQLite persistence for the team deployment: accounts, sessions, usage, audit.

Four decisions worth stating, because each one is a place the design could have gone
another way:

**SQLite, not PostgreSQL.**  The deployment target is one process serving an internal
team, so a second database server would be pure operational cost.  The documented
trigger to migrate is in ``docs/plans/2026-09-14-team-saas-design.md``: more than one
API process, or more than ~50 accounts.

**Conversations stay on disk.**  Session transcripts remain ``pi``'s v3 JSONL files
under each user's agent dir — that format is the interoperability asset of this port.
Only metadata, usage and audit live here, so there is never a second source of truth
for a conversation.

**One connection behind a lock.**  Queries are small and indexed; serialising them is
cheaper than managing a pool, and it keeps writes (quota checks, audit rows) trivially
consistent.  ``atomic()`` is the only place that opens a transaction, so a reader can
never observe a half-written turn.

**Migrations are append-only.**  ``MIGRATIONS`` is the schema's history; a shipped
version is never edited, only added to.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

_SCHEMA_V1 = """
CREATE TABLE users (
  uid                   TEXT PRIMARY KEY,
  username              TEXT NOT NULL UNIQUE,
  display_name          TEXT NOT NULL DEFAULT '',
  password_hash         TEXT NOT NULL,
  role                  TEXT NOT NULL DEFAULT 'user',
  status                TEXT NOT NULL DEFAULT 'active',
  must_change_password  INTEGER NOT NULL DEFAULT 0,
  monthly_token_quota   INTEGER,
  monthly_cost_quota    REAL,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  last_login_at         TEXT
);

CREATE TABLE auth_sessions (
  token_hash   TEXT PRIMARY KEY,
  uid          TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  revoked_at   TEXT,
  user_agent   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE usage_records (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                 TEXT NOT NULL,
  uid                TEXT NOT NULL,
  session_id         TEXT NOT NULL,
  provider           TEXT NOT NULL DEFAULT '',
  model_id           TEXT NOT NULL DEFAULT '',
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost               REAL NOT NULL DEFAULT 0,
  request_kind       TEXT NOT NULL DEFAULT 'turn'
);

CREATE TABLE audit_records (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  uid           TEXT NOT NULL,
  session_id    TEXT NOT NULL DEFAULT '',
  tool_name     TEXT NOT NULL,
  tool_call_id  TEXT NOT NULL DEFAULT '',
  decision      TEXT NOT NULL,
  reason        TEXT NOT NULL DEFAULT '',
  input_preview TEXT NOT NULL DEFAULT '',
  is_error      INTEGER NOT NULL DEFAULT 0,
  duration_ms   INTEGER,
  exit_code     INTEGER
);

CREATE INDEX idx_usage_uid_ts ON usage_records(uid, ts);
CREATE INDEX idx_usage_ts ON usage_records(ts);
CREATE INDEX idx_audit_uid_ts ON audit_records(uid, ts);
CREATE INDEX idx_auth_sessions_uid ON auth_sessions(uid);
"""

#: Which session file a user was last working in.  A worker is evicted when it goes
#: idle, and without this pointer the next visit would silently start a *new* session
#: instead of reopening the one they left.
_SCHEMA_V2 = """
CREATE TABLE session_pointers (
  uid          TEXT PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE,
  session_file TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
"""

_SCHEMA_V3 = """
CREATE TABLE skill_prefs (
  uid        TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  workspace  TEXT NOT NULL DEFAULT 'default',
  name       TEXT NOT NULL,
  enabled    INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (uid, workspace, name)
);
"""

_SCHEMA_V4 = """
CREATE TABLE workspaces (
  uid        TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  id         TEXT NOT NULL,
  name       TEXT NOT NULL,
  path       TEXT NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (uid, id),
  UNIQUE (uid, name)
);

CREATE TABLE workspace_session_pointers (
  uid          TEXT NOT NULL,
  workspace    TEXT NOT NULL,
  session_file TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (uid, workspace)
);

INSERT INTO workspace_session_pointers (uid, workspace, session_file, updated_at)
SELECT uid, 'default', session_file, updated_at FROM session_pointers;
"""

_SCHEMA_V5 = """
CREATE TABLE composition_session_pointers (
  uid          TEXT NOT NULL,
  workspace    TEXT NOT NULL,
  app_id       TEXT NOT NULL,
  mode_id      TEXT NOT NULL,
  session_file TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (uid, workspace, app_id, mode_id)
);

INSERT INTO composition_session_pointers
  (uid, workspace, app_id, mode_id, session_file, updated_at)
SELECT uid, workspace, 'general', 'office', session_file, updated_at
FROM workspace_session_pointers;
"""

_SCHEMA_V6 = """
CREATE TABLE plugin_enable (
  uid        TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  app_id     TEXT NOT NULL,
  plugin_id  TEXT NOT NULL,
  enabled    INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (uid, app_id, plugin_id)
);
"""

_SCHEMA_V7 = """
CREATE TABLE schedules (
  id               TEXT PRIMARY KEY,
  uid              TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  workspace        TEXT NOT NULL,
  app_id           TEXT NOT NULL,
  mode_id          TEXT NOT NULL,
  name             TEXT NOT NULL,
  prompt           TEXT NOT NULL,
  kind             TEXT NOT NULL,
  run_at           TEXT,
  hour             INTEGER,
  minute           INTEGER,
  weekday          INTEGER,
  interval_minutes INTEGER,
  enabled          INTEGER NOT NULL DEFAULT 1,
  next_run_at      TEXT,
  last_run_at      TEXT,
  last_status      TEXT,
  last_error       TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE schedule_runs (
  id          TEXT PRIMARY KEY,
  schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL,
  session_file TEXT,
  error       TEXT
);
"""

_SCHEMA_V8 = """
CREATE TABLE notifications (
  id         TEXT PRIMARY KEY,
  uid        TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  title      TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  link       TEXT NOT NULL DEFAULT '',
  read       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX notifications_uid_created ON notifications(uid, created_at DESC);
"""

_SCHEMA_V9 = """
CREATE TABLE apps (
  uid         TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  id          TEXT NOT NULL,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  accent      TEXT NOT NULL DEFAULT '#3b5fba',
  builtin     INTEGER NOT NULL DEFAULT 0,
  sort        INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (uid, id)
);

CREATE TABLE app_modes (
  uid         TEXT NOT NULL,
  app_id      TEXT NOT NULL,
  id          TEXT NOT NULL,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  prompt      TEXT NOT NULL DEFAULT '',
  skills_json TEXT,
  sort        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, app_id, id)
);

CREATE TABLE app_capsules (
  uid      TEXT NOT NULL,
  app_id   TEXT NOT NULL,
  mode_id  TEXT NOT NULL,
  id       TEXT NOT NULL,
  name     TEXT NOT NULL,
  prompt   TEXT NOT NULL DEFAULT '',
  sort     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, app_id, mode_id, id)
);
"""

_SCHEMA_V10 = """
CREATE TABLE app_audit (
  id          TEXT PRIMARY KEY,
  uid         TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  app_id      TEXT NOT NULL,
  action      TEXT NOT NULL,
  actor       TEXT NOT NULL DEFAULT '',
  before_json TEXT,
  after_json  TEXT,
  created_at  TEXT NOT NULL
);

CREATE INDEX app_audit_uid_app ON app_audit(uid, app_id, created_at DESC);
"""

_SCHEMA_V11 = """
CREATE TABLE market_ratings (
  uid        TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  item_id    TEXT NOT NULL,
  score      INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (uid, kind, item_id)
);
"""

_SCHEMA_V13 = """
CREATE TABLE office_locks (
  doc_id     TEXT PRIMARY KEY,
  uid        TEXT NOT NULL,
  token      TEXT NOT NULL,
  expires_at REAL NOT NULL,
  updated_at TEXT NOT NULL
);
"""

_SCHEMA_V12 = """
CREATE TABLE feedback (
  id                TEXT PRIMARY KEY,
  uid               TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  session_file      TEXT NOT NULL,
  message_timestamp REAL NOT NULL,
  rating             INTEGER NOT NULL,
  comment            TEXT NOT NULL DEFAULT '',
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (uid, session_file, message_timestamp)
);
"""

#: ``(version, sql)`` pairs, applied in order and never edited after release.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
    (5, _SCHEMA_V5),
    (6, _SCHEMA_V6),
    (7, _SCHEMA_V7),
    (8, _SCHEMA_V8),
    (9, _SCHEMA_V9),
    (10, _SCHEMA_V10),
    (11, _SCHEMA_V11),
    (12, _SCHEMA_V12),
    (13, _SCHEMA_V13),
)


#: Columns an API may update on ``users``.  Anything else is rejected rather than
#: silently interpolated into SQL.
_UPDATABLE_USER_COLUMNS = frozenset(
    {
        "display_name",
        "role",
        "status",
        "must_change_password",
        "monthly_token_quota",
        "monthly_cost_quota",
        "password_hash",
        "last_login_at",
    }
)


def utcnow() -> str:
    """Current time as a sortable UTC ISO-8601 string (SQLite has no date type)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_uid() -> str:
    """A tenant id.  Matches the pattern ``config._checked_uid`` accepts."""
    return uuid.uuid4().hex


def month_start(now: datetime | None = None) -> str:
    """First instant of the current UTC month — the quota accounting window."""
    current = now or datetime.now(timezone.utc)
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(
        timespec="seconds"
    )


class DuplicateUsername(Exception):
    """Raised when two accounts would share a login name."""


class DuplicateWorkspace(Exception):
    """Raised when two workspaces would share a name for one user."""


class Database:
    """Every read and write the team server performs on its SQLite file."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # -- plumbing --------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """One write transaction.  Commits on success, rolls back on any exception."""
        with self._lock:
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _write(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.atomic() as conn:
            conn.execute(sql, params)

    def _migrate(self) -> None:
        with self.atomic() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                int(row["version"])
                for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, sql in MIGRATIONS:
                if version in applied:
                    continue
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utcnow()),
                )

    @property
    def schema_version(self) -> int:
        row = self._one("SELECT MAX(version) AS version FROM schema_migrations")
        return int(row["version"]) if row and row["version"] is not None else 0

    # -- users -----------------------------------------------------------------------

    def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        display_name: str = "",
        role: str = "user",
        must_change_password: bool = False,
        monthly_token_quota: int | None = None,
        monthly_cost_quota: float | None = None,
        uid: str | None = None,
    ) -> str:
        """Insert one account and return its uid."""
        user_id = uid or new_uid()
        now = utcnow()
        try:
            self._write(
                "INSERT INTO users (uid, username, display_name, password_hash, role, status,"
                " must_change_password, monthly_token_quota, monthly_cost_quota,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
                (
                    user_id,
                    username,
                    display_name or username,
                    password_hash,
                    role,
                    1 if must_change_password else 0,
                    monthly_token_quota,
                    monthly_cost_quota,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise DuplicateUsername(username) from error
        return user_id

    def get_user(self, uid: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE uid = ?", (uid,))

    def get_user_by_username(self, username: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE username = ?", (username,))

    def list_users(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM users ORDER BY created_at, username")

    def count_users(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def count_active_admins(self) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM users WHERE role IN ('admin', 'superadmin')"
            " AND status = 'active'"
        )
        return int(row["n"]) if row else 0

    def update_user(self, uid: str, **fields: Any) -> None:
        """Update whitelisted columns.  Unknown column names are a programming error."""
        unknown = set(fields) - _UPDATABLE_USER_COLUMNS
        if unknown:
            raise ValueError(f"cannot update users column(s): {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        params = [bool(value) if isinstance(value, bool) else value for value in fields.values()]
        self._write(
            f"UPDATE users SET {assignments}, updated_at = ? WHERE uid = ?",
            (*params, utcnow(), uid),
        )

    def delete_user(self, uid: str) -> None:
        with self.atomic() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE uid = ?", (uid,))
            conn.execute("DELETE FROM users WHERE uid = ?", (uid,))

    def set_password(self, uid: str, password_hash: str, *, must_change: bool = False) -> None:
        self.update_user(uid, password_hash=password_hash, must_change_password=must_change)

    # -- browser sessions ------------------------------------------------------------

    def create_auth_session(
        self, *, token_hash: str, uid: str, expires_at: str, user_agent: str = ""
    ) -> None:
        now = utcnow()
        self._write(
            "INSERT INTO auth_sessions (token_hash, uid, created_at, expires_at, last_seen_at,"
            " user_agent) VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash, uid, now, expires_at, now, user_agent[:255]),
        )

    def get_auth_session(self, token_hash: str) -> sqlite3.Row | None:
        """The session row joined with its account, or ``None`` if it is not usable.

        A revoked, expired or disabled account yields ``None`` here rather than at the
        call site, so there is exactly one place that decides whether a cookie is live.
        """
        row = self._one(
            "SELECT s.token_hash, s.uid, s.expires_at, s.revoked_at, s.last_seen_at,"
            " u.username, u.display_name, u.role, u.status, u.must_change_password,"
            " u.monthly_token_quota, u.monthly_cost_quota"
            " FROM auth_sessions s JOIN users u ON u.uid = s.uid"
            " WHERE s.token_hash = ?",
            (token_hash,),
        )
        if row is None or row["revoked_at"] is not None:
            return None
        if row["expires_at"] <= utcnow() or row["status"] != "active":
            return None
        return row

    def touch_auth_session(self, token_hash: str, *, last_seen_at: str) -> None:
        self._write(
            "UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (last_seen_at, token_hash),
        )

    def revoke_auth_session(self, token_hash: str) -> None:
        self._write(
            "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (utcnow(), token_hash),
        )

    def revoke_user_sessions(self, uid: str) -> None:
        self._write(
            "UPDATE auth_sessions SET revoked_at = ? WHERE uid = ? AND revoked_at IS NULL",
            (utcnow(), uid),
        )

    def purge_expired_sessions(self) -> int:
        with self.atomic() as conn:
            cursor = conn.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (utcnow(),),
            )
            return int(cursor.rowcount or 0)

    # -- session pointers ------------------------------------------------------------

    def remember_session(self, uid: str, session_file: str) -> None:
        """Record which session file a user is working in, so eviction is lossless."""
        if not session_file:
            return
        self._write(
            "INSERT INTO session_pointers (uid, session_file, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(uid) DO UPDATE SET session_file = excluded.session_file,"
            " updated_at = excluded.updated_at",
            (uid, session_file, utcnow()),
        )

    def recall_session(self, uid: str) -> str | None:
        row = self._one("SELECT session_file FROM session_pointers WHERE uid = ?", (uid,))
        return str(row["session_file"]) if row else None

    def forget_session(self, uid: str) -> None:
        self._write("DELETE FROM session_pointers WHERE uid = ?", (uid,))

    # -- usage -----------------------------------------------------------------------

    def record_usage(
        self,
        *,
        uid: str,
        session_id: str,
        provider: str = "",
        model_id: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost: float = 0.0,
        request_kind: str = "turn",
    ) -> None:
        self._write(
            "INSERT INTO usage_records (ts, uid, session_id, provider, model_id, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens, cost, request_kind)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                uid,
                session_id,
                provider,
                model_id,
                int(input_tokens),
                int(output_tokens),
                int(cache_read_tokens),
                int(cache_write_tokens),
                float(cost),
                request_kind,
            ),
        )

    _USAGE_SUMS = (
        "COALESCE(SUM(input_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
        " COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,"
        " COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,"
        " COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,"
        " COALESCE(SUM(cost), 0) AS cost,"
        " COUNT(*) AS turns"
    )

    # -- workspaces ------------------------------------------------------------------

    def ensure_default_workspace(self, uid: str, path: str, name: str = "默认工作空间") -> None:
        """Create the implicit default row and keep its path current.

        The path is stored for display/debugging only; callers still compute it from
        the data root so moving a deployment does not strand the row.
        """
        now = utcnow()
        self._write(
            "INSERT OR IGNORE INTO workspaces"
            " (uid, id, name, path, is_default, created_at, updated_at)"
            " VALUES (?, 'default', ?, ?, 1, ?, ?)",
            (uid, name, path, now, now),
        )
        row = self._one("SELECT path FROM workspaces WHERE uid = ? AND id = 'default'", (uid,))
        if row is not None and str(row["path"]) != path:
            self._write(
                "UPDATE workspaces SET path = ?, updated_at = ? WHERE uid = ? AND id = 'default'",
                (path, now, uid),
            )

    def list_workspaces(self, uid: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT id, name, path, is_default, created_at FROM workspaces"
            " WHERE uid = ? ORDER BY is_default DESC, created_at ASC",
            (uid,),
        )

    def get_workspace(self, uid: str, workspace_id: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT id, name, path, is_default FROM workspaces WHERE uid = ? AND id = ?",
            (uid, workspace_id),
        )

    def create_workspace(self, uid: str, name: str, path: str, workspace_id: str | None = None) -> str:
        workspace = workspace_id or uuid.uuid4().hex[:12]
        now = utcnow()
        try:
            self._write(
                "INSERT INTO workspaces (uid, id, name, path, is_default, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 0, ?, ?)",
                (uid, workspace, name, path, now, now),
            )
        except sqlite3.IntegrityError as error:
            raise DuplicateWorkspace(name) from error
        return workspace

    def rename_workspace(self, uid: str, workspace_id: str, name: str) -> None:
        try:
            self._write(
                "UPDATE workspaces SET name = ?, updated_at = ? WHERE uid = ? AND id = ?",
                (name, utcnow(), uid, workspace_id),
            )
        except sqlite3.IntegrityError as error:
            raise DuplicateWorkspace(name) from error

    def remember_workspace_session(self, uid: str, workspace: str, session_file: str) -> None:
        if not session_file:
            return
        self._write(
            "INSERT INTO workspace_session_pointers (uid, workspace, session_file, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(uid, workspace) DO UPDATE SET session_file = excluded.session_file,"
            " updated_at = excluded.updated_at",
            (uid, workspace, session_file, utcnow()),
        )

    def recall_workspace_session(self, uid: str, workspace: str) -> str | None:
        row = self._one(
            "SELECT session_file FROM workspace_session_pointers"
            " WHERE uid = ? AND workspace = ?",
            (uid, workspace),
        )
        return str(row["session_file"]) if row else None

    # -- apps -------------------------------------------------------------------------

    def seed_apps(self, uid: str, apps: list[dict[str, Any]]) -> None:
        """Install the built-in catalog for a tenant exactly once."""
        row = self._one("SELECT COUNT(*) AS count FROM apps WHERE uid = ?", (uid,))
        if row is not None and int(row["count"]) > 0:
            return
        for app in apps:
            self.replace_app(uid, app, builtin=True)

    def list_apps(self, uid: str) -> list[dict[str, Any]]:
        app_rows = self._query(
            "SELECT * FROM apps WHERE uid = ? ORDER BY sort ASC, created_at ASC", (uid,)
        )
        mode_rows = self._query(
            "SELECT * FROM app_modes WHERE uid = ? ORDER BY sort ASC", (uid,)
        )
        capsule_rows = self._query(
            "SELECT * FROM app_capsules WHERE uid = ? ORDER BY sort ASC", (uid,)
        )
        modes_by_app: dict[str, list[dict[str, Any]]] = {}
        mode_index: dict[tuple[str, str], dict[str, Any]] = {}
        for row in mode_rows:
            skills_raw = row["skills_json"]
            try:
                skills = json.loads(skills_raw) if skills_raw else None
            except (TypeError, json.JSONDecodeError):
                skills = None
            mode = {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "prompt": row["prompt"],
                "skillBinding": "all" if skills is None else skills,
                "capsules": [],
            }
            modes_by_app.setdefault(str(row["app_id"]), []).append(mode)
            mode_index[(str(row["app_id"]), str(row["id"]))] = mode
        for row in capsule_rows:
            mode = mode_index.get((str(row["app_id"]), str(row["mode_id"])))
            if mode is not None:
                mode["capsules"].append(
                    {"id": row["id"], "name": row["name"], "prompt": row["prompt"]}
                )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "accent": row["accent"],
                "builtin": bool(row["builtin"]),
                "modes": modes_by_app.get(str(row["id"]), []),
            }
            for row in app_rows
        ]

    def replace_app(self, uid: str, app: dict[str, Any], *, builtin: bool = False) -> None:
        now = utcnow()
        app_id = str(app["id"])
        with self.atomic() as conn:
            existing = conn.execute(
                "SELECT builtin FROM apps WHERE uid = ? AND id = ?", (uid, app_id)
            ).fetchone()
            is_builtin = 1 if builtin or (existing is not None and existing["builtin"]) else 0
            if existing is None:
                conn.execute(
                    "INSERT INTO apps (uid, id, name, description, accent, builtin, sort, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        uid,
                        app_id,
                        str(app.get("name") or app_id),
                        str(app.get("description") or ""),
                        str(app.get("accent") or "#3b5fba"),
                        is_builtin,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE apps SET name = ?, description = ?, accent = ?, updated_at = ?"
                    " WHERE uid = ? AND id = ?",
                    (
                        str(app.get("name") or app_id),
                        str(app.get("description") or ""),
                        str(app.get("accent") or "#3b5fba"),
                        now,
                        uid,
                        app_id,
                    ),
                )
            conn.execute("DELETE FROM app_modes WHERE uid = ? AND app_id = ?", (uid, app_id))
            conn.execute("DELETE FROM app_capsules WHERE uid = ? AND app_id = ?", (uid, app_id))
            for mode_index, mode in enumerate(app.get("modes") or []):
                mode_id = str(mode.get("id") or f"mode-{mode_index + 1}")
                binding = mode.get("skillBinding", "all")
                if binding == "all" or not isinstance(binding, list):
                    skills_json = None
                else:
                    skills_json = json.dumps([str(item) for item in binding], ensure_ascii=False)
                conn.execute(
                    "INSERT INTO app_modes"
                    " (uid, app_id, id, name, description, prompt, skills_json, sort)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uid,
                        app_id,
                        mode_id,
                        str(mode.get("name") or mode_id),
                        str(mode.get("description") or ""),
                        str(mode.get("prompt") or ""),
                        skills_json,
                        mode_index,
                    ),
                )
                for capsule_index, capsule in enumerate(mode.get("capsules") or []):
                    capsule_id = str(capsule.get("id") or f"capsule-{capsule_index + 1}")
                    conn.execute(
                        "INSERT INTO app_capsules"
                        " (uid, app_id, mode_id, id, name, prompt, sort)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            uid,
                            app_id,
                            mode_id,
                            capsule_id,
                            str(capsule.get("name") or capsule_id),
                            str(capsule.get("prompt") or ""),
                            capsule_index,
                        ),
                    )

    def delete_app(self, uid: str, app_id: str) -> None:
        with self.atomic() as conn:
            conn.execute("DELETE FROM app_capsules WHERE uid = ? AND app_id = ?", (uid, app_id))
            conn.execute("DELETE FROM app_modes WHERE uid = ? AND app_id = ?", (uid, app_id))
            conn.execute("DELETE FROM apps WHERE uid = ? AND id = ?", (uid, app_id))

    def app_composition(self, uid: str, app_id: str, mode_id: str) -> tuple[str, tuple[str, ...] | None] | None:
        row = self._one(
            "SELECT prompt, skills_json FROM app_modes"
            " WHERE uid = ? AND app_id = ? AND id = ?",
            (uid, app_id, mode_id),
        )
        if row is None:
            return None
        try:
            skills = json.loads(row["skills_json"]) if row["skills_json"] else None
        except (TypeError, json.JSONDecodeError):
            skills = None
        return str(row["prompt"]), None if skills is None else tuple(str(item) for item in skills)

    # -- office editor locks ----------------------------------------------------------

    def office_lock(self, doc_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM office_locks WHERE doc_id = ?", (doc_id,))

    def acquire_office_lock(self, doc_id: str, uid: str, token: str, ttl_seconds: int = 120) -> sqlite3.Row:
        """Return the active lock; only replace it when free/expired/owned by uid."""
        now = time.time()
        with self.atomic() as conn:
            row = conn.execute("SELECT * FROM office_locks WHERE doc_id = ?", (doc_id,)).fetchone()
            if row is not None and float(row["expires_at"]) > now and str(row["uid"]) != uid:
                return row
            conn.execute(
                "INSERT INTO office_locks (doc_id, uid, token, expires_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(doc_id) DO UPDATE SET uid=excluded.uid, token=excluded.token,"
                " expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                (doc_id, uid, token, now + ttl_seconds, utcnow()),
            )
            return conn.execute("SELECT * FROM office_locks WHERE doc_id = ?", (doc_id,)).fetchone()

    def release_office_lock(self, doc_id: str, token: str) -> bool:
        with self.atomic() as conn:
            cursor = conn.execute(
                "DELETE FROM office_locks WHERE doc_id = ? AND token = ?", (doc_id, token)
            )
            return cursor.rowcount > 0

    # -- message feedback -------------------------------------------------------------

    def set_feedback(
        self,
        uid: str,
        session_file: str,
        message_timestamp: float,
        rating: int,
        comment: str = "",
    ) -> None:
        now = utcnow()
        self._write(
            "INSERT INTO feedback"
            " (id, uid, session_file, message_timestamp, rating, comment, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(uid, session_file, message_timestamp)"
            " DO UPDATE SET rating = excluded.rating, comment = excluded.comment,"
            " updated_at = excluded.updated_at",
            (
                uuid.uuid4().hex[:12], uid, session_file, float(message_timestamp),
                int(rating), comment[:1000], now, now,
            ),
        )

    def list_feedback(self, uid: str, session_file: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM feedback WHERE uid = ? AND session_file = ?"
            " ORDER BY message_timestamp ASC",
            (uid, session_file),
        )

    # -- market ratings ---------------------------------------------------------------

    def set_market_rating(self, uid: str, kind: str, item_id: str, score: int) -> None:
        self._write(
            "INSERT INTO market_ratings (uid, kind, item_id, score, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(uid, kind, item_id)"
            " DO UPDATE SET score = excluded.score, updated_at = excluded.updated_at",
            (uid, kind, item_id, max(1, min(5, int(score))), utcnow()),
        )

    def market_rating_summary(self, kind: str, item_id: str) -> tuple[float, int]:
        row = self._one(
            "SELECT AVG(score) AS average, COUNT(*) AS count FROM market_ratings"
            " WHERE kind = ? AND item_id = ?",
            (kind, item_id),
        )
        if row is None or not row["count"]:
            return 0.0, 0
        return round(float(row["average"] or 0.0), 2), int(row["count"])

    def my_market_rating(self, uid: str, kind: str, item_id: str) -> int | None:
        row = self._one(
            "SELECT score FROM market_ratings WHERE uid = ? AND kind = ? AND item_id = ?",
            (uid, kind, item_id),
        )
        return int(row["score"]) if row else None

    # -- app audit --------------------------------------------------------------------

    def record_app_audit(
        self,
        uid: str,
        app_id: str,
        action: str,
        actor: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        self._write(
            "INSERT INTO app_audit (id, uid, app_id, action, actor, before_json, after_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex[:12],
                uid,
                app_id,
                action,
                actor,
                None if before is None else json.dumps(before, ensure_ascii=False),
                None if after is None else json.dumps(after, ensure_ascii=False),
                utcnow(),
            ),
        )

    def list_app_audit(self, uid: str, app_id: str, limit: int = 30) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM app_audit WHERE uid = ? AND app_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (uid, app_id, limit),
        )

    # -- notifications ----------------------------------------------------------------

    def create_notification(
        self, uid: str, *, kind: str, title: str, body: str = "", link: str = ""
    ) -> str:
        notification_id = uuid.uuid4().hex[:12]
        self._write(
            "INSERT INTO notifications (id, uid, kind, title, body, link, read, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (notification_id, uid, kind, title, body, link, utcnow()),
        )
        return notification_id

    def list_notifications(self, uid: str, limit: int = 50) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM notifications WHERE uid = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (uid, limit),
        )

    def unread_notifications(self, uid: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS count FROM notifications WHERE uid = ? AND read = 0",
            (uid,),
        )
        return int(row["count"]) if row else 0

    def mark_notification(self, uid: str, notification_id: str) -> None:
        self._write(
            "UPDATE notifications SET read = 1 WHERE uid = ? AND id = ?",
            (uid, notification_id),
        )

    def mark_all_notifications(self, uid: str) -> None:
        self._write("UPDATE notifications SET read = 1 WHERE uid = ? AND read = 0", (uid,))

    # -- schedules --------------------------------------------------------------------

    def create_schedule(
        self,
        *,
        uid: str,
        workspace: str,
        app_id: str,
        mode_id: str,
        name: str,
        prompt: str,
        kind: str,
        next_run_at: str,
        run_at: str | None = None,
        hour: int | None = None,
        minute: int | None = None,
        weekday: int | None = None,
        interval_minutes: int | None = None,
    ) -> str:
        schedule_id = uuid.uuid4().hex[:12]
        now = utcnow()
        self._write(
            "INSERT INTO schedules"
            " (id, uid, workspace, app_id, mode_id, name, prompt, kind, run_at, hour,"
            "  minute, weekday, interval_minutes, enabled, next_run_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                schedule_id, uid, workspace, app_id, mode_id, name, prompt, kind, run_at,
                hour, minute, weekday, interval_minutes, next_run_at, now, now,
            ),
        )
        return schedule_id

    def list_schedules(self, uid: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM schedules WHERE uid = ? ORDER BY created_at DESC", (uid,)
        )

    def get_schedule(self, uid: str, schedule_id: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM schedules WHERE uid = ? AND id = ?", (uid, schedule_id)
        )

    def get_schedule_by_id(self, schedule_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))

    def set_schedule_enabled(self, uid: str, schedule_id: str, enabled: bool, next_run_at: str | None) -> None:
        self._write(
            "UPDATE schedules SET enabled = ?, next_run_at = ?, updated_at = ?"
            " WHERE uid = ? AND id = ?",
            (1 if enabled else 0, next_run_at, utcnow(), uid, schedule_id),
        )

    def delete_schedule(self, uid: str, schedule_id: str) -> None:
        self._write("DELETE FROM schedules WHERE uid = ? AND id = ?", (uid, schedule_id))

    def due_schedules(self, now: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL"
            " AND next_run_at <= ? ORDER BY next_run_at ASC LIMIT 20",
            (now,),
        )

    def mark_schedule_running(self, schedule_id: str, expected_next: str, next_run_at: str | None) -> bool:
        with self.atomic() as conn:
            cursor = conn.execute(
                "UPDATE schedules SET last_status = 'running', last_run_at = ?,"
                " next_run_at = ?, updated_at = ?"
                " WHERE id = ? AND next_run_at = ?",
                (utcnow(), next_run_at, utcnow(), schedule_id, expected_next),
            )
            return cursor.rowcount > 0

    def finish_schedule(
        self, schedule_id: str, *, status: str, error: str | None, next_run_at: str | None, enabled: bool
    ) -> None:
        self._write(
            "UPDATE schedules SET last_status = ?, last_error = ?, next_run_at = ?,"
            " enabled = ?, updated_at = ? WHERE id = ?",
            (status, error, next_run_at, 1 if enabled else 0, utcnow(), schedule_id),
        )

    def start_schedule_run(self, schedule_id: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        self._write(
            "INSERT INTO schedule_runs (id, schedule_id, started_at, status)"
            " VALUES (?, ?, ?, 'running')",
            (run_id, schedule_id, utcnow()),
        )
        return run_id

    def finish_schedule_run(
        self, run_id: str, *, status: str, session_file: str | None, error: str | None
    ) -> None:
        self._write(
            "UPDATE schedule_runs SET finished_at = ?, status = ?, session_file = ?, error = ?"
            " WHERE id = ?",
            (utcnow(), status, session_file, error, run_id),
        )

    def list_user_schedule_runs(self, uid: str, limit: int = 50) -> list[sqlite3.Row]:
        """Recent runs across every schedule of one user, newest first."""
        return self._query(
            "SELECT r.*, s.name AS schedule_name, s.workspace AS schedule_workspace,"
            " s.app_id AS schedule_app_id, s.mode_id AS schedule_mode_id"
            " FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id"
            " WHERE s.uid = ? ORDER BY r.started_at DESC LIMIT ?",
            (uid, max(1, min(200, limit))),
        )

    def list_schedule_runs(self, schedule_id: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM schedule_runs WHERE schedule_id = ?"
            " ORDER BY started_at DESC LIMIT ?",
            (schedule_id, limit),
        )

    # -- plugins ----------------------------------------------------------------------

    def plugin_states(self, uid: str, app_id: str) -> dict[str, bool]:
        rows = self._query(
            "SELECT plugin_id, enabled FROM plugin_enable WHERE uid = ? AND app_id = ?",
            (uid, app_id),
        )
        return {str(row["plugin_id"]): bool(row["enabled"]) for row in rows}

    def enabled_plugins(self, uid: str, app_id: str) -> list[str]:
        return [
            plugin_id
            for plugin_id, enabled in self.plugin_states(uid, app_id).items()
            if enabled
        ]

    def delete_plugin_enable(self, plugin_id: str) -> None:
        self._write("DELETE FROM plugin_enable WHERE plugin_id = ?", (plugin_id,))

    def set_plugin_enabled(self, uid: str, app_id: str, plugin_id: str, enabled: bool) -> None:
        self._write(
            "INSERT INTO plugin_enable (uid, app_id, plugin_id, enabled, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(uid, app_id, plugin_id)"
            " DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (uid, app_id, plugin_id, 1 if enabled else 0, utcnow()),
        )

    # -- composition pointers ---------------------------------------------------------

    def remember_composition_session(
        self, uid: str, workspace: str, app_id: str, mode_id: str, session_file: str
    ) -> None:
        if not session_file:
            return
        self._write(
            "INSERT INTO composition_session_pointers"
            " (uid, workspace, app_id, mode_id, session_file, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(uid, workspace, app_id, mode_id)"
            " DO UPDATE SET session_file = excluded.session_file,"
            " updated_at = excluded.updated_at",
            (uid, workspace, app_id, mode_id, session_file, utcnow()),
        )

    def recall_composition_session(
        self, uid: str, workspace: str, app_id: str, mode_id: str
    ) -> str | None:
        row = self._one(
            "SELECT session_file FROM composition_session_pointers"
            " WHERE uid = ? AND workspace = ? AND app_id = ? AND mode_id = ?",
            (uid, workspace, app_id, mode_id),
        )
        return str(row["session_file"]) if row else None

    # -- skills ----------------------------------------------------------------------

    def skill_prefs(self, uid: str, workspace: str = "default") -> dict[str, bool]:
        """Explicit skill preferences for one user/workspace.

        Absence means "enabled": a newly discovered skill must not require an
        allow-list row before it can be used.
        """
        rows = self._query(
            "SELECT name, enabled FROM skill_prefs WHERE uid = ? AND workspace = ?",
            (uid, workspace),
        )
        return {str(row["name"]): bool(row["enabled"]) for row in rows}

    def disabled_skills(self, uid: str, workspace: str = "default") -> list[str]:
        """Skill names explicitly switched off, for prompt assembly."""
        return [name for name, enabled in self.skill_prefs(uid, workspace).items() if not enabled]

    def set_skill_pref(self, uid: str, name: str, enabled: bool, workspace: str = "default") -> None:
        """Record one toggle; the table is keyed by user/workspace/skill."""
        self._write(
            "INSERT INTO skill_prefs (uid, workspace, name, enabled, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(uid, workspace, name)"
            " DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (uid, workspace, name, 1 if enabled else 0, utcnow()),
        )

    def usage_summary(self, uid: str, since: str) -> dict[str, Any]:
        """One user's usage since ``since`` — the quota check's input."""
        row = self._one(
            f"SELECT {self._USAGE_SUMS} FROM usage_records WHERE uid = ? AND ts >= ?",
            (uid, since),
        )
        return dict(row) if row else {}

    def usage_by_day(self, uid: str, since: str) -> list[dict[str, Any]]:
        rows = self._query(
            f"SELECT substr(ts, 1, 10) AS day, {self._USAGE_SUMS} FROM usage_records"
            " WHERE uid = ? AND ts >= ? GROUP BY day ORDER BY day",
            (uid, since),
        )
        return [dict(row) for row in rows]

    def usage_by_session(self, uid: str, since: str) -> list[dict[str, Any]]:
        rows = self._query(
            f"SELECT session_id, {self._USAGE_SUMS} FROM usage_records"
            " WHERE uid = ? AND ts >= ? GROUP BY session_id ORDER BY cost DESC",
            (uid, since),
        )
        return [dict(row) for row in rows]

    def usage_by_kind(self, uid: str, since: str) -> list[dict[str, Any]]:
        """Split a user's usage into ordinary turns and context compaction."""
        rows = self._query(
            f"SELECT request_kind, {self._USAGE_SUMS} FROM usage_records"
            " WHERE uid = ? AND ts >= ? GROUP BY request_kind ORDER BY request_kind",
            (uid, since),
        )
        return [dict(row) for row in rows]

    def usage_all_users(self, since: str) -> list[dict[str, Any]]:
        """Admin view: every account's usage this window, including those with none."""
        rows = self._query(
            "SELECT u.uid, u.username, u.display_name, u.role, u.status,"
            " u.monthly_token_quota, u.monthly_cost_quota,"
            " COALESCE(SUM(r.input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(r.output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(r.input_tokens + r.output_tokens), 0) AS total_tokens,"
            " COALESCE(SUM(r.cost), 0) AS cost,"
            " COUNT(r.id) AS turns"
            " FROM users u LEFT JOIN usage_records r"
            " ON r.uid = u.uid AND r.ts >= ?"
            " GROUP BY u.uid ORDER BY total_tokens DESC",
            (since,),
        )
        return [dict(row) for row in rows]

    # -- audit -----------------------------------------------------------------------

    def record_audit(
        self,
        *,
        uid: str,
        tool_name: str,
        decision: str,
        session_id: str = "",
        tool_call_id: str = "",
        reason: str = "",
        input_preview: str = "",
        is_error: bool = False,
        duration_ms: int | None = None,
        exit_code: int | None = None,
    ) -> int:
        with self.atomic() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_records (ts, uid, session_id, tool_name, tool_call_id,"
                " decision, reason, input_preview, is_error, duration_ms, exit_code)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    utcnow(),
                    uid,
                    session_id,
                    tool_name,
                    tool_call_id,
                    decision,
                    reason,
                    input_preview,
                    1 if is_error else 0,
                    duration_ms,
                    exit_code,
                ),
            )
            return int(cursor.lastrowid or 0)

    def finish_audit(
        self,
        row_id: int,
        *,
        is_error: bool = False,
        duration_ms: int | None = None,
        exit_code: int | None = None,
    ) -> None:
        """Attach an outcome to a decision already recorded for the same tool call.

        The decision is written before the call runs (a blocked call never produces an
        outcome), so this is an update rather than a second row: one audit row per call.
        """
        if not row_id:
            return
        self._write(
            "UPDATE audit_records SET is_error = ?, duration_ms = ?, exit_code = ? WHERE id = ?",
            (1 if is_error else 0, duration_ms, exit_code, int(row_id)),
        )

    def query_audit(
        self,
        *,
        uid: str | None = None,
        since: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if uid is not None:
            clauses.append("uid = ?")
            params.append(uid)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        rows = self._query(
            f"SELECT * FROM audit_records{where} ORDER BY id DESC LIMIT ? OFFSET ?", params
        )
        return [dict(row) for row in rows]


__all__ = [
    "Database",
    "DuplicateUsername",
    "MIGRATIONS",
    "month_start",
    "new_uid",
    "utcnow",
]
