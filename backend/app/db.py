"""SQLite connections, atomic schema initialization, and non-destructive checks.

No business repository or Agent tool is exposed by this module.
"""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = BACKEND_ROOT / ".local" / "resume-agent.sqlite"
MIGRATIONS = (
    (1, Path(__file__).with_name("migrations") / "001_initial.sql"),
    (2, Path(__file__).with_name("migrations") / "002_request_log.sql"),
    (3, Path(__file__).with_name("migrations") / "003_suggestion_decisions.sql"),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


class SchemaError(RuntimeError):
    """Database cannot safely be used with the supported schema."""


@contextmanager
def database(
    path: str | Path = DEFAULT_DATABASE_PATH, *, read_only: bool = False
) -> Iterator[sqlite3.Connection]:
    if str(path) == ":memory:":
        if read_only:
            raise ValueError("An in-memory database cannot be opened read-only.")
        target = ":memory:"
        uri = False
    else:
        resolved = Path(path).resolve()
        if read_only:
            target = resolved.as_uri() + "?mode=ro"
            uri = True
        else:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            target = str(resolved)
            uri = False
    connection = sqlite3.connect(target, uri=uri, timeout=5, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise SchemaError("Foreign key enforcement is unavailable.")
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A short explicit transaction, including rollback on deferred FK failure."""
    if connection.in_transaction:
        raise SchemaError("Nested transactions require an explicit savepoint.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    # executescript() can commit an existing transaction. Execute complete bundled
    # statements individually so DDL and the version marker remain atomic.
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise SchemaError("Incomplete bundled migration statement.")


def _objects(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    rows = connection.execute(
        "SELECT name, type, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    )
    return {
        row["name"]: (row["type"], " ".join(row["sql"].split()))
        for row in rows
    }


def _versions(connection: sqlite3.Connection, *, allow_pending: bool = False) -> list[int]:
    try:
        versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_version ORDER BY version"
        )]
    except sqlite3.Error as error:
        raise SchemaError("Missing or invalid schema version table; database was not rebuilt.") from error
    if versions and versions[-1] > SCHEMA_VERSION:
        raise SchemaError("Database schema is newer than this application; no downgrade performed.")
    expected = [version for version, _ in MIGRATIONS]
    if not versions or versions != expected[:len(versions)] or (not allow_pending and versions != expected):
        raise SchemaError("Incomplete or unsupported migration history; database was not rebuilt.")
    return versions


def check_database(connection: sqlite3.Connection) -> dict:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SchemaError("Foreign key enforcement is disabled on this connection.")
    versions = _versions(connection)
    with database(":memory:") as reference:
        for _, migration in MIGRATIONS:
            _execute_script(reference, migration.read_text(encoding="utf-8"))
        expected = _objects(reference)
    actual = _objects(connection)
    missing = sorted(expected.keys() - actual.keys())
    changed = sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])
    if missing or changed:
        raise SchemaError(f"Schema mismatch: missing={missing}, changed={changed}; no repair performed.")
    if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
        raise SchemaError("SQLite integrity check failed; database was not rebuilt.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SchemaError("Database contains invalid foreign key references.")
    return {
        "schemaVersion": versions[-1],
        "foreignKeys": True,
        "integrity": "ok",
        "tables": sorted(name for name, (kind, _) in expected.items() if kind == "table"),
    }


def initialize_database(connection: sqlite3.Connection) -> dict:
    # Serialize initialization with other writers; never delete or recreate an
    # existing database to recover from an unknown or incomplete schema.
    with transaction(connection):
        objects = _objects(connection)
        if "schema_version" not in objects:
            if objects:
                raise SchemaError("Unversioned nonempty database; refusing to overwrite it.")
            applied = []
        else:
            applied = _versions(connection, allow_pending=True)
        for version, migration in MIGRATIONS:
            if version not in applied:
                _execute_script(connection, migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
        return check_database(connection)


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize or inspect the local business database.")
    parser.add_argument("command", choices=("init", "check"))
    parser.add_argument("--path", type=Path, default=DEFAULT_DATABASE_PATH,
                        help="Developer-selected database path; not an Agent parameter.")
    args = parser.parse_args()
    try:
        with database(args.path, read_only=args.command == "check") as connection:
            report = initialize_database(connection) if args.command == "init" else check_database(connection)
        print(json.dumps({"ok": True, "data": report}, ensure_ascii=False))
        return 0
    except (sqlite3.Error, SchemaError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": {"code": "database_unavailable", "message": str(error)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
