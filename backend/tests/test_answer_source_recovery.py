"""Offline regression for answered evidence, quotable chat, and safe continuation."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import db
from app.agent.memory import load_experience
from app.agent_answer_sources import factual_answer
from app.agent_state import GuardedStateStore
from app.repository import Repository, RequestContext
from test_proactive_flow import ProactiveClient
import test_proactive_flow as proactive

ANSWER = '我负责用 Python 自带 sqlite3 建表保存，查询支持按名字搜索，查询比逐行查找方便。'
EVIDENCE = '项目已上传 Gitee，我保留了完整代码。'
QUESTION = '有没有材料能支持这段经历的描述？'


class RecoveryClient(ProactiveClient):
    async def decide(self, **kwargs):
        host = self.runtime.host
        host.calls += 1
        store = GuardedStateStore(self.runtime)
        ctx = self.runtime.repo.get_session_context(self.runtime.context)
        session_key = next(meta.key for meta in store.list(self.runtime.sid, 'session') if meta.key == self.runtime.sid)
        sid_doc = store.get(self.runtime.sid, 'session', session_key).doc
        focus = sid_doc['current_focus_id']
        host.seen_sources[focus] = kwargs['user'].split('## 可用来源（quote 必须逐字来自这里）', 1)[1].split('## 已确认事实', 1)[0]
        doc = load_experience(store, self.runtime.sid, focus)[0]
        if not doc.questions:
            return SimpleNamespace(data={'action': 'ask_user', 'message': '你本人负责哪部分？', 'slot': 'responsibility'})
        if not doc.facts:
            return SimpleNamespace(data={'action': 'tool', 'tool': 'save_facts', 'args': {
                'experience_id': focus, 'facts': [
                    {'slot': 'responsibility', 'statement': '负责开发'},
                    {'slot': 'context', 'statement': '查找学生信息'},
                    {'slot': 'action', 'statement': '使用sqlite3保存数据'},
                    {'slot': 'effect', 'statement': '查询比逐行查找方便'},
                    {'slot': 'tech', 'statement': 'Python sqlite3'},
                ]}})
        if not any(q.slot == 'evidence' for q in doc.questions):
            return SimpleNamespace(data={'action': 'ask_user', 'message': QUESTION, 'slot': 'evidence'})
        if self.proposed:
            return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed'})
        block = next(b for b in ctx['draft']['blocks'] if b['blockId'] == focus)
        supplements = [s for s in ctx['sources'] if s['role'] == 'supplement']
        refs = block['sourceRefs'] + [{'sourceId': s['sourceId'], 'sourceRevision': s['sourceRevision'], 'quote': s['text']} for s in supplements]
        if not host.bad:
            self.proposed = True
        return SimpleNamespace(data={'action': 'tool', 'tool': 'propose_change', 'args': {
            'operation': 'replace', 'target': focus, 'before': block['markdown'],
            'after': '使用 Python 自带 sqlite3 建表保存，支持按名字搜索。' if not host.bad else '测试效率提升999%。',
            'reason': '梳理用户明确说明的实现', 'gaps': [],
            'source_refs': [{'source_id': r['sourceId'], 'source_revision': r['sourceRevision'], 'quote': r['quote']} for r in refs]}})


class RecoveryTests(unittest.TestCase):
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
    pending = proactive.ProactiveFlowTests.pending

    def setup_recovery(self, bad=False):
        session, host, ids = self.ready()
        host.calls, host.bad, host.seen_sources = 0, bad, {}
        host.client_factory = lambda runtime, budget: RecoveryClient(runtime)
        self.turn(session, '继续', 'first')
        self.turn(session, ANSWER, 'answer')
        self.assertTrue(self.conversation(session)['messages'][-1]['content'].startswith(QUESTION))
        return session, host, ids

    def test_answers_become_exact_sources_and_sqlite3_can_be_quoted_after_later_turn(self):
        session, host, ids = self.setup_recovery()
        user, events = self.turn(session, EVIDENCE, 'evidence')
        self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user')
        suggestion = self.pending(session)[0]
        self.assertIn('sqlite3', suggestion['after'])
        self.assertEqual(suggestion['target']['blockId'], ids[0])
        with db.database(self.path, read_only=True) as c:
            sources = [dict(r) for r in c.execute("SELECT * FROM source_blocks WHERE role='supplement' AND message_id IS NOT NULL")]
        self.assertEqual({s['text'] for s in sources}, {ANSWER, EVIDENCE})
        self.assertTrue(all(s['source_revision'] == 1 for s in sources))
        self.assertTrue(any(ref['quote'] == ANSWER for ref in suggestion['sourceRefs']))
        calls = host.calls
        self.analyze(session, user, 'replay')
        self.assertEqual(host.calls, calls)
        self.assertEqual(self.count('suggestions'), 1)

    def test_blocked_rewrite_does_not_repeat_answered_evidence_and_explicit_keep_advances(self):
        session, host, ids = self.setup_recovery(bad=True)
        self.turn(session, EVIDENCE, 'evidence')
        messages = self.conversation(session)['messages']
        self.assertEqual(sum(m['role'] == 'assistant' and m['content'].startswith(QUESTION) for m in messages), 1)
        self.assertIn('999', messages[-1]['content'])
        self.assertEqual(messages[-1]['assistant']['kind'], 'reply')
        self.assertEqual(self.pending(session), [])
        before = self.conversation(session)['draft']['markdown']
        calls = host.calls
        self.turn(session, '继续', 'continue')
        self.assertEqual(host.calls, calls)
        self.assertIn('保留原文', self.conversation(session)['messages'][-1]['content'])
        self.turn(session, '保留原文，继续下一段', 'keep')
        self.assertEqual(self.conversation(session)['draft']['markdown'], before)
        self.assertEqual(self.count('suggestion_decisions'), 0)
        self.turn(session, ANSWER, 'second-answer')
        with db.database(self.path, read_only=True) as c:
            progress = json.loads(c.execute("SELECT doc_json FROM agent_state WHERE key='host-progress:g1'").fetchone()[0])
        self.assertIn(ids[0], progress['reviewed'])
        self.assertEqual(progress['waiting'], ids[1])
        self.assertNotIn(EVIDENCE, host.seen_sources[ids[1]])

    def test_mixed_hypothetical_answer_is_not_activated_as_personal_evidence(self):
        session, host, ids = self.setup_recovery(bad=True)
        self.turn(session, '我保留了代码，如果效率提升30%，可以这样写吗？', 'hypothesis')
        with db.database(self.path, read_only=True) as c:
            texts = [r[0] for r in c.execute("SELECT text FROM source_blocks WHERE role='supplement'")]
        self.assertEqual(texts, [ANSWER])
        self.assertEqual(self.pending(session), [])

    def test_source_write_failure_rolls_back_and_keeps_user_answer(self):
        session, host, ids = self.setup_recovery()
        before = self.count('source_blocks')
        original = Repository.save_source
        def fail(repo, *args, **kwargs):
            original(repo, *args, **kwargs)
            raise OSError('scripted disk failure')
        with patch.object(Repository, 'save_source', fail):
            self.turn(session, EVIDENCE, 'failure')
        self.assertEqual(self.count('source_blocks'), before)
        self.assertTrue(any(m['content'] == EVIDENCE for m in self.conversation(session)['messages']))
        self.assertEqual(self.pending(session), [])


class AnswerAdmissionTests(unittest.TestCase):
    def test_control_hypothetical_and_other_person_text_stays_chat_only(self):
        for text in ('继续', '是', '没有', '如果效率提升30%', '他说主导开发', '请帮我写成负责人', '直接改写吧', '好像用了sqlite3'):
            with self.subTest(text=text):
                self.assertFalse(factual_answer(text))
        self.assertTrue(factual_answer(ANSWER))
        self.assertTrue(factual_answer('没有使用框架，我用sqlite3保存数据。'))
