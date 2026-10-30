"""Real Host/state/source transactions, scripted intents and blocked network."""
import json
import unittest
from unittest.mock import patch

import httpx
from app import db
from app.agent.memory import load_experience
from app.agent_state import GuardedStateStore
import test_answer_source_recovery as recovery
import test_agent_integration as integration


class DialogueHostTests(unittest.TestCase):
    setUp = recovery.RecoveryTests.setUp
    request = recovery.RecoveryTests.request
    version = recovery.RecoveryTests.version
    chat = recovery.RecoveryTests.chat
    material = recovery.RecoveryTests.material
    send = recovery.RecoveryTests.send
    analyze = recovery.RecoveryTests.analyze
    conversation = recovery.RecoveryTests.conversation
    count = recovery.RecoveryTests.count
    ready = recovery.RecoveryTests.ready
    turn = recovery.RecoveryTests.turn
    pending = recovery.RecoveryTests.pending
    setup_recovery = recovery.RecoveryTests.setup_recovery

    def memory(self, session, target):
        # Use a current Host runtime only for the guarded scope, not a model call.
        from app.repository import RequestContext
        from types import SimpleNamespace
        runtime = SimpleNamespace(repo=self.app.state.business_service.repository,
                                  context=RequestContext(session['sessionId']), sid=session['sessionId'])
        return load_experience(GuardedStateStore(runtime), session['sessionId'], target)[0]

    def semantic_client(self, decision):
        test = self
        class Client(integration.FakeClient):
            async def route(self, **kwargs):
                test.route_input = kwargs['user']
                return {'scope': 'experience', 'confidence': 1, **decision}
            async def decide(self, **kwargs):
                raise AssertionError('Explanatory turn must not enter interview/rewrite loop')
        self.app.state.agent_host.client_factory = lambda runtime, budget: Client(runtime)

    def test_explanations_keep_pending_state_and_replay_consumes_nothing(self):
        with patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', side_effect=AssertionError('offline')):
            session, host, ids = self.setup_recovery()
            before = self.memory(session, ids[0])
            draft = self.conversation(session)['draft']
            sources = self.count('source_blocks')
            self.semantic_client({'intent':'meta','reply':'这里是在核对当前项目中保留的代码或文档。'})
            for i, text in enumerate(('你说的是哪项内容', '这个问题我没理解', '？')):
                user, events = self.turn(session, text, f'clarify-{i}')
                current = self.memory(session, ids[0])
                self.assertEqual(current.questions, before.questions)
                self.assertEqual((current.round,current.no_fact_streak),(before.round,before.no_fact_streak))
                self.assertEqual(self.conversation(session)['draft'], draft)
                self.assertIn('当前正在处理',self.conversation(session)['messages'][-1]['content'])
            self.assertEqual(self.count('source_blocks'), sources)
            self.assertEqual(self.count('suggestion_decisions'),0)
            self.assertEqual(self.pending(session),[])
            self.assertIn(recovery.QUESTION,self.route_input)
            self.analyze(session,user,'clarify-2')
            self.assertEqual(self.memory(session,ids[0]).questions,before.questions)

    def test_mixed_message_activates_only_verbatim_factual_sentence(self):
        with patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', side_effect=AssertionError('offline')):
            session, host, ids = self.setup_recovery()
            draft = self.conversation(session)['draft']
            self.semantic_client({'intent':'meta','answers_pending':True,'answer_quote':recovery.EVIDENCE,
                                  'reply':'问的是当前项目保留的依据，不要求你提供额外成果。'})
            text=recovery.EVIDENCE+'你问的是哪个项目？'
            self.turn(session,text,'mixed')
            current=self.memory(session,ids[0])
            self.assertEqual(current.questions[-1].outcome,'answered')
            self.assertEqual(current.questions[-1].answer_quote,recovery.EVIDENCE)
            with db.database(self.path,read_only=True) as c:
                rows=c.execute("SELECT b.text,m.content FROM source_blocks b JOIN messages m ON b.message_id=m.message_id WHERE m.content=?",(text,)).fetchall()
            self.assertEqual([(r[0],r[1]) for r in rows],[(recovery.EVIDENCE,text)])
            self.assertEqual(self.conversation(session)['draft'],draft)
            self.assertEqual(self.pending(session),[])

    def test_invalid_quote_cannot_create_a_source_or_answer(self):
        with patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', side_effect=AssertionError('offline')):
            session, host, ids=self.setup_recovery()
            before=self.memory(session,ids[0]); sources=self.count('source_blocks')
            self.semantic_client({'intent':'meta','answers_pending':True,'answer_quote':'我熟练掌握Python。'})
            self.turn(session,'我不熟练掌握Python。你问哪个项目？','bad-quote')
            self.assertEqual(self.memory(session,ids[0]).questions,before.questions)
            self.assertEqual(self.count('source_blocks'),sources)

    def test_other_dimension_supplement_is_saved_without_consuming_pending_question(self):
        with patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', side_effect=AssertionError('offline')):
            session, host, ids=self.setup_recovery()
            before=self.memory(session,ids[0]);quote='我使用Python实现了查询模块。'
            self.semantic_client({'intent':'meta','answers_pending':False,'answer_quote':quote,
                                  'reply':'实现方式已保留，当前是在核对留存材料。'})
            self.turn(session,quote+'你为什么问这个？','other-slot')
            after=self.memory(session,ids[0])
            self.assertEqual(after.questions,before.questions)
            with db.database(self.path,read_only=True) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM source_blocks WHERE text=?',(quote,)).fetchone()[0],1)


if __name__=='__main__':
    unittest.main()
