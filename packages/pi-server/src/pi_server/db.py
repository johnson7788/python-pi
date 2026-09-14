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

import os
import sqlite3
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

#: ``(version, sql)`` pairs, applied in order and never edited after release.
MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _SCHEMA_V1), (2, _SCHEMA_V2))


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
