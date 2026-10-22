"""Deterministic schema tests using synthetic fixtures, never the business DB."""

import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import db


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((WORKSPACE_ROOT / "tests/fixtures/minimal-resume-case.json").read_text(encoding="utf-8"))
TEST_ROOT = (db.BACKEND_ROOT / ".local" / "schema-tests").resolve()


@contextmanager
def temporary_directory():
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="isolated-", dir=TEST_ROOT) as directory:
        resolved = Path(directory).resolve()
        # Verify the automatic recursive cleanup stays inside the test workspace.
        if not resolved.is_relative_to(TEST_ROOT):
            raise RuntimeError("Unexpected test cleanup location")
        yield resolved


def seed_fixture(connection):
    """Load the existing explicitly synthetic A/B case into an isolated DB."""
    session = FIXTURE["session"]["sessionId"]
    with db.transaction(connection):
        connection.execute("INSERT INTO sessions(session_id, direction) VALUES (?, ?)",
                           (session, FIXTURE["session"]["direction"]))
        for material in FIXTURE["materials"]:
            raw = material["text"].encode("utf-8")
            connection.execute(
                "INSERT INTO attachments(attachment_id, session_id, name, media_type, content_hash, "
                "original_data, original_order, role, parse_status, use_status) "
                "VALUES (?, ?, ?, 'text/markdown', ?, ?, ?, ?, 'parsed', 'active')",
                (material["materialId"], session, material["name"], hashlib.sha256(raw).hexdigest(),
                 raw, FIXTURE["materials"].index(material), material["role"]),
            )
        for message in FIXTURE["messages"]:
            connection.execute(
                "INSERT INTO messages(message_id, session_id, role, content, kind) VALUES (?, ?, ?, ?, 'supplement')",
                (message["messageId"], session, message["role"], message["content"]),
            )
        for source in FIXTURE["sources"]:
            origin = source["origin"]
            connection.execute(
                "INSERT INTO source_blocks(source_id, source_revision, session_id, attachment_id, message_id, "
                "role, text, origin_json, line_start, line_end, use_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (source["sourceId"], source["sourceRevision"], session,
                 origin.get("materialId"), origin.get("messageId"), source["role"], source["text"],
                 json.dumps(origin, ensure_ascii=False), origin.get("lineStart"), origin.get("lineEnd")),
            )
        for position, block in enumerate(FIXTURE["draft"]["blocks"]):
            connection.execute(
                "INSERT INTO draft_blocks(block_id, block_revision, session_id, position, markdown, write_kind) "
                "VALUES (?, ?, ?, ?, ?, 'import')",
                (block["blockId"], block["blockRevision"], session, position, block["markdown"]),
            )
            for source in block["sourceRefs"]:
                connection.execute("INSERT INTO draft_sources VALUES (?, ?, ?, ?, ?, ?)",
                                   (session, block["blockId"], block["blockRevision"],
                                    source["sourceId"], source["sourceRevision"], source["quote"]))
        suggestion = FIXTURE["suggestion"]
        connection.execute(
            "INSERT INTO suggestions(suggestion_id, session_id, operation, target_block_id, expected_block_revision, "
            "expected_state_revision, before_text, after_text, reason, gaps_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (suggestion["suggestionId"], session, suggestion["operation"], suggestion["target"]["blockId"],
             suggestion["target"]["expectedBlockRevision"], FIXTURE["session"]["stateRevision"],
             suggestion["before"], suggestion["after"], suggestion["reason"], json.dumps(suggestion["gaps"])),
        )
        for source in suggestion["sourceRefs"]:
            connection.execute("INSERT INTO suggestion_sources VALUES (?, ?, ?, ?, ?)",
                               (session, suggestion["suggestionId"], source["sourceId"],
                                source["sourceRevision"], source["quote"]))
        for issue in FIXTURE["issues"]:
            connection.execute("INSERT INTO issues(issue_id, session_id, kind, summary) VALUES (?, ?, ?, ?)",
                               (issue["issueId"], session, issue["kind"], issue["summary"]))
            for source in issue["sourceRefs"]:
                connection.execute("INSERT INTO issue_sources VALUES (?, ?, ?, ?, ?)",
                                   (session, issue["issueId"], source["sourceId"], source["sourceRevision"], source["quote"]))
            for block in issue["affectedBlockIds"]:
                connection.execute("INSERT INTO issue_blocks VALUES (?, ?, ?, 1)", (session, issue["issueId"], block))
        connection.execute(
            "INSERT INTO runs(run_id, session_id, generation, input_state_revision, resume_generation, status) "
            "VALUES ('run-test-001', ?, 1, 1, 1, 'waiting_user')", (session,),
        )
        connection.execute(
            "INSERT INTO run_events(session_id, run_id, sequence, type, mode, payload_json) "
            "VALUES (?, 'run-test-001', 1, 'done', 'mock', '{}')", (session,),
        )
        connection.execute(
            "INSERT INTO draft_replacements(replacement_id, session_id, request_id, previous_generation, "
            "next_generation, before_markdown, after_markdown, previous_scope_json, next_scope_json, authorization_text) "
            "VALUES ('replacement-test', ?, 'request-test', 1, 2, 'before', 'after', '[]', '[]', 'synthetic authorization')",
            (session,),
        )
    return session


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.manager = db.database(":memory:")
        self.connection = self.manager.__enter__()
        self.addCleanup(self.manager.__exit__, None, None, None)
        db.initialize_database(self.connection)

    def assert_rejected(self, sql, parameters=()):
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction(self.connection):
                self.connection.execute(sql, parameters)

    def test_fixture_roundtrip_keeps_pending_suggestion_out_of_draft(self):
        seed_fixture(self.connection)
        self.assertEqual(self.connection.execute("SELECT markdown FROM draft_blocks").fetchone()[0], FIXTURE["suggestion"]["before"])
        self.assertEqual(self.connection.execute("SELECT status FROM suggestions").fetchone()[0], "pending")
        self.assertEqual(self.connection.execute("SELECT text FROM source_blocks WHERE source_id = ?",
                         ("source-supplement-001",)).fetchone()[0], FIXTURE["sources"][2]["text"])
        self.assertEqual(len(db.check_database(self.connection)["tables"]), 24)

    def test_repeated_initialization_preserves_existing_rows(self):
        seed_fixture(self.connection)
        before = list(self.connection.iterdump())
        db.initialize_database(self.connection)
        db.initialize_database(self.connection)
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_invalid_state_duplicate_key_and_empty_direction_rejected(self):
        session = seed_fixture(self.connection)
        self.assert_rejected("INSERT INTO sessions(session_id, direction) VALUES (?, 'duplicate')", (session,))
        self.assert_rejected("INSERT INTO sessions(session_id, direction) VALUES ('empty', '   ')")
        self.assert_rejected("UPDATE suggestions SET status = 'automatically_saved'")
        self.assert_rejected("UPDATE source_blocks SET source_revision = 0")
        self.assert_rejected("UPDATE attachments SET original_data = 'not a blob'")

    def test_missing_session_rejected_on_each_connection(self):
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assert_rejected("INSERT INTO messages(message_id, session_id, role, content) VALUES ('m', 'missing', 'user', 'x')")
        with db.database(":memory:") as second:
            self.assertEqual(second.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_cross_session_sources_targets_and_events_rejected(self):
        session = seed_fixture(self.connection)
        with db.transaction(self.connection):
            self.connection.execute("INSERT INTO sessions(session_id, direction) VALUES ('other', 'other direction')")
            self.connection.execute("INSERT INTO messages(message_id, session_id, role, content) VALUES ('other-message', 'other', 'user', 'other fact')")
            self.connection.execute("INSERT INTO source_blocks(source_id, source_revision, session_id, message_id, role, text, origin_json) "
                                    "VALUES ('other-source', 1, 'other', 'other-message', 'supplement', 'other fact', '{}')")
            self.connection.execute("INSERT INTO draft_blocks(block_id, block_revision, session_id, position, markdown, write_kind) "
                                    "VALUES ('other-block', 1, 'other', 0, 'other fact', 'import')")
        self.assert_rejected("INSERT INTO source_blocks(source_id, source_revision, session_id, message_id, role, text, origin_json) "
                             "VALUES ('bad-source', 1, ?, 'other-message', 'supplement', 'x', '{}')", (session,))
        self.assert_rejected("INSERT INTO suggestion_sources VALUES (?, ?, 'other-source', 1, 'other fact')",
                             (session, FIXTURE["suggestion"]["suggestionId"]))
        self.assert_rejected("INSERT INTO suggestions(suggestion_id, session_id, operation, target_block_id, expected_block_revision, "
                             "expected_state_revision, before_text, after_text, reason) VALUES ('bad', ?, 'replace', 'other-block', 1, 1, 'x', 'y', 'z')", (session,))
        self.assert_rejected("INSERT INTO run_events(session_id, run_id, sequence, type, mode, payload_json) "
                             "VALUES ('other', 'run-test-001', 2, 'done', 'mock', '{}')")

    def test_missing_source_and_wrong_source_version_rejected(self):
        session = seed_fixture(self.connection)
        for source, revision in [("does-not-exist", 1), ("source-resume-001", 99)]:
            with self.subTest(source=source, revision=revision):
                self.assert_rejected("INSERT INTO suggestion_sources VALUES (?, ?, ?, ?, 'quote')",
                                     (session, FIXTURE["suggestion"]["suggestionId"], source, revision))

    def test_source_snapshot_preserved_after_new_revision(self):
        seed_fixture(self.connection)
        self.assert_rejected("UPDATE source_blocks SET text = 'silently changed'")
        with db.transaction(self.connection):
            self.connection.execute("UPDATE source_blocks SET use_status = 'history' WHERE source_id = 'source-resume-001'")
            self.connection.execute("INSERT INTO source_blocks(source_id, source_revision, session_id, attachment_id, role, text, origin_json, use_status) "
                                    "SELECT source_id, 2, session_id, attachment_id, role, 'corrected text', origin_json, 'active' "
                                    "FROM source_blocks WHERE source_id = 'source-resume-001' AND source_revision = 1")
        historical = self.connection.execute(
            "SELECT b.text FROM suggestion_sources s JOIN source_blocks b "
            "ON (s.session_id, s.source_id, s.source_revision) = (b.session_id, b.source_id, b.source_revision) "
            "WHERE s.source_id = 'source-resume-001'"
        ).fetchone()[0]
        self.assertEqual(historical, FIXTURE["sources"][0]["text"])

    def test_delete_session_cascades_only_its_own_records(self):
        session = seed_fixture(self.connection)
        with db.transaction(self.connection):
            self.connection.execute("INSERT INTO sessions(session_id, direction) VALUES ('keep', 'keep')")
            self.connection.execute("INSERT INTO messages(message_id, session_id, role, content) VALUES ('keep-message', 'keep', 'user', 'keep')")
            self.connection.execute("DELETE FROM sessions WHERE session_id = ?", (session,))
        self.assertEqual(self.connection.execute("SELECT session_id FROM sessions").fetchone()[0], "keep")
        self.assertEqual(self.connection.execute("SELECT message_id FROM messages").fetchone()[0], "keep-message")
        for table in db.check_database(self.connection)["tables"]:
            if table not in ("sessions", "schema_version", "messages"):
                self.assertEqual(self.connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0], 0, table)

    def test_transaction_failure_rolls_back_earlier_writes(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction(self.connection):
                self.connection.execute("INSERT INTO sessions(session_id, direction) VALUES ('rollback', 'x')")
                self.connection.execute("INSERT INTO messages(message_id, session_id, role, content) VALUES ('bad', 'missing', 'user', 'x')")
        self.assertIsNone(self.connection.execute("SELECT 1 FROM sessions WHERE session_id = 'rollback'").fetchone())

    def test_failed_initialization_rolls_back_schema_and_version(self):
        real_execute = db._execute_script
        with db.database(":memory:") as empty:
            with patch.object(db, "_execute_script", side_effect=lambda connection, _: real_execute(
                connection, "CREATE TABLE partial(value TEXT);\nINVALID SQL;\n"
            )):
                with self.assertRaises(sqlite3.OperationalError):
                    db.initialize_database(empty)
            self.assertEqual(empty.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
            db.initialize_database(empty)

    def test_unversioned_database_is_not_overwritten(self):
        with db.database(":memory:") as legacy:
            legacy.execute("CREATE TABLE original(value TEXT)")
            legacy.execute("INSERT INTO original VALUES ('keep me')")
            with self.assertRaises(db.SchemaError):
                db.initialize_database(legacy)
            self.assertEqual(legacy.execute("SELECT value FROM original").fetchone()[0], "keep me")

    def test_missing_table_or_constraint_index_detected_without_repair(self):
        self.connection.execute("INSERT INTO sessions(session_id, direction) VALUES ('keep', 'x')")
        self.connection.execute("DROP TABLE run_events")
        self.connection.execute("DROP INDEX runs_one_active")
        for operation in (db.check_database, db.initialize_database):
            with self.assertRaisesRegex(db.SchemaError, "Schema mismatch"):
                operation(self.connection)
        self.assertEqual(self.connection.execute("SELECT direction FROM sessions").fetchone()[0], "x")
        self.assertIsNone(self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='run_events'").fetchone())

    def test_newer_version_not_downgraded(self):
        self.connection.execute("INSERT INTO schema_version(version) VALUES (?)", (db.SCHEMA_VERSION + 1,))
        with self.assertRaisesRegex(db.SchemaError, "newer"):
            db.initialize_database(self.connection)
        self.assertEqual(self.connection.execute("SELECT max(version) FROM schema_version").fetchone()[0], db.SCHEMA_VERSION + 1)

    def test_only_one_running_agent_run_per_session(self):
        session = seed_fixture(self.connection)
        with db.transaction(self.connection):
            self.connection.execute("UPDATE runs SET status='running'")
        self.assert_rejected("INSERT INTO runs(run_id, session_id, generation, input_state_revision, resume_generation, status) "
                             "VALUES ('second-run', ?, 2, 1, 1, 'running')", (session,))

    def test_file_reopen_preserves_original_bytes_and_rows(self):
        with temporary_directory() as directory:
            path = directory / "roundtrip.sqlite"
            with db.database(path) as connection:
                db.initialize_database(connection)
                seed_fixture(connection)
            with db.database(path, read_only=True) as reopened:
                db.check_database(reopened)
                raw = reopened.execute("SELECT original_data FROM attachments WHERE attachment_id = ?",
                                       (FIXTURE["materials"][0]["materialId"],)).fetchone()[0]
                self.assertEqual(raw, FIXTURE["materials"][0]["text"].encode("utf-8"))
                self.assertEqual(reopened.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_readonly_check_does_not_create_or_repair_files(self):
        with temporary_directory() as directory:
            missing = directory / "not-created" / "missing.sqlite"
            with self.assertRaises(sqlite3.OperationalError):
                with db.database(missing, read_only=True):
                    self.fail("Missing file unexpectedly opened")
            self.assertFalse(missing.parent.exists())
            corrupt = directory / "corrupt.sqlite"
            raw = b"not a SQLite database"
            corrupt.write_bytes(raw)
            with self.assertRaises((sqlite3.DatabaseError, db.SchemaError)):
                with db.database(corrupt, read_only=True) as connection:
                    db.check_database(connection)
            self.assertEqual(corrupt.read_bytes(), raw)

    def test_ordered_upgrade_preserves_rows_and_is_idempotent(self):
        seed_fixture(self.connection)
        next_version = db.SCHEMA_VERSION + 1
        with temporary_directory() as directory:
            upgrade = directory / "future_probe.sql"
            upgrade.write_text("CREATE TABLE upgrade_probe(value TEXT) STRICT;\n", encoding="utf-8")
            with patch.object(db, "MIGRATIONS", (*db.MIGRATIONS, (next_version, upgrade))), patch.object(db, "SCHEMA_VERSION", next_version):
                report = db.initialize_database(self.connection)
                self.assertEqual(report["schemaVersion"], next_version)
                self.assertIn("upgrade_probe", report["tables"])
                self.assertEqual(self.connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
                before = list(self.connection.iterdump())
                db.initialize_database(self.connection)
                self.assertEqual(list(self.connection.iterdump()), before)

    def test_failed_upgrade_preserves_previous_schema_version_and_rows(self):
        seed_fixture(self.connection)
        before = list(self.connection.iterdump())
        version = db.SCHEMA_VERSION
        with temporary_directory() as directory:
            upgrade = directory / "future_broken.sql"
            upgrade.write_text("CREATE TABLE partial_upgrade(value TEXT);\nINVALID SQL;\n", encoding="utf-8")
            with patch.object(db, "MIGRATIONS", (*db.MIGRATIONS, (version + 1, upgrade))), patch.object(db, "SCHEMA_VERSION", version + 1):
                with self.assertRaises(sqlite3.OperationalError):
                    db.initialize_database(self.connection)
        self.assertEqual(list(self.connection.iterdump()), before)
        self.assertEqual(db.check_database(self.connection)["schemaVersion"], version)

    def test_real_v1_migration_to_current_preserves_existing_data(self):
        with db.database(":memory:") as old:
            initial = (db.MIGRATIONS[0],)
            with patch.object(db, "MIGRATIONS", initial), patch.object(db, "SCHEMA_VERSION", 1):
                db.initialize_database(old)
                seed_fixture(old)
            report = db.initialize_database(old)
            self.assertEqual(report["schemaVersion"], db.SCHEMA_VERSION)
            self.assertEqual(old.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(old.execute("SELECT content FROM messages").fetchone()[0], FIXTURE["messages"][0]["content"])

    def test_real_v2_migration_preserves_pending_draft_and_idempotency_cache(self):
        with db.database(":memory:") as old:
            migrations = db.MIGRATIONS[:2]
            with patch.object(db, "MIGRATIONS", migrations), patch.object(db, "SCHEMA_VERSION", 2):
                db.initialize_database(old)
                session_id = seed_fixture(old)
                old.execute("INSERT INTO request_log(scope_key, request_id, operation, request_hash, session_id, response_json, expires_at) VALUES ('scope', 'request', 'test', 'hash', ?, '{}', 9999999999)", (session_id,))
            before = [tuple(row) for row in old.execute("SELECT * FROM draft_blocks")]
            report = db.initialize_database(old)
            self.assertEqual(report["schemaVersion"], db.SCHEMA_VERSION)
            self.assertEqual([tuple(row) for row in old.execute("SELECT * FROM draft_blocks")], before)
            self.assertEqual(old.execute("SELECT count(*) FROM request_log").fetchone()[0], 1)
            self.assertEqual(old.execute("SELECT status FROM suggestions").fetchone()[0], "pending")
            self.assertEqual(old.execute("SELECT count(*) FROM suggestion_decisions").fetchone()[0], 0)

    def test_application_startup_initializes_database_without_model(self):
        from app.main import app, get_health
        with temporary_directory() as directory:
            path = directory / "startup.sqlite"

            async def startup():
                async with app.router.lifespan_context(app):
                    with db.database(path, read_only=True) as connection:
                        db.check_database(connection)
                    self.assertEqual(get_health()["status"], "ok")

            with patch("app.main.DEFAULT_DATABASE_PATH", path):
                asyncio.run(startup())


if __name__ == "__main__":
    unittest.main()
