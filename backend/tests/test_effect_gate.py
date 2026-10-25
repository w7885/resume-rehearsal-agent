"""Rewrites require a confirmed actual effect; one follow-up at most."""
import asyncio
import unittest

from app.agent.guard import EFFECT_EXHAUSTED_TEXT, GuardState, check
from app.agent.loop import _fallback_question, run_loop
from app.agent.memory import ExperienceDoc, Fact, Question, effect_state, save_experience
from app.agent.ports import DraftBlock, SessionContext, SessionInfo, SourceText
from app.agent.router import _is_decline
from app.agent.schemas import AskStep, StepDecisionAdapter
from app.agent.store import InMemoryStateStore
from fakes import FakeLLM, FakeRepo
import test_agent_session_policy as policy

BEFORE = '参与接口测试。'


def decision(target='p1', after='协助完成接口测试。', before=BEFORE):
    return StepDecisionAdapter.validate_python({'action': 'tool', 'tool': 'propose_change', 'args': {
        'operation': 'replace', 'target': target, 'before': before, 'after': after,
        'reason': '梳理职责表达',
        'source_refs': [{'source_id': 'resume', 'source_revision': 1, 'quote': '接口测试'}],
        'gaps': []}})


def effect_question(index, outcome='answered'):
    return Question(
        id=f'q{index}', slot='effect',
        text='这段经历上线或交付后，带来了哪些实际变化？',
        fingerprint=f'f{index}', outcome=outcome,
    )


def state(*, effect_state_value=None, direct=False, before=BEFORE):
    doc = ExperienceDoc(
        experience_id='p1', seq=1, kind='project', title='构造项目',
        raw_text=before, block_id='p1', block_revision=1,
    )
    doc.questions.append(Question(
        id='q0', slot='responsibility', text='你负责哪部分？',
        fingerprint='f0', outcome='answered',
    ))
    if effect_state_value == 'followup':
        doc.questions.append(effect_question(1))
    elif effect_state_value == 'absent':
        doc.questions.extend([effect_question(1), effect_question(2)])
    elif effect_state_value == 'declined':
        doc.questions.append(effect_question(1, 'declined'))
        doc.declined.append('effect')
    elif effect_state_value == 'active':
        doc.facts.append(Fact(
            id='f1', slot='effect', statement='接口测试效率提高',
            created_at='2026-01-01T00:00:00Z',
        ))
    elif effect_state_value == 'superseded':
        doc.facts.append(Fact(
            id='f1', slot='effect', statement='接口测试效率提高',
            status='superseded', created_at='2026-01-01T00:00:00Z',
        ))
    return GuardState(
        session=SessionContext(
            session=SessionInfo(session_id='test', experience_order=['p1']),
            draft_blocks=[DraftBlock(block_id='p1', markdown=before)],
        ),
        focus_doc=doc, anchor_text=before, focus_block_markdown=before,
        source_texts={'resume': before}, source_kinds={'resume': 'resume'},
        direct_rewrite=direct,
    )


class EffectGuardTests(unittest.TestCase):
    def test_no_effect_questions_asks_effect(self):
        result = check(decision(), state())
        self.assertEqual(result.violation.code, 'ask_effect')
        self.assertTrue(result.violation.retryable)

    def test_one_answered_effect_question_asks_followup(self):
        result = check(decision(), state(effect_state_value='followup'))
        self.assertEqual(result.violation.code, 'ask_effect_followup')
        self.assertTrue(result.violation.retryable)

    def test_one_declined_effect_question_also_asks_followup(self):
        result = check(decision(), state(effect_state_value='declined'))
        self.assertEqual(result.violation.code, 'ask_effect_followup')

    def test_two_effect_questions_allow_honest_rewrite(self):
        self.assertIsNone(
            check(decision(), state(effect_state_value='absent')).violation
        )

    def test_absent_rewrite_cannot_add_effect_claim(self):
        result = check(
            decision(after='协助完成接口测试，提升了测试效率。'),
            state(effect_state_value='absent'),
        )
        self.assertEqual(result.violation.code, 'effect_claim_without_evidence')
        self.assertTrue(result.violation.retryable)

    def test_absent_rewrite_keeps_effect_wording_from_the_original(self):
        before = '方便记录信息，参与接口测试。'
        result = check(
            decision(before=before, after='方便记录信息，协助完成接口测试。'),
            state(effect_state_value='absent', before=before),
        )
        self.assertIsNone(result.violation)

    def test_active_effect_passes(self):
        self.assertIsNone(check(decision(), state(effect_state_value='active')).violation)

    def test_superseded_effect_is_still_missing(self):
        result = check(decision(), state(effect_state_value='superseded'))
        self.assertEqual(result.violation.code, 'ask_effect')

    def test_direct_rewrite_cannot_skip_effect(self):
        result = check(decision(), state(direct=True))
        self.assertEqual(result.violation.code, 'ask_effect')

    def test_third_effect_question_is_retryably_blocked(self):
        guard = state(effect_state_value='absent')
        step = AskStep(
            action='ask_user', message='还有没有别的实际变化？', slot='effect',
        )
        result = check(step, guard)
        self.assertEqual(result.violation.code, 'effect_exhausted')
        self.assertEqual(result.violation.message, EFFECT_EXHAUSTED_TEXT)
        self.assertTrue(result.violation.retryable)

    def test_cn_filler_one_is_not_a_number(self):
        result = check(
            decision(after='协助完成一个博客系统的接口测试。'),
            state(effect_state_value='absent'),
        )
        self.assertIsNone(result.violation)

    def test_added_cn_quantity_is_still_blocked(self):
        result = check(
            decision(after='协助三人完成接口测试。'),
            state(effect_state_value='absent'),
        )
        self.assertEqual(result.violation.code, 'unsupported_number')
        self.assertTrue(result.violation.retryable)


class EffectFallbackTests(unittest.TestCase):
    def doc(self, **kwargs):
        doc = ExperienceDoc(
            experience_id='p1', seq=1, kind='project', title='构造项目',
            raw_text=BEFORE, block_id='p1', block_revision=1,
        )
        for slot in ('responsibility', 'context', 'action'):
            doc.facts.append(Fact(
                id=f'f-{slot}', slot=slot, statement=f'{slot}已确认',
                created_at='2026-01-01T00:00:00Z',
            ))
        if kwargs.get('effect') == 'followup':
            doc.questions.append(effect_question(1))
        elif kwargs.get('effect') == 'absent':
            doc.questions.extend([effect_question(1), effect_question(2)])
        elif kwargs.get('effect') == 'declined':
            doc.questions.append(effect_question(1, 'declined'))
            doc.declined.append('effect')
        elif kwargs.get('effect') == 'active':
            doc.facts.append(Fact(
                id='f-effect', slot='effect', statement='接口测试效率提高',
                created_at='2026-01-01T00:00:00Z',
            ))
        return doc

    def test_fallback_asks_effect_after_other_slots(self):
        question, slot = _fallback_question(self.doc())
        self.assertEqual(slot, 'effect')
        self.assertIn('实际变化', question)

    def test_fallback_asks_one_followup(self):
        question, slot = _fallback_question(self.doc(effect='followup'))
        self.assertEqual(slot, 'effect')
        self.assertEqual(question, '除了使用情况，实际带来了什么变化？')

    def test_fallback_after_decline_asks_one_followup(self):
        self.assertEqual(
            _fallback_question(self.doc(effect='declined')),
            ('除了使用情况，实际带来了什么变化？', 'effect'),
        )

    def test_fallback_skips_absent_effect(self):
        self.assertEqual(
            _fallback_question(self.doc(effect='absent')),
            ('有没有材料能支持这段经历的描述？', 'evidence'),
        )

    def test_confirmed_effect_skips_to_evidence(self):
        question, slot = _fallback_question(self.doc(effect='active'))
        self.assertEqual(slot, 'evidence')
        self.assertIn('材料', question)

    def test_absent_effect_still_produces_an_honest_suggestion(self):
        repo = FakeRepo(
            session_id=policy.SESSION,
            direction='前端开发实习',
            sources=[SourceText(
                source_id='resume', kind='resume', text=BEFORE,
            )],
            experience_order=['p1'],
            current_focus_id='p1',
            draft_blocks=[DraftBlock(
                block_id='p1', experience_id='p1', block_revision=1,
                markdown=BEFORE,
            )],
        )
        store = InMemoryStateStore()
        save_experience(store, policy.SESSION, self.doc(effect='absent'), None)
        llm = FakeLLM(decide_script=[
            {'action': 'tool', 'tool': 'propose_change', 'args': {
                'operation': 'replace', 'target': 'p1', 'before': BEFORE,
                'after': '协助完成接口测试与结果记录。',
                'reason': '梳理职责与动作',
                'source_refs': [{
                    'source_id': 'resume', 'source_revision': 1,
                    'quote': '接口测试',
                }],
                'gaps': [],
            }},
            {'action': 'finish', 'outcome': 'completed', 'summary': '建议已生成。'},
        ])
        result = asyncio.run(run_loop(
            run_id='r_absent', session_id=policy.SESSION, repo=repo,
            store=store, llm=llm, focus_experience_id='p1',
        ))
        self.assertEqual(result.outcome, 'waiting_user')
        self.assertEqual(len(repo.suggestions), 1)
        self.assertFalse(any(issue.kind == 'missing_effect' for issue in repo.issues))


class EffectDeclineTests(unittest.TestCase):
    def test_short_negative_answers_are_declined(self):
        self.assertTrue(_is_decline('没有更多统计数据'))
        self.assertTrue(_is_decline('没有实际效果'))
        self.assertTrue(_is_decline('不记得了'))

    def test_effect_answer_is_not_declined(self):
        self.assertFalse(_is_decline('没有统计数据，但老师反馈快多了'))

    def test_long_answer_is_not_declined(self):
        self.assertFalse(_is_decline('不是没有效果，' + '老师说录入流程比原来方便很多' * 3))


class EffectStateTests(unittest.TestCase):
    def doc(self, *outcomes, status='active'):
        doc = ExperienceDoc(
            experience_id='p1', seq=1, kind='project', title='构造项目',
            raw_text=BEFORE, block_id='p1', block_revision=1,
        )
        for index, outcome in enumerate(outcomes, start=1):
            doc.questions.append(Question(
                id=f'q{index}', slot='effect', text='带来了哪些实际变化？',
                fingerprint=f'f{index}', outcome=outcome,
            ))
        if status is not None:
            doc.facts.append(Fact(
                id='f1', slot='effect', statement='接口测试效率提高',
                status=status, created_at='2026-01-01T00:00:00Z',
            ))
        return doc

    def test_no_effect_question_is_missing(self):
        self.assertEqual(effect_state(self.doc(status=None)), 'missing')

    def test_one_answer_is_followup(self):
        self.assertEqual(effect_state(self.doc('answered', status=None)), 'followup')

    def test_one_decline_is_followup(self):
        self.assertEqual(effect_state(self.doc('declined', status=None)), 'followup')

    def test_two_answers_are_absent(self):
        self.assertEqual(
            effect_state(self.doc('answered', 'answered', status=None)), 'absent'
        )

    def test_answer_plus_decline_is_absent(self):
        self.assertEqual(
            effect_state(self.doc('answered', 'declined', status=None)), 'absent'
        )

    def test_active_fact_is_confirmed(self):
        self.assertEqual(effect_state(self.doc()), 'confirmed')

    def test_superseded_fact_without_questions_is_missing(self):
        self.assertEqual(effect_state(self.doc(status='superseded')), 'missing')

    def test_superseded_fact_with_two_questions_is_absent(self):
        self.assertEqual(
            effect_state(self.doc('answered', 'answered', status='superseded')),
            'absent',
        )


class DeliveryDeclineTests(unittest.TestCase):
    def test_no_deployment_is_not_a_decline(self):
        self.assertFalse(_is_decline('没有上线，只在本地运行'))

    def test_local_only_is_not_a_decline(self):
        self.assertFalse(_is_decline('只在本地跑过'))

    def test_no_effect_is_still_a_decline(self):
        self.assertTrue(_is_decline('没有实际效果'))


if __name__ == '__main__':
    unittest.main()
