"""PostgreSQL transport for the repository's deliberately small SQL dialect.

Keep database addresses out of repr/errors. Business writes use one transaction
lock, preserving SQLite's single-writer contract (including project budgets).
"""
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3


WRITE_LOCK = 721493106311


def is_postgres(target):
    return isinstance(target, str) and target.startswith(('postgresql://', 'postgres://'))


def database_target(target):
    return target if is_postgres(target) else Path(target).resolve()


class Row:
    """sqlite3.Row semantics: names, ordinal indexes, iteration over values."""
    def __init__(self, names, values):
        self.names, self.values = names, values

    def __getitem__(self, key):
        return self.values[self.names.index(key)] if isinstance(key, str) else self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def keys(self):
        return self.names


def _row_factory(cursor):
    names = [column.name for column in cursor.description or ()]
    return lambda values: Row(names, values)


def _sql(statement, parameters):
    # Only translate known JSON scalar paths in bundled business queries.
    def extract(match):
        column, key = match.groups()
        value = f"({column}::json ->> '{key}')"
        return value + '::bigint' if key == 'paragraph' else value
    statement = re.sub(r"json_extract\(\s*([\w.]+)\s*,\s*'\$\.(\w+)'\s*\)", extract, statement)
    statement = statement.replace('instr(', 'strpos(').replace('COLLATE BINARY', 'COLLATE "C"')
    statement = re.sub(r"strftime\('%Y-%m-%dT%H:%M:%fZ',\s*'now'\)",
                       "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')", statement)
    # Parameter markers and literal percent signs must be handled separately;
    # question marks inside SQL strings are data, not bind markers.
    if parameters is None:
        return statement
    parts = re.split(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")", statement)
    return ''.join(part.replace('%', '%%') if i % 2 else part.replace('%', '%%').replace('?', '%s')
                   for i, part in enumerate(parts))


class Cursor:
    def __init__(self, raw):
        self.raw = raw

    @property
    def rowcount(self):
        return self.raw.rowcount

    def fetchone(self):
        return self.raw.fetchone() if self.raw.description else None

    def fetchall(self):
        return self.raw.fetchall() if self.raw.description else []

    def __iter__(self):
        return iter(self.raw) if self.raw.description else iter(())


class Connection:
    dialect = 'postgresql'

    def __init__(self, raw):
        self.raw = raw

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self.raw.info.transaction_status != TransactionStatus.IDLE

    def execute(self, statement, parameters=None):
        import psycopg
        try:
            return Cursor(self.raw.execute(_sql(statement, parameters), parameters))
        except psycopg.IntegrityError:
            raise sqlite3.IntegrityError('Database integrity constraint rejected the operation.') from None
        except psycopg.Error:
            # Server diagnostics can contain input data or connection details.
            raise sqlite3.OperationalError('PostgreSQL operation failed; no success confirmed.') from None

    def commit(self):
        import psycopg
        try:
            self.raw.commit()
        except psycopg.IntegrityError:
            raise sqlite3.IntegrityError('Database integrity constraint rejected the commit.') from None
        except psycopg.Error:
            raise sqlite3.OperationalError('PostgreSQL commit failed; no success confirmed.') from None

    def rollback(self):
        self.raw.rollback()


@contextmanager
def connect(target, *, read_only=False):
    try:
        import psycopg
        raw = psycopg.connect(target, autocommit=True, row_factory=_row_factory,
                             connect_timeout=10, prepare_threshold=None)
    except Exception:
        raise sqlite3.OperationalError('PostgreSQL connection unavailable; check private configuration.') from None
    connection = Connection(raw)
    try:
        connection.execute('SET search_path = resume_app, pg_catalog')
        connection.execute("SET statement_timeout = '30s'")
        connection.execute("SET lock_timeout = '5s'")
        if read_only:
            connection.execute('SET default_transaction_read_only = on')
        yield connection
    finally:
        raw.close()


@contextmanager
def transaction(connection):
    from app.db import SchemaError
    if connection.in_transaction:
        raise SchemaError('Nested transactions require an explicit savepoint.')
    connection.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
    try:
        connection.execute('SELECT pg_advisory_xact_lock(?)', (WRITE_LOCK,))
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
