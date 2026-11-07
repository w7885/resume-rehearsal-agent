"""Atomic, non-destructive PostgreSQL schema baseline and drift checks."""
import hashlib
import json
from pathlib import Path

from app.db import MIGRATIONS, SCHEMA_VERSION, SchemaError, transaction


SCHEMA = 'resume_app'
BASELINE = Path(__file__).with_name('migrations') / 'postgres_015.sql'


def _tables(connection):
    return [row[0] for row in connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname=? ORDER BY tablename", (SCHEMA,))]


def _signature(connection):
    # Include column types/defaults, constraints, indexes, trigger enablement and
    # function bodies. A version row alone cannot prove protection still exists.
    queries = (
        """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
            a.attidentity,pg_get_expr(d.adbin,d.adrelid)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
            LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
            WHERE n.nspname=? AND c.relkind='r' AND c.relname<>'app_schema_contract'
            ORDER BY c.relname,a.attnum""",
        """SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
            FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=? AND c.relname<>'app_schema_contract' ORDER BY c.relname,k.conname""",
        """SELECT tablename,indexname,indexdef FROM pg_indexes
            WHERE schemaname=? AND tablename<>'app_schema_contract' ORDER BY tablename,indexname""",
        """SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid)
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=? AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""",
        """SELECT p.proname,pg_get_functiondef(p.oid) FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname=? ORDER BY p.proname""",
    )
    structure = [[list(row) for row in connection.execute(sql, (SCHEMA,))] for sql in queries]
    return hashlib.sha256(json.dumps(structure, ensure_ascii=True).encode()).hexdigest()


def check(connection, *, allow_pending=False):
    tables = _tables(connection)
    if 'schema_version' not in tables or 'app_schema_contract' not in tables:
        raise SchemaError('Missing PostgreSQL schema contract; no repair performed.')
    versions = [row[0] for row in connection.execute('SELECT version FROM schema_version ORDER BY version')]
    expected = [version for version, _ in MIGRATIONS]
    if versions != expected and not (allow_pending and versions == expected[:15]):
        raise SchemaError('Unsupported PostgreSQL migration history; no downgrade or repair performed.')
    row = connection.execute('SELECT baseline_hash,structure_hash FROM app_schema_contract WHERE singleton=1').fetchone()
    if row is None or row[0] != hashlib.sha256(BASELINE.read_bytes()).hexdigest() or row[1] != _signature(connection):
        raise SchemaError('PostgreSQL schema mismatch; no automatic repair performed.')
    return {'schemaVersion': versions[-1], 'foreignKeys': True, 'integrity': 'schema_checked',
            'tables': [name for name in tables if name != 'app_schema_contract'], 'dialect': 'postgresql'}


def initialize(connection):
    with transaction(connection):
        connection.execute('CREATE SCHEMA IF NOT EXISTS resume_app')
        connection.execute('REVOKE ALL ON SCHEMA resume_app FROM PUBLIC')
        tables = _tables(connection)
        if not tables:
            # Raw SQL is trusted, bundled DDL; no dynamic values or credentials.
            connection.execute(BASELINE.read_text(encoding='utf-8'))
            for version, _ in MIGRATIONS[:15]:
                connection.execute('INSERT INTO schema_version(version) VALUES (?)', (version,))
            connection.execute('''CREATE TABLE app_schema_contract (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                baseline_hash TEXT NOT NULL, structure_hash TEXT NOT NULL)''')
            connection.execute('INSERT INTO app_schema_contract VALUES (1,?,?)',
                               (hashlib.sha256(BASELINE.read_bytes()).hexdigest(), _signature(connection)))
            connection.execute('REVOKE ALL ON ALL TABLES IN SCHEMA resume_app FROM PUBLIC')
            connection.execute('REVOKE ALL ON ALL SEQUENCES IN SCHEMA resume_app FROM PUBLIC')
            connection.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA resume_app FROM PUBLIC')
        elif 'schema_version' not in tables:
            raise SchemaError('Unversioned nonempty PostgreSQL schema; refusing to overwrite it.')
        check(connection, allow_pending=True)
        applied = connection.execute('SELECT max(version) FROM schema_version').fetchone()[0]
        for version, migration in MIGRATIONS[15:]:
            if version > applied:
                connection.execute(migration.read_text(encoding='utf-8'))
                connection.execute('INSERT INTO schema_version(version) VALUES (?)', (version,))
                connection.execute('UPDATE app_schema_contract SET structure_hash=? WHERE singleton=1', (_signature(connection),))
        return check(connection)
