"""Scoped pasted-resume intake; constructed model answers, never provider calls."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import db
from app.repository import Repository, RepositoryError
import test_agent_integration


RESUME = '# 测试简历\n\n项目经历：整理反馈，协助测试。'


class TextClient(test_agent_integration.FakeClient):
    async def route(self, **kwargs):
        # A material confirmation must never reach the rewrite-decision router.
        content=self.runtime.repo.get_session_context(self.runtime.context)['messages'][-1]['content']
        if content in {RESUME, '将刚才那段文字作为待修改简历。'}:
            raise AssertionError('Material confirmation was routed as an ordinary turn')
        return await super().route(**kwargs)

    async def complete_json(self, *, system, user, run_id='test'):
        value = json.loads(user)
        self.runtime.host.test_calls.append(system)
        if system.startswith('识别用户是否提供'):
            matches = [m for m in value['messages'] if m['content'] == RESUME]
            lines = [line for line in RESUME.splitlines() if line.strip()]
            data = {'status': 'candidate', 'messageId': matches[0]['messageId'], 'resumeStart': lines[0], 'resumeEnd': lines[-1]} if matches else {'status': 'none'}
        elif system.startswith('判断用户是否确认'):
            data = {'status': self.runtime.host.test_choice, 'messageId': self.runtime.host.test_target or value['candidates'][0]['messageId']}
        else:
            return await super().complete_json(system=system, user=user, run_id=run_id)
        return SimpleNamespace(data=data)


class TextIntakeTests(unittest.TestCase):
    setUp = test_agent_integration.AgentIntegrationTests.setUp
    request = test_agent_integration.AgentIntegrationTests.request
    version = test_agent_integration.AgentIntegrationTests.version
    material = test_agent_integration.AgentIntegrationTests.material
    chat = test_agent_integration.AgentIntegrationTests.chat
    send = test_agent_integration.AgentIntegrationTests.send
    count = test_agent_integration.AgentIntegrationTests.count
    analyze = test_agent_integration.AgentIntegrationTests.analyze
    conversation = test_agent_integration.AgentIntegrationTests.conversation
    answer_goal = test_agent_integration.AgentIntegrationTests.answer_goal

    def prepared(self, text=RESUME):
        self.host = self.app.state.agent_host
        self.host.client_factory = lambda runtime, budget: TextClient(runtime)
        self.host.test_calls = []
        self.host.test_choice = 'confirm'
        self.host.test_target = None
        session = self.chat()
        user = self.send(session, text)
        return session, user

    def confirm(self, session):
        prompt = self.conversation(session)['messages'][-1]
        answer = self.send(session, '将刚才那段文字作为待修改简历。', request_id='confirm', replyToMessageId=prompt['messageId'])
        return self.analyze(session, answer, 'confirm-run')

    def test_paste_is_confirmed_then_imported_exactly_and_analysis_resumes(self):
        session, user = self.prepared()
        self.analyze(session, user)
        value = self.conversation(session)
        self.assertIn('已收到简历文字', value['messages'][-1]['content'])
        self.assertEqual(value['draft']['markdown'], '')
        self.assertEqual(self.count('source_blocks'), 0)
        events = self.confirm(session)
        self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user', events)
        self.answer_goal(session, 'confirm-goal')
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], RESUME)
        self.assertEqual(value['messages'][0]['content'], RESUME)
        self.assertEqual(value['messages'][-1]['assistant']['kind'], 'change_question')
        with db.database(self.path, read_only=True) as connection:
            row = connection.execute("SELECT * FROM source_blocks WHERE role='resume'").fetchone()
            self.assertEqual(row['message_id'], user['messageId'])
            self.assertEqual(row['text'], RESUME)
            self.assertEqual(connection.execute("SELECT count(*) FROM source_blocks WHERE role='resume'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM source_blocks WHERE role='supplement'").fetchone()[0], 2)
        source_count = self.count('source_blocks')
        calls = len(self.host.test_calls)
        answer = next(m for m in value['messages'] if m['content'] == '将刚才那段文字作为待修改简历。')
        self.analyze(session, answer, 'replay-confirm')
        self.assertEqual(len(self.host.test_calls), calls)
        self.assertEqual(self.count('source_blocks'), source_count)

    def test_pending_image_does_not_hide_pasted_resume(self):
        session, user = self.prepared()
        repo = self.app.state.business_service.repository
        from app.repository import RequestContext
        repo.save_material(RequestContext(session['sessionId'], self.version(session['sessionId'])),
                             name='test.png', media_type='image/png', original_data=b'constructed-image')
        user = self.send(session, RESUME, request_id='after-image')
        self.analyze(session, user)
        self.assertIn('已收到简历文字', self.conversation(session)['messages'][-1]['content'])
        self.confirm(session)
        self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)

    def test_old_paste_can_be_recovered_by_a_new_turn(self):
        session, user = self.prepared()
        user = self.send(session, '请分析刚才粘贴的简历。', request_id='recover')
        self.analyze(session, user)
        self.confirm(session)
        self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)

    def test_hypothetical_chat_never_becomes_draft(self):
        session, user = self.prepared('如果我做过 API 开发，可以写进简历吗？')
        self.analyze(session, user)
        self.assertEqual(self.count('source_blocks'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')

    def declined(self, choice):
        session, user = self.prepared()
        self.analyze(session, user)
        self.host.test_choice = choice
        self.confirm(session)
        self.assertEqual(self.count('source_blocks'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')

    def test_decline_cannot_import(self):
        self.declined('decline')

    def test_ambiguous_reply_cannot_import(self):
        self.declined('clarify')

    def test_model_cannot_confirm_unoffered_message(self):
        session, user = self.prepared()
        self.analyze(session, user)
        self.host.test_target = 'foreign-message'
        self.confirm(session)
        self.assertEqual(self.count('source_blocks'), 0)

    def test_oversized_text_is_preserved_and_requests_file_upload(self):
        session, user = self.prepared('长' * 20001)
        self.analyze(session, user)
        value = self.conversation(session)
        self.assertIn('Markdown', value['messages'][-1]['content'])
        self.assertEqual(value['messages'][0]['content'], '长' * 20001)
        self.assertEqual(self.count('source_blocks'), 0)

    def test_other_upload_invalidates_old_confirmation(self):
        session, user = self.prepared()
        self.analyze(session, user)
        self.material(session, text='补传资料', role='unknown')
        self.confirm(session)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')

    def test_import_failure_rolls_back_source_and_initial_draft(self):
        session, user = self.prepared()
        self.analyze(session, user)
        with patch.object(Repository, 'create_initial_draft', side_effect=RepositoryError('invalid_input', 'constructed failure')):
            events = self.confirm(session)
        self.assertEqual(events[-1]['payload']['outcome'], 'invalid_input', events)
        self.assertEqual(self.count('source_blocks'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')
