"""Candidate choice never implies accepted evidence or edits the resume."""
import asyncio
import unittest
from types import SimpleNamespace
import httpx

from app import web_candidates as candidates, web_intake
from app.db import database, transaction
from app.repository import RepositoryError
from app.web_provider import WebProviderError, PublicTarget
from app.agent_web import AgentWebBridge, Evidence
from test_web_provider import fetch_response
import test_web_intake_service as fixture


class CandidateTests(unittest.TestCase):
    setUp = fixture.IntakeServiceTests.setUp
    settings = fixture.IntakeServiceTests.settings
    run_context = fixture.IntakeServiceTests.run_context
    prepare = fixture.IntakeServiceTests.prepare
    setup_input = fixture.IntakeServiceTests.setup_input
    discover = fixture.IntakeServiceTests.discover

    def offered(self):
        self.setup_input()
        self.reference = self.discover()['references'][0]
        self.candidate = {'referenceId': self.reference['referenceId'], 'company': '构造公司',
                          'role': '工程实习', 'location': '', 'employmentType': ''}
        with self.repo._operation(self.context, write=True) as (c, s):
            state = web_intake.load(c, self.sid)
            return candidates.offer(c, s, episode_id=state['episodeId'],
                expected_revision=state['revision'], candidates=[self.candidate])

    def prompted(self):
        state = self.offered()
        with self.repo._operation(self.context, write=True) as (c, s):
            c.execute("INSERT INTO messages(message_id,session_id,role,content,input_channel) VALUES ('choice-prompt',?,'assistant',?,'chat')",
                      (self.sid, candidates.question(state)))
            c.execute("INSERT INTO assistant_messages VALUES (?, 'choice-prompt', 'user-link', 'run-test-001', 'question', 'host', ?, ?, ?, '[]')",
                      (self.sid, s['state_revision'], s['draft_revision'], s['current_resume_generation']))
            return candidates.record_prompt(c, s, episode_id=state['episodeId'],
                expected_revision=state['revision'], message_id='choice-prompt')

    def choose(self, state, text='1'):
        with self.repo._operation(self.context, write=True) as (c, s):
            c.execute("INSERT INTO messages(message_id,session_id,role,content,input_channel) VALUES ('choice-answer',?,'user',?,'chat')", (self.sid, text))
            return candidates.select(c, s, episode_id=state['episodeId'],
                expected_revision=state['revision'], user_message_id='choice-answer')

    def test_offer_requires_selection_even_with_one_result(self):
        state = self.offered()
        self.assertEqual(state['phase'], 'awaiting_selection')
        self.assertIsNone(state['selectedCandidateId'])
        self.assertIn('地点未知', candidates.question(state))
        self.assertIn('招聘状态未独立核实', candidates.question(state))

    def test_explicit_selection_records_limited_source_consent_not_acceptance(self):
        state = self.prompted()
        before = self.repo.get_draft(self.user)
        chosen = self.choose(state)
        self.assertEqual(chosen['phase'], 'needs_material')
        self.assertEqual(chosen['selectedCandidateId'], state['candidates'][0]['candidateId'])
        self.assertEqual(chosen['confirmedThirdPartyReferenceIds'], [self.reference['referenceId']])
        with database(self.path) as c:
            self.assertEqual(c.execute('select count(*) from external_assessments').fetchone()[0], 0)
        self.assertEqual(self.repo.get_draft(self.user), before)

    def test_generic_continue_is_not_candidate_selection(self):
        state = self.prompted()
        with self.assertRaises(RepositoryError) as error:
            self.choose(state, '继续')
        self.assertEqual(error.exception.code, 'ambiguous_selection')

    def test_selection_requires_displayed_current_prompt(self):
        state = self.offered()
        with self.assertRaises(RepositoryError) as error:
            self.choose(state)
        self.assertEqual(error.exception.code, 'stale_state')

    def test_candidates_cannot_invent_company_or_reuse_one_source_twice(self):
        state = self.offered()
        for values in [[{**self.candidate, 'company': '伪造公司'}],
                       [self.candidate, self.candidate]]:
            with self.assertRaises(RepositoryError):
                with self.repo._operation(self.context, write=True) as (c, s):
                    candidates.offer(c, s, episode_id=state['episodeId'],
                        expected_revision=state['revision'], candidates=values)

    def test_stale_settings_or_intent_cannot_select(self):
        state = self.prompted()
        with database(self.path) as c, transaction(c):
            c.execute("UPDATE sessions SET direction='另一个方向' WHERE session_id=?", (self.sid,))
        with self.assertRaises(RepositoryError) as error:
            self.choose(state)
        self.assertEqual(error.exception.code, 'stale_state')

    def test_candidate_fetch_uses_persisted_discovery_not_arbitrary_url(self):
        self.offered()
        result = asyncio.run(self.web.fetch_candidate(self.context, self.reference['referenceId'],
            request_id='candidate-body', web_settings_revision=2))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.sent), 2)
        with self.assertRaises(WebProviderError):
            asyncio.run(self.web.fetch_candidate(self.context, 'forged-ref',
                request_id='forged-body', web_settings_revision=2))

    def test_candidate_fetch_survives_turn_scope_change_without_resetting_counts(self):
        self.offered()
        with database(self.path) as c, transaction(c):
            c.execute("UPDATE web_requests SET scope_run_id='previous-turn'")
        asyncio.run(self.web.fetch_candidate(self.context, self.reference['referenceId'],
            request_id='next-turn-body', web_settings_revision=2))
        with database(self.path) as c:
            state = web_intake.load(c, self.sid)
        self.assertEqual(state['searchCount'], 1)
        self.assertEqual(len(state['fetchIdentities']), 1)

    def test_foreign_reference_cannot_be_substituted(self):
        state = self.prompted()
        with self.assertRaises(RepositoryError):
            with self.repo._operation(self.context, write=True) as (c, s):
                candidates.offer(c, s, episode_id=state['episodeId'],
                    expected_revision=state['revision'], candidates=[{**self.candidate, 'referenceId': 'other-session-ref'}])

    def body(self, *, selected=True, text='构造公司 工程实习\n岗位职责：负责接口开发和测试。'):
        state = self.prompted()
        if selected:
            self.choose(state)
        self.web.client.transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=fetch_response(text=text)))
        result = asyncio.run(self.web.fetch_candidate(self.context, self.reference['referenceId'],
            request_id='qualified-body', web_settings_revision=2))
        ref = result['references'][0]
        runtime = SimpleNamespace(repo=self.repo, context=self.context, sid=self.sid,
                                  host=SimpleNamespace(web_service_factory=lambda _: self.web))
        self.bridge = AgentWebBridge(runtime)
        self.body_id = ref['referenceId']
        return Evidence(reference_id=self.body_id, match='matched', company_quote='构造公司',
                        role_quote='工程实习', duties_quote='负责接口开发和测试')

    def test_selected_body_with_only_duties_is_usable_but_not_published(self):
        proof = self.body()
        verdict = self.bridge.intake_body_verdict(PublicTarget('构造公司', '工程实习'), self.body_id, proof)
        self.assertEqual(verdict, 'accepted')
        with database(self.path) as c:
            self.assertEqual(c.execute('select count(*) from external_assessments').fetchone()[0], 0)

    def test_body_without_user_selection_remains_unconfirmed(self):
        proof = self.body(selected=False)
        self.assertEqual(self.bridge.intake_body_verdict(PublicTarget('构造公司', '工程实习'),
            self.body_id, proof), 'unconfirmed')

    def test_fabricated_quote_or_target_change_cannot_qualify_body(self):
        proof = self.body()
        self.assertEqual(self.bridge.intake_body_verdict(PublicTarget('其他公司', '工程实习'),
            self.body_id, proof), 'unconfirmed')
        proof.duties_quote = '网页中根本没有的职责'
        self.assertEqual(self.bridge.intake_body_verdict(PublicTarget('构造公司', '工程实习'),
            self.body_id, proof), 'unconfirmed')

    def test_search_snippet_never_qualifies_as_body(self):
        proof = self.body()
        proof.reference_id = self.reference['referenceId']
        self.assertEqual(self.bridge.intake_body_verdict(PublicTarget('构造公司', '工程实习'),
            proof.reference_id, proof), 'needs_body')
