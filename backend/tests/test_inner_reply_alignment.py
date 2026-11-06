"""内层应答对齐：先复现四类错位，全部使用 fake client / 内存状态，不调用真实模型。"""
import asyncio
from types import SimpleNamespace
import unittest

from app.agent.guard import GuardState, _cn_numbers, _no_information, check
from app.agent.memory import ExperienceDoc, NewFact, load_experience, save_experience
from app.agent.ports import Message, SessionContext, SessionInfo, SourceRef
from app.agent.schemas import ProposeChangeArgs, SaveFactsArgs, ToolStep
from app.agent.store import InMemoryStateStore
from app.agent.tools import ToolContext, _save_facts
import test_agent_integration as integration
import test_proactive_flow as proactive


class ChineseNumberTests(unittest.TestCase):
    """P3：中文“一 + 量词”不是数字声明，不能被 unsupported_number 拦住。"""

    def test_quantifier_yi_is_not_treated_as_number(self):
        # 当前实现会把“一项/一条/一步/第一项”里的“一”当成数字
        for text in ('增加一项功能', '减少一条记录', '减少一步操作', '第一项'):
            with self.subTest(text=text):
                self.assertEqual(list(_cn_numbers(text)), [])

    def test_real_quantities_stay_guarded(self):
        for text in ('三人团队', '五次回访'):
            with self.subTest(text=text):
                self.assertNotEqual(list(_cn_numbers(text)), [])


class EmptyAnswerTests(unittest.TestCase):
    """P2：空壳回答（如“是我负责的”）不应被写成事实，也不该产出同义反复建议。"""

    def setUp(self):
        self.doc = ExperienceDoc(experience_id='p1', seq=1, kind='project',
                                 raw_text='参与项目一测试。')
        self.store = InMemoryStateStore()
        save_experience(self.store, 's', self.doc, None)
        self.context = SessionContext(
            session=SessionInfo(session_id='s', experience_order=['p1']),
            messages=[Message(message_id='m', role='user', text='是我负责的。')],
        )
        self.repo = SimpleNamespace(get_session_context=lambda sid: self.context)

    def test_shell_answer_is_not_saved_as_fact(self):
        args = SaveFactsArgs(experience_id='p1',
                             facts=[NewFact(slot='responsibility', statement='是我负责的')])
        asyncio.run(_save_facts(args, ToolContext('s', self.repo, self.store, 'm')))
        active = [f for f in load_experience(self.store, 's', 'p1')[0].facts
                  if f.status == 'active']
        self.assertEqual(active, [])

    def test_rewrite_that_only_adds_shell_words_is_rejected(self):
        # 信息段走证据门禁；after 只在原文前加了“本人负责”，应判 no_information
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='skills', raw_text='Python')
        step = ToolStep(action='tool', tool='propose_change', args=ProposeChangeArgs(
            operation='replace', target='p1', before='Python', after='本人负责Python',
            reason='整理表达', gaps=[],
            source_refs=[SourceRef(source_id='s1', source_revision=1, quote='Python')],
        ).model_dump())
        state = GuardState(
            session=SessionContext(session=SessionInfo(session_id='s', experience_order=['p1'])),
            focus_doc=doc, focus_block_markdown='Python',
            source_texts={'s1': 'Python'}, source_kinds={'s1': 'resume'},
        )
        result = check(step, state)
        self.assertIsNotNone(result.violation)
        self.assertEqual(result.violation.code, 'no_information')


class NoInformationTests(unittest.TestCase):
    """P2 判据只拦“纯插入空壳词”，不误伤精简和角色调整。"""

    def test_inserted_shell_words_are_no_information(self):
        self.assertTrue(_no_information('Python', '本人负责Python'))

    def test_condensation_is_information(self):
        self.assertFalse(_no_information('本人负责该项目的开发工作', '负责开发'))

    def test_role_switch_is_information(self):
        self.assertFalse(_no_information('参与开发', '负责开发'))


class _IntentClient(integration.FakeClient):
    """固定返回指定路由结果；决定解析默认 clarify，模拟当前“固定模板”行为。"""

    def __init__(self, runtime, payload, answer='clarify'):
        super().__init__(runtime, answer=answer)
        self.payload = payload

    async def route(self, **kwargs):
        return dict(self.payload)


class _HostCase(unittest.TestCase):
    # 复用现有集成/主动流测试的 host 构造与断言工具
    setUp = integration.AgentIntegrationTests.setUp
    request = integration.AgentIntegrationTests.request
    version = integration.AgentIntegrationTests.version
    chat = integration.AgentIntegrationTests.chat
    material = integration.AgentIntegrationTests.material
    send = integration.AgentIntegrationTests.send
    analyze = integration.AgentIntegrationTests.analyze
    conversation = integration.AgentIntegrationTests.conversation
    count = integration.AgentIntegrationTests.count
    ready = proactive.ProactiveFlowTests.ready
    drive = proactive.ProactiveFlowTests.drive
    turn = proactive.ProactiveFlowTests.turn
    start = proactive.ProactiveFlowTests.start
    answer = proactive.ProactiveFlowTests.answer
    pending = proactive.ProactiveFlowTests.pending

    def last(self, session):
        return self.conversation(session)['messages'][-1]['content']


class PendingIntentTests(_HostCase):
    """P1：有待确认建议时，meta 提问与切段请求不能被固定模板吞掉。"""

    def pending_session(self):
        session, host, ids = self.ready()
        self.start(session, 'first')
        self.assertTrue(self.pending(session), '前置条件：应已产生待确认建议')
        return session, host, ids

    def test_pending_gate_answers_meta_question(self):
        session, host, ids = self.pending_session()
        host.client_factory = lambda runtime, budget: _IntentClient(runtime, {
            'intent': 'meta', 'scope': 'experience', 'confidence': 0.9,
            'reply': '当前正在处理第 1 段。',
        })
        self.turn(session, '这是哪一段？', 'meta')
        messages = [m['content'] for m in self.conversation(session)['messages']
                    if m['role'] == 'assistant']
        # 当前实现会回“当前只有这条待确认建议…”；修复后应先回应，再补一次提醒。
        self.assertFalse(any('当前只有这条待确认建议' in m for m in messages[-3:]))
        self.assertTrue(any('当前正在处理第 1 段' in m for m in messages[-3:]))

    def test_pending_gate_handles_global_edit_without_fixed_template(self):
        session, host, ids = self.pending_session()
        host.client_factory = lambda runtime, budget: _IntentClient(runtime, {
            'intent': 'global_edit', 'scope': 'whole_resume', 'confidence': 0.9,
            'quote': '整份都改',
        })
        self.turn(session, '整份都改', 'whole')
        text = self.last(session)
        self.assertIn('当前有待确认的改写', text)
        self.assertNotIn('当前只有这条待确认建议', text)

    def test_pending_gate_does_not_swallow_segment_reference(self):
        session, host, ids = self.pending_session()
        current = self.pending(session)[0]['target']['blockId']
        target = next(b for b in ids if b != current)   # 必须真的换一段
        host.client_factory = lambda runtime, budget: _IntentClient(runtime, {
            'intent': 'focus', 'target_experience_id': target,
            'scope': 'experience', 'confidence': 0.9, 'quote': '第一段',
        })
        self.turn(session, '第一段', 'focus')
        text = self.last(session)
        # 当前实现会回“当前只有这条待确认建议…”，完全没有理会“第一段”
        self.assertNotIn('当前只有这条待确认建议', text)
        self.assertIn('段', text)


class WordingEditTests(_HostCase):
    """P4：当前段的措辞调整不应被当成“整体改写”。"""

    def test_wording_edit_is_not_global_edit(self):
        session, host, ids = self.ready()
        host.client_factory = lambda runtime, budget: _IntentClient(runtime, {
            'intent': 'global_edit', 'scope': 'whole_resume', 'confidence': 0.9,
            'quote': '那就按原文改改措辞',
        })
        self.turn(session, '那就按原文改改措辞', 'wording')
        self.assertNotIn('整体改写还没开放', self.last(session))

    def test_whole_resume_request_still_global(self):
        # 反例：明确的整体请求仍按整体处理，避免“收窄”过度。
        session, host, ids = self.ready()
        host.client_factory = lambda runtime, budget: _IntentClient(runtime, {
            'intent': 'global_edit', 'scope': 'whole_resume', 'confidence': 0.9,
            'quote': '整份都改',
        })
        self.turn(session, '整份都改', 'whole')
        self.assertIn('整体改写', self.last(session))


if __name__ == '__main__':
    unittest.main()

