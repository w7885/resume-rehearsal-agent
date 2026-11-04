"""Offline discovery and user-bound URL reads via a scripted provider."""
import asyncio
import unittest

from app.db import database, transaction
from app.repository import RepositoryError
from app.web_provider import PublicSearchIntent, WebProviderError
from app import web_intake
import test_web_search as fixture


class IntakeServiceTests(unittest.TestCase):
    setUp = fixture.WebSearchTests.setUp
    settings = fixture.WebSearchTests.settings
    run_context = fixture.WebSearchTests.run_context
    prepare = fixture.WebSearchTests.prepare

    def setup_input(self, text='请读取 https://example.com/jobs/1'):
        self.prepare()
        with database(self.path) as c, transaction(c):
            c.execute("INSERT INTO messages(message_id,session_id,role,content,input_channel) VALUES ('user-link',?,'user',?,'chat')", (self.sid, text))
            c.execute("UPDATE sessions SET direction='构造公司 工程实习 广州',direction_status='provided' WHERE session_id=?", (self.sid,))

    def bindings(self):
        return self.web.bind_user_jd_links(self.context, kind='message', source_id='user-link')

    def read(self, binding, key='read-1'):
        return asyncio.run(self.web.fetch_user_jd(self.context, binding,
            request_id=key, web_settings_revision=2))

    def discover(self, key='discover-1', intent=None):
        return asyncio.run(self.web.discover(self.context,
            intent or PublicSearchIntent(role='工程实习'), request_id=key, web_settings_revision=2))

    def test_user_url_fetch_does_not_require_company_or_role(self):
        self.setup_input()
        binding = self.bindings()[0]
        result = self.read(binding['bindingId'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['Url'], 'https://example.com/jobs/1')
        self.assertEqual(result['assessment'], 'target_match_and_freshness_unconfirmed')
        replay = self.read(binding['bindingId'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(self.sent), 1)

    def test_discovery_uses_only_anchored_public_fields(self):
        self.setup_input()
        result = self.discover()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.sent[0]['Query'], '工程实习')
        with self.assertRaises(WebProviderError) as error:
            self.discover('invented', PublicSearchIntent(company='不存在公司', role='工程实习'))
        self.assertEqual(error.exception.code, 'unanchored_public_intent')
        self.assertEqual(len(self.sent), 1)

    def test_company_without_direction_and_private_query_are_rejected(self):
        for values in [{'company': '构造公司'}, {'role': 'private@example.com'},
                       {'role': '工程实习', 'location': '13800138000'}]:
            with self.assertRaises(WebProviderError):
                PublicSearchIntent(**values)

    def test_forged_binding_and_assistant_input_cannot_fetch(self):
        self.setup_input()
        with self.assertRaises(RepositoryError):
            self.read('invented')
        with database(self.path) as c, transaction(c):
            c.execute("UPDATE messages SET role='assistant' WHERE message_id='user-link'")
        with self.assertRaises(RepositoryError):
            self.bindings()
        self.assertEqual(self.sent, [])

    def test_changed_or_withdrawn_message_invalidates_saved_binding(self):
        self.setup_input()
        binding = self.bindings()[0]['bindingId']
        with database(self.path) as c, transaction(c):
            c.execute("UPDATE messages SET use_status='history' WHERE message_id='user-link'")
        with self.assertRaises(RepositoryError):
            self.read(binding)
        self.assertEqual(self.sent, [])

    def test_unsafe_link_rejected_before_any_provider_call(self):
        self.setup_input('请读取 http://127.0.0.1/private')
        with self.assertRaises(WebProviderError):
            self.bindings()
        self.assertEqual(self.sent, [])

    def test_existing_search_result_fetch_still_rejects_undiscovered_url(self):
        self.setup_input()
        with self.assertRaises(WebProviderError) as error:
            asyncio.run(self.web.fetch(self.context, self.target, 'https://example.com/jobs/1',
                request_id='unbound', web_settings_revision=2))
        self.assertEqual(error.exception.code, 'undiscovered_url')
        self.assertEqual(self.sent, [])

    def test_discovery_and_user_fetch_share_episode_and_limits(self):
        self.setup_input()
        self.discover()
        self.read(self.bindings()[0]['bindingId'])
        self.discover('discover-2')
        with self.assertRaises(RepositoryError) as error:
            self.discover('discover-3')
        self.assertEqual(error.exception.code, 'web_limit')
        with database(self.path) as c:
            state = web_intake.load(c, self.sid)
        self.assertEqual(state['searchCount'], 2)
        self.assertEqual(len(state['fetchIdentities']), 1)
        self.assertEqual(len(self.sent), 3)

    def test_duplicate_page_cannot_be_refetched_with_new_request_id(self):
        self.setup_input()
        binding = self.bindings()[0]['bindingId']
        self.read(binding)
        with self.assertRaises(RepositoryError) as error:
            self.read(binding, 'read-again')
        self.assertEqual(error.exception.code, 'web_duplicate_page')
        self.assertEqual(len(self.sent), 1)

    def test_atomic_request_failure_does_not_claim_attempt(self):
        self.setup_input()
        self.discover()
        with database(self.path) as c:
            before = web_intake.load(c, self.sid)
        with self.assertRaises(RepositoryError):
            self.discover('discover-1', PublicSearchIntent(company='构造公司', role='工程实习'))
        with database(self.path) as c:
            after = web_intake.load(c, self.sid)
        self.assertEqual(before, after)

    def test_new_run_does_not_replenish_episode_allowance(self):
        self.setup_input()
        self.discover()
        self.discover('discover-2')
        with database(self.path) as c, transaction(c):
            c.execute("UPDATE web_requests SET scope_run_id='historical-turn'")
        with self.assertRaises(RepositoryError) as error:
            self.discover('next-turn')
        self.assertEqual(error.exception.code, 'web_limit')
        self.assertEqual(len(self.sent), 2)
