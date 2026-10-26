"""Offline diagnosis contracts; synthetic inputs, not model quality results."""
import unittest

from app.agent.context import _effect_block, assemble_context
from app.agent.guard import GuardState, check
from app.agent.loop import _fallback_question
from app.agent.memory import ExperienceDoc, Fact, Question, effect_state, record_answer, supported_experience_slots
from app.agent.ports import SessionContext, SessionInfo, SourceRef
from app.agent.router import _effect_answer_kind
from app.agent.schemas import AskStep
from app.agent.memory import load_experience
from app.agent.store import InMemoryStateStore
from fakes import FakeLLM
import test_agent_session_policy as policy


def doc(text='参与接口测试。'):
    return ExperienceDoc(experience_id='p1', seq=1, kind='project', title='构造项目', raw_text=text)


def answer(value, previous=0):
    value_doc = doc()
    for index in range(previous):
        value_doc.questions.append(Question(id=f'previous-{index}', slot='effect', text='实际变化？', fingerprint=str(index), outcome='declined', answer_kind='uncertain'))
    value_doc.questions.append(Question(id='current', slot='effect', text='实际变化？', fingerprint='current'))
    kind = _effect_answer_kind(value)
    record_answer(value_doc, 'current', 'user-message', declined=kind != 'answered', answer_kind=kind)
    return value_doc


class DiagnosisTests(unittest.TestCase):
    def test_guard_rejects_reinterview_of_each_supported_original_dimension(self):
        value = doc('我负责查询模块；使用Python实现姓名模糊查询；将查询耗时从10分钟缩短到3分钟。')
        state = GuardState(session=SessionContext(session=SessionInfo(session_id='offline')),
                           focus_doc=value, anchor_text=value.raw_text)
        for slot, message in (
            ('responsibility', '查询模块中，你本人负责哪部分？'),
            ('action', '查询功能中，你具体做了什么？'),
            ('effect', '查询功能交付后，带来了什么变化？'),
        ):
            with self.subTest(slot=slot):
                result = check(AskStep(action='ask_user', message=message, slot=slot), state)
                self.assertIsNotNone(result.violation)
                self.assertEqual(result.violation.code, 'question_answered')

    def test_guard_allows_missing_personal_responsibility_question(self):
        for original in ('参与项目。', '团队负责查询模块。'):
            with self.subTest(original=original):
                value = doc(original)
                state = GuardState(session=SessionContext(session=SessionInfo(session_id='offline')),
                                   focus_doc=value, anchor_text=original)
                question = AskStep(action='ask_user', message='查询模块中，你本人负责哪部分？',
                                   slot='responsibility')
                self.assertIsNone(check(question, state).violation)

    def test_guard_skips_question_only_for_locatable_personal_supplement(self):
        quote = '我负责查询模块。'
        for kind, source, expected in (
            ('supplement', quote, 'question_answered'),
            ('jd', quote, None),
            ('supplement', '这是另一段不匹配的原文。', None),
        ):
            with self.subTest(source_kind=kind, source_text=source):
                value = doc()
                value.facts.append(Fact(id='answer', slot='responsibility', statement='负责查询模块',
                                        source_refs=[SourceRef(source_id='answer-source',
                                                               source_revision=1, quote=quote)]))
                state = GuardState(session=SessionContext(session=SessionInfo(session_id='offline')),
                                   focus_doc=value, anchor_text=value.raw_text,
                                   source_texts={'answer-source': source},
                                   source_kinds={'answer-source': kind})
                result = check(AskStep(action='ask_user', message='查询模块中，你本人负责哪部分？',
                                       slot='responsibility'), state)
                self.assertEqual(result.violation.code if result.violation else None, expected)

    def test_original_personal_responsibility_actions_and_effect_are_recognized(self):
        value = doc('我负责查询模块；使用Python实现姓名模糊查询；将查询耗时从10分钟缩短到3分钟。')
        self.assertEqual(supported_experience_slots(value), {'responsibility', 'action', 'effect'})
        self.assertEqual(effect_state(value), 'confirmed')
        self.assertIn('已确认', _effect_block(value))
        self.assertNotEqual(_fallback_question(value)[1], 'responsibility')

    def test_vague_or_collective_original_does_not_confirm_personal_responsibility(self):
        for text in ('参与项目。', '团队负责查询模块。', '项目组负责查询模块。', '我们负责查询模块。', '小组主要负责查询模块。'):
            with self.subTest(text=text):
                self.assertNotIn('responsibility', supported_experience_slots(doc(text)))
        self.assertIn('responsibility', supported_experience_slots(doc('团队开发查询工具，我负责查询模块。')))

    def test_planned_or_hypothetical_improvement_is_not_confirmed_effect(self):
        for text in ('计划减少查询步骤。', '如果减少查询步骤，可以这样写吗？', '设计查询工具以减少查询步骤。', '目的是提高查询效率。',
                     '我负责实现姓名查询功能，用于提高检索效率。', '我负责实现姓名查询功能，尝试提升检索效率。',
                     '我负责实现姓名查询功能，为了提高检索效率。', '我负责实现姓名查询功能，旨在提高检索效率。'):
            with self.subTest(text=text):
                self.assertNotIn('effect', supported_experience_slots(doc(text)))

    def test_memory_needs_locatable_personal_quotes_not_model_statement(self):
        value = doc()
        value.facts.append(Fact(id='memory', slot='responsibility', statement='我负责查询模块', source_refs=[SourceRef(source_id='s1', quote='我负责查询模块')]))
        self.assertNotIn('responsibility', supported_experience_slots(value))
        self.assertNotIn('responsibility', supported_experience_slots(value, source_texts={'s1':'我负责查询模块'}, source_kinds={'s1':'jd'}))
        self.assertNotIn('responsibility', supported_experience_slots(value, source_texts={'s1':'不同原文'}, source_kinds={'s1':'supplement'}))
        self.assertIn('responsibility', supported_experience_slots(value, source_texts={'s1':'我负责查询模块'}, source_kinds={'s1':'supplement'}))

    def test_explicit_absence_or_refusal_stops_effect_interview(self):
        for text in ('没有', '没有实际效果', '没有观察到变化', '不想回答这个问题', '跳过', '不要再问了'):
            with self.subTest(text=text):
                value = answer(text)
                self.assertEqual(effect_state(value), 'absent')
                self.assertNotEqual(_fallback_question(value)[1], 'effect')

    def test_uncertainty_allows_only_one_followup(self):
        for text in ('记不清了', '不知道', '不确定效果'):
            self.assertEqual(effect_state(answer(text)), 'followup')
            self.assertEqual(effect_state(answer(text, previous=1)), 'absent')

    def test_delivery_and_mixed_positive_reply_are_not_absence(self):
        for text in ('没有上线，只在本地运行', '只在本地运行', '没有统计数据，但老师反馈查询快多了', '不是没有效果，老师说步骤减少了'):
            with self.subTest(text=text):
                self.assertEqual(_effect_answer_kind(text), 'answered')

    def test_old_question_records_remain_loadable(self):
        question = Question.model_validate({'id':'old', 'slot':'effect', 'text':'实际变化？', 'fingerprint':'old', 'outcome':'declined'})
        self.assertEqual(question.answer_kind, 'legacy')
        value = doc()
        value.questions.append(question)
        self.assertEqual(effect_state(value), 'followup')

    def test_explicit_denial_does_not_revive_old_original_or_memory_effect(self):
        value = doc('我负责查询模块；实现姓名查询；将耗时缩短到3分钟。')
        value.facts.append(Fact(id='old-effect', slot='effect', statement='耗时缩短到3分钟',
                                created_at='2026-01-01T00:00:00+00:00',
                                source_refs=[SourceRef(source_id='old', quote='耗时缩短到3分钟')]))
        value.questions.append(Question(id='deny', slot='effect', text='能确认效果吗？', fingerprint='deny',
                                        outcome='declined', answer_kind='absent', answered_at='2026-01-02T00:00:00+00:00'))
        self.assertEqual(effect_state(value), 'absent')
        self.assertNotIn('effect', supported_experience_slots(value, source_texts={'old':'耗时缩短到3分钟'}, source_kinds={'old':'supplement'}))
        self.assertNotIn('已确认', _effect_block(value))

    def test_later_personal_effect_can_resume_after_explicit_absence(self):
        value = answer('没有实际效果')
        value.questions[0].answered_at = '2026-01-01T00:00:00+00:00'
        value.facts.append(Fact(id='new-effect', slot='effect', statement='查询耗时缩短到3分钟',
                                created_at='2026-01-02T00:00:00+00:00',
                                source_refs=[SourceRef(source_id='new', quote='查询耗时缩短到3分钟')]))
        self.assertEqual(effect_state(value), 'confirmed')
        self.assertIn('effect', supported_experience_slots(value, source_texts={'new':'查询耗时缩短到3分钟'}, source_kinds={'new':'supplement'}))

    def test_refusal_stops_questions_without_retracting_original_effect(self):
        value = doc('我负责查询模块；实现姓名查询；将耗时缩短到3分钟。')
        value.questions.append(Question(id='refuse', slot='effect', text='能确认效果吗？', fingerprint='refuse',
                                        outcome='declined', answer_kind='refused'))
        self.assertEqual(effect_state(value), 'confirmed')
        self.assertIn('effect', supported_experience_slots(value))
        self.assertNotEqual(_fallback_question(value)[1], 'effect')

    def test_context_explains_available_original_without_active_effect_fact(self):
        value = doc('我负责查询模块；实现姓名模糊查询；将耗时缩短到3分钟。')
        context = SessionContext(session=SessionInfo(session_id='offline', experience_order=['p1']))
        text = assemble_context(context, [value], {}, 'p1', '开发实习')
        self.assertIn('已有材料诊断', text)
        self.assertIn('responsibility、action、effect', text)
        self.assertIn('已确认', text)


class RouterAnswerTests(unittest.TestCase):
    def test_common_explicit_refusal_variants_stop_effect_followup(self):
        for text in ('我不愿意回答这个问题。', '我不想再回答了。',
                     '这个问题我不想讨论。', '我不愿意继续回答这个问题。'):
            with self.subTest(text=text):
                repo = policy.make_repo(direction='开发实习', direction_confirmed=True)
                store = InMemoryStateStore()
                policy.seed_experience(store, asked='实际发生了什么变化？', slot='effect')
                policy.run_turn(repo, store, FakeLLM(decide_script=[policy.FINISH]), text)
                value = load_experience(store, policy.SESSION, 'exp_01')[0]
                self.assertEqual(value.questions[0].answer_kind, 'refused')
                self.assertIn('effect', value.declined)
                self.assertEqual(effect_state(value), 'absent')
                self.assertNotEqual(_fallback_question(value)[1], 'effect')

    def test_negated_hypothetical_or_quoted_refusal_is_not_a_refusal(self):
        for text in ('我不是不愿意回答这个问题。',
                     '如果我不愿意回答这个问题该怎么办？',
                     '他表示“我不愿意回答这个问题”。'):
            with self.subTest(text=text):
                self.assertNotEqual(_effect_answer_kind(text), 'refused')

    def test_explicit_refusal_also_stops_non_effect_dimension(self):
        repo = policy.make_repo(direction='开发实习', direction_confirmed=True)
        store = InMemoryStateStore()
        policy.seed_experience(store, asked='你负责哪部分？', slot='responsibility')
        policy.run_turn(repo, store, FakeLLM(decide_script=[policy.FINISH]), '不想回答这个问题')
        value = load_experience(store, policy.SESSION, 'exp_01')[0]
        self.assertIn('responsibility', value.declined)
        self.assertEqual(value.questions[0].answer_kind, 'refused')

    def test_saved_reply_classification_survives_router_and_state_reload(self):
        for text, expected_kind, expected_state in (
            ('没有', 'absent', 'absent'),
            ('没有实际效果', 'absent', 'absent'),
            ('不想回答这个问题', 'refused', 'absent'),
            ('跳过', 'refused', 'absent'),
            ('记不清了', 'uncertain', 'followup'),
            ('不知道', 'uncertain', 'followup'),
            ('没有上线，只在本地运行', 'answered', 'followup'),
        ):
            with self.subTest(text=text):
                repo = policy.make_repo(direction='开发实习', direction_confirmed=True)
                store = InMemoryStateStore()
                policy.seed_experience(store, asked='交付后实际发生了什么变化？', slot='effect')
                events = policy.run_turn(repo, store, FakeLLM(decide_script=[policy.FINISH]), text)
                value = load_experience(store, policy.SESSION, 'exp_01')[0]
                self.assertEqual(value.questions[0].answer_kind, expected_kind)
                self.assertEqual(value.questions[0].answer_message_id, 'm1')
                self.assertEqual(effect_state(value), expected_state)
                self.assertFalse(any(e.type == 'error' for e in events))

    def test_second_uncertain_reply_exhausts_recall_followup(self):
        repo = policy.make_repo(direction='开发实习', direction_confirmed=True)
        store = InMemoryStateStore()
        policy.seed_experience(store, asked='交付后实际发生了什么变化？', slot='effect')
        policy.run_turn(repo, store, FakeLLM(decide_script=[policy.FINISH]), '记不清了', 'first')
        value, revision = load_experience(store, policy.SESSION, 'exp_01')
        value.questions.append(Question(id='q2', slot='effect', text='操作步骤有变化吗？', fingerprint='q2'))
        from app.agent.memory import save_experience
        save_experience(store, policy.SESSION, value, revision)
        policy.run_turn(repo, store, FakeLLM(decide_script=[policy.FINISH]), '还是记不清', 'second')
        value = load_experience(store, policy.SESSION, 'exp_01')[0]
        self.assertEqual(effect_state(value), 'absent')
        self.assertEqual(value.questions[-1].answer_kind, 'uncertain')


if __name__ == '__main__':
    unittest.main()
