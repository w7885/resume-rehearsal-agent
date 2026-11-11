"""Transport reuse must never cache transactions or replay business writes."""
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from psycopg.pq import TransactionStatus
from app import postgres


class Raw:
    def __init__(self):
        self.closed = self.broken = False
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.statements = []
        self.fail_ping = False

    def execute(self, sql, parameters=None):
        self.statements.append(sql)
        if sql == 'SELECT 1' and self.fail_ping:
            raise psycopg.OperationalError('private target')
        return SimpleNamespace(fetchone=lambda: (1,), description=True)

    def close(self):
        self.closed = True


class PoolTests(unittest.TestCase):
    def setUp(self):
        postgres.close_idle_connections()
        self.raws = []
        def create(*args, **kwargs):
            raw = Raw()
            self.raws.append(raw)
            return raw
        self.patch = patch('psycopg.connect', side_effect=create)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(postgres.close_idle_connections)

    def test_reuses_idle_transport_and_initializes_once(self):
        with postgres.connect('test-target') as first:
            raw = first.raw
        with postgres.connect('test-target') as second:
            self.assertIs(second.raw, raw)
        self.assertEqual(len(self.raws), 1)
        self.assertEqual(raw.statements.count('SELECT 1'), 1)
        self.assertEqual(sum(s.startswith('SET ') for s in raw.statements), 3)

    def test_profiles_and_targets_are_separate(self):
        with postgres.connect('a', read_only=True) as reader:
            read_raw = reader.raw
        with postgres.connect('a') as writer:
            self.assertIsNot(writer.raw, read_raw)
        with postgres.connect('b') as other:
            self.assertIsNot(other.raw, read_raw)
        self.assertEqual(len(self.raws), 3)
        self.assertIn('SET default_transaction_read_only = on', read_raw.statements)
        self.assertNotIn('SET default_transaction_read_only = on', self.raws[1].statements)

    def test_overlapping_scopes_never_share_transport(self):
        with postgres.connect('a') as first:
            with postgres.connect('a') as second:
                self.assertIsNot(first.raw, second.raw)

    def test_open_or_failed_transactions_are_discarded(self):
        for status in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
            with postgres.connect('a') as connection:
                raw = connection.raw
                raw.info.transaction_status = status
            self.assertTrue(raw.closed)
            with postgres.connect('a') as replacement:
                self.assertIsNot(replacement.raw, raw)

    def test_dead_idle_transport_is_replaced_before_business_work(self):
        with postgres.connect('a') as connection:
            raw = connection.raw
        raw.fail_ping = True
        with postgres.connect('a') as replacement:
            self.assertIsNot(replacement.raw, raw)
            replacement.execute('INSERT example')
        self.assertTrue(raw.closed)
        self.assertNotIn('INSERT example', raw.statements)
        self.assertEqual(self.raws[1].statements.count('INSERT example'), 1)

    def test_business_failure_is_not_replayed(self):
        with self.assertRaises(sqlite3.OperationalError):
            with postgres.connect('a') as connection:
                connection.raw.broken = True
                raise sqlite3.OperationalError('constructed business failure')
        self.assertEqual(len(self.raws), 1)
        self.assertTrue(self.raws[0].closed)


class ModelModeTests(unittest.TestCase):
    def test_new_models_disable_thinking_without_changing_json_or_token_cap(self):
        from app.agent.llm import DeepSeekClient, LLMSettings
        for model in ('deepseek-flash', 'deepseek-v4-pro', 'deepseek-chat'):
            payload = DeepSeekClient(LLMSettings(api_key='fake', model=model))._payload('s', 'u', 1234)
            self.assertEqual(payload['max_tokens'], 1234)
            self.assertEqual(payload['response_format'], {'type': 'json_object'})
            if model == 'deepseek-chat':
                self.assertNotIn('thinking', payload)
            else:
                self.assertEqual(payload['thinking'], {'type': 'disabled'})
