"""Schema migrations: ordered, idempotent, and equivalent however they are applied.

`Database.__init__` runs every migration that has not been applied yet, so the only
thing that can go wrong silently is the *list*: a duplicate version number, a gap, or
an edit to a released migration.  These tests pin the list, and prove that a database
upgraded in two steps ends up byte-identical (schema-wise) to one created at once.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pi_server.db import MIGRATIONS, Database


def schema_dump(path: Path) -> list[tuple[str, str, str]]:
    """Normalised ``sqlite_master`` rows: the schema, without the migration table."""
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND name != 'schema_migrations' "
            "ORDER BY type, name"
        ).fetchall()
    finally:
        connection.close()
    return [(kind, name, " ".join(str(sql).split())) for kind, name, sql in rows]


def test_migration_versions_start_at_one_and_never_repeat():
    versions = [version for version, _sql in MIGRATIONS]

    assert versions[0] == 1
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    # A gap is not itself wrong, but it means a migration was deleted rather than
    # superseded, which silently changes what a fresh install looks like.
    assert versions == list(range(1, len(versions) + 1))


def test_a_fresh_database_records_every_version(tmp_path: Path) -> None:
    database = Database(str(tmp_path / "fresh.db"))
    try:
        assert database.schema_version == MIGRATIONS[-1][0]
    finally:
        database.close()


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "twice.db"
    first = Database(str(path))
    try:
        before = schema_dump(path)
    finally:
        first.close()

    second = Database(str(path))
    try:
        assert second.schema_version == MIGRATIONS[-1][0]
        assert schema_dump(path) == before
    finally:
        second.close()


def test_an_incremental_upgrade_matches_a_full_install(tmp_path: Path) -> None:
    """The realistic failure: an old install that only now gets the new migrations."""
    full_path = tmp_path / "full.db"
    Database(str(full_path)).close()

    stepped_path = tmp_path / "stepped.db"
    connection = sqlite3.connect(str(stepped_path))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        # Stop halfway, exactly as a server from an older release would have.
        halfway = len(MIGRATIONS) // 2
        for version, sql in MIGRATIONS[:halfway]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, "2024-01-01T00:00:00.000Z"),
            )
        connection.commit()
    finally:
        connection.close()

    upgraded = Database(str(stepped_path))
    try:
        assert upgraded.schema_version == MIGRATIONS[-1][0]
    finally:
        upgraded.close()

    assert schema_dump(stepped_path) == schema_dump(full_path)


def test_every_migration_is_a_valid_script_on_its_own(tmp_path: Path) -> None:
    """A syntax error in migration N is otherwise found only by a real upgrade."""
    connection = sqlite3.connect(":memory:")
    try:
        for version, sql in MIGRATIONS:
            try:
                connection.executescript(sql)
            except sqlite3.Error as error:  # pragma: no cover - the assertion is the message
                pytest.fail(f"migration {version} failed: {error}")
    finally:
        connection.close()
