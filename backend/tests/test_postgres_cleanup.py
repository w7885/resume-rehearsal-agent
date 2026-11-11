"""Driver cleanup faults are sanitized; no live database is used here."""
import sqlite3
import unittest
from unittest.mock import Mock

import psycopg
from app.postgres import Connection, transaction
from psycopg.pq import TransactionStatus


class PostgresCleanupTests(unittest.TestCase):
    def test_lost_connection_on_rollback_becomes_safe_storage_failure(self):
        raw = Mock()
        raw.rollback.side_effect = psycopg.OperationalError('private-database-credentials')
        with self.assertRaises(sqlite3.OperationalError) as caught:
            Connection(raw).rollback()
        self.assertNotIn('private-database', str(caught.exception))
        raw.rollback.assert_called_once_with()
        raw.commit.assert_not_called()

    def test_successful_rollback_and_commit_keep_existing_behavior(self):
        raw = Mock()
        connection = Connection(raw)
        connection.rollback()
        raw.rollback.assert_called_once_with()
        connection.commit()
        raw.commit.assert_called_once_with()

    def test_failed_write_and_cleanup_never_commit_or_retry_the_transaction(self):
        raw = Mock()
        raw.info.transaction_status = TransactionStatus.IDLE
        raw.rollback.side_effect = psycopg.OperationalError('private-cleanup-detail')
        connection = Connection(raw)
        calls = 0
        with self.assertRaises(sqlite3.OperationalError):
            with transaction(connection):
                calls += 1
                raise ValueError('constructed write failure')
        self.assertEqual(calls, 1)
        raw.commit.assert_not_called()
        raw.rollback.assert_called_once_with()
