"""State-aware courtesy without model calls, edits or accidental decisions."""
from contextlib import nullcontext
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from app import db
from app.agent_social import social_kind, handle
from app.repository import RequestContext
import test_proactive_flow as proactive


class NoModelCalls:
    async def complete_json(self, **kwargs):
        raise AssertionError('Social reply must not invoke a model')

    async def route(self, **kwargs):
        raise AssertionError('Social reply must not invoke a model')

    async def decide(self, **kwargs):
        raise AssertionError('Social reply must not invoke a model')


class SocialClosureTests(unittest.TestCase):
    setUp = proactive.ProactiveFlowTests.setUp
    request = proactive.ProactiveFlowTests.request
    version = proactive.ProactiveFlowTests.version
    chat = proactive.ProactiveFlowTests.chat
    material = proactive.ProactiveFlowTests.material
    send = proactive.ProactiveFlowTests.send
    analyze = proactive.ProactiveFlowTests.analyze
    conversation = proactive.ProactiveFlowTests.conversation
    count = proactive.ProactiveFlowTests.count
    ready = proactive.ProactiveFlowTests.ready
    turn = proactive.ProactiveFlowTests.turn
    drive = proactive.ProactiveFlowTests.drive
    start = proactive.ProactiveFlowTests.start
    answer = proactive.ProactiveFlowTests.answer
    pending = proactive.ProactiveFlowTests.pending

    def completed(self):
        session, host, ids = self.ready()
        self.start(session, 'first')
        for i in range(3):
            self.turn(session, '是', f'yes-{i}')
            if i < 2:
                self.answer(session, f'answer-{i}')
        self.assertEqual(self.pending(session), [])
        self.assertIn('本轮简历检查已完成', self.conversation(session)['messages'][-1]['content'])
        return session, host

    def test_screenshot_sequence_closes_naturally_without_reopening_or_writing(self):
        session, host = self.completed()
        before = self.conversation(session)['draft']
        counts = [self.count(t) for t in ('source_blocks', 'suggestions', 'suggestion_decisions')]
        phases = list(host.phases)
        host.client_factory = lambda runtime, budget: NoModelCalls()
        for i, text in enumerate(('谢谢你', '好的', '哦')):
            user, events = self.turn(session, text, f'social-{i}')
            self.assertEqual(events[-1]['payload']['outcome'], 'completed', events)
            last = self.conversation(session)['messages'][-1]
            self.assertEqual(last['assistant']['kind'], 'reply')
            self.assertNotIn('回到简历', last['content'])
            self.assertNotIn('待确认的改写', last['content'])
            self.assertNotIn('？', last['content'])
        self.assertEqual(self.conversation(session)['draft'], before)
        self.assertEqual([self.count(t) for t in ('source_blocks', 'suggestions', 'suggestion_decisions')], counts)
        self.assertEqual(host.phases, phases)

    def test_thanks_with_pending_proposal_does_not_accept_and_preserves_next_confirmation(self):
        session, host, ids = self.ready()
        self.start(session, 'first')
        before = self.conversation(session)['draft']
        self.turn(session, '谢谢你', 'thanks')
        last = self.conversation(session)['messages'][-1]
        self.assertIn('不客气', last['content'])
        self.assertEqual(last['assistant']['kind'], 'change_question')
        self.assertEqual(self.count('suggestion_decisions'), 0)
        self.assertEqual(self.conversation(session)['draft'], before)
        self.turn(session, '好的', 'accept')
        self.assertEqual(self.count('suggestion_decisions'), 1)
        self.assertIn('协助项目一', self.conversation(session)['draft']['markdown'])

    def test_yes_to_an_interview_question_is_not_swallowed_as_courtesy(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        self.turn(session, '是的', 'answer')
        with db.database(self.path, read_only=True) as c:
            docs = [json.loads(r[0]) for r in c.execute("SELECT doc_json FROM agent_state WHERE scope='experience'")]
        doc = next(d for d in docs if d['experience_id'] == ids[0])
        self.assertEqual(doc['questions'][0]['outcome'], 'answered')

    def test_thanks_without_draft_does_not_claim_a_resume_was_saved(self):
        session = self.chat()
        self.app.state.business_service.repository.save_material(RequestContext(session['sessionId']),
            name='pending.png', media_type='image/png', original_data=b'pending-image')
        self.app.state.agent_host.client_factory = lambda runtime, budget: NoModelCalls()
        self.turn(session, '谢谢', 'thanks')
        last = self.conversation(session)['messages'][-1]['content']
        self.assertIn('不客气', last)
        self.assertNotIn('当前稿已保存', last)
        self.assertEqual(self.count('source_blocks'), 0)


class SocialIntentTests(unittest.TestCase):
    def test_only_standalone_social_messages_match(self):
        self.assertEqual(social_kind('谢谢你！'), 'thanks')
        self.assertEqual(social_kind('好的。'), 'ack')
        for text in ('谢谢，第二段改短一点', '好的，但不要修改', '他说“谢谢”', '谢谢，帮我看看这张图'):
            self.assertIsNone(social_kind(text))

    def test_pending_material_intake_keeps_its_original_flow(self):
        runtime = SimpleNamespace(_pending=lambda: [{'materialId': 'pending'}])
        self.assertFalse(handle(runtime, {'userMessage': {'content': '好的'}}))

    def test_injected_ack_still_defers_to_pending_material_intake(self):
        runtime = SimpleNamespace(_pending=lambda: [{'materialId': 'pending'}])
        self.assertFalse(handle(runtime, {'userMessage': {'content': '好的'}}, kind='ack'))

    def test_injected_ack_still_defers_to_pending_suggestion(self):
        class Repo:
            def _operation(self, _ctx):
                connection = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: None))
                return nullcontext((connection, None))

            def get_session_context(self, _ctx):
                return {'draft': {'blocks': [{'blockId': 'blk_1'}]}}

        runtime = SimpleNamespace(
            _pending=lambda: [], repo=Repo(), context='ctx', sid='sid',
            _reply=lambda *a, **k: self.fail('injected ack was swallowed as courtesy'))

        import app.agent_social as social
        with patch.object(social, 'plan',
                          lambda _: {'pending': [{'suggestionId': 'sg_1'}], 'blocks': [{}],
                                     'remaining': [{}], 'review': []}):
            # 不是词表里的写法：只有模型把它判成 ack 才会走到这条守卫。
            self.assertFalse(handle(runtime, {'userMessage': {'content': '收到啦', 'messageId': 'm1'}},
                                   kind='ack'))
