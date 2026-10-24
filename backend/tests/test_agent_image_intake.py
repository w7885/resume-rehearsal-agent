"""Saved-image recognition, classification and replay with no live provider."""
import unittest
from unittest.mock import AsyncMock, patch

from app import db
from app.repository import RequestContext, Repository
import test_agent_integration as integration
from test_agent_material_intake import Classifier, classified, RESUME, JD


class ImageIntakeTests(unittest.TestCase):
    setUp = integration.AgentIntegrationTests.setUp
    request = integration.AgentIntegrationTests.request
    version = integration.AgentIntegrationTests.version
    chat = integration.AgentIntegrationTests.chat
    send = integration.AgentIntegrationTests.send
    analyze = integration.AgentIntegrationTests.analyze
    conversation = integration.AgentIntegrationTests.conversation
    count = integration.AgentIntegrationTests.count

    def prepared(self, count=2):
        session = self.chat()
        repo = self.app.state.business_service.repository
        for i in range(count):
            repo.save_material(RequestContext(session['sessionId'], self.version(session['sessionId'])),
                               name=f'screenshot-{i}.png', media_type='image/png', original_data=f'fixture-{i}'.encode())
        host = self.app.state.agent_host
        host.client_factory = lambda runtime, budget: Classifier(runtime)
        host.classify_inputs, host.classify_output = [], classified
        return session, host

    def enabled(self):
        return patch.dict('os.environ', {'VISION_ENABLED': '1', 'AGENT_BUDGET_CONFIRMED': '1', 'DEEPSEEK_API_KEY': 'synthetic-no-provider'})

    def test_old_saved_images_are_recognized_then_classified_without_reupload(self):
        session, host = self.prepared()
        with self.enabled(), patch('app.vision.transcribe_image', new=AsyncMock(side_effect=[RESUME, JD])) as transcribe:
            user = self.send(session, '识别已保存图片并修改简历')
            events = self.analyze(session, user)
            self.assertEqual(events[-1]['payload']['outcome'], 'completed', events)
            value = self.conversation(session)
            self.assertEqual([m['role'] for m in value['materials']], ['resume', 'jd'])
            self.assertEqual(value['draft']['markdown'], RESUME)
            self.assertEqual(value['session']['directionStatus'], 'provided')
            self.assertFalse(any('这次想投什么岗位' in m['content'] for m in value['messages']))
            self.assertEqual(transcribe.await_count, 2)
            self.assertTrue(all(call.kwargs.get('budget') is not None for call in transcribe.await_args_list))
            self.analyze(session, user, 'replay')
            self.assertEqual(transcribe.await_count, 2)
            self.assertEqual(self.count('attachments'), 2)

    def test_failure_preserves_original_and_only_explicit_retry_recalls_provider(self):
        session, host = self.prepared(1)
        with self.enabled(), patch('app.vision.transcribe_image', new=AsyncMock(return_value=None)) as transcribe:
            user = self.send(session, '处理图片')
            self.analyze(session, user)
            self.assertEqual(transcribe.await_count, 1)
            self.assertEqual(self.conversation(session)['materials'][0]['parseStatus'], 'failed')
            self.assertEqual(self.count('attachments'), 1)
            self.assertEqual(self.count('source_blocks'), 0)
            user = self.send(session, '先看看', request_id='other')
            self.analyze(session, user, 'other')
            self.assertEqual(transcribe.await_count, 1)
            user = self.send(session, '重试识别图片', request_id='retry')
            self.analyze(session, user, 'retry')
            self.assertEqual(transcribe.await_count, 2)

    def test_new_input_during_recognition_rejects_late_result(self):
        session, host = self.prepared(1)
        async def late(*args, **kwargs):
            repo = Repository(self.path)
            current = repo.get_session(RequestContext(session['sessionId']))
            repo.save_message(RequestContext(session['sessionId'], current['stateRevision']), '新要求')
            return RESUME
        with self.enabled(), patch('app.vision.transcribe_image', new=late):
            user = self.send(session, '识别图片')
            events = self.analyze(session, user)
        self.assertEqual(events[-1]['payload']['outcome'], 'stale_state', events)
        self.assertEqual(self.count('source_blocks'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')

    def test_partial_transcript_never_becomes_complete_draft(self):
        session, host = self.prepared(1)
        with self.enabled(), patch('app.vision.transcribe_image', new=AsyncMock(return_value=RESUME+'\n[无法识别]')):
            user = self.send(session, '识别图片')
            self.analyze(session, user)
        value = self.conversation(session)
        self.assertEqual(value['materials'][0]['parseStatus'], 'partial')
        self.assertEqual(value['draft']['markdown'], '')


if __name__ == '__main__':
    unittest.main()
