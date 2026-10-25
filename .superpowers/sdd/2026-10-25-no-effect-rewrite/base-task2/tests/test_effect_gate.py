"""Rewrites require a confirmed actual effect; one follow-up at most."""
import asyncio
import unittest

from app.agent.guard import MISSING_EFFECT_TEXT, GuardState, check
from app.agent.loop import _fallback_question, run_loop
from app.agent.memory import ExperienceDoc, Fact, Question, effect_state, save_experience
from app.agent.ports import DraftBlock, SessionContext, SessionInfo
from app.agent.router import _is_decline
from app.agent.schemas import AskStep, StepDecisionAdapter
from app.agent.store import InMemoryStateStore
from fakes import FakeLLM, FakeRepo
import test_agent_session_policy as policy

BEFORE = '参与接口测试。'


def decision(target='p1'):
    return StepDecisionAdapter.validate_python({'action': 'tool', 'tool': 'propose_change', 'args': {
        'operation': 'replace', 'target': target, 'before': BEFORE, 'after': '协助完成接口测试。',
        'reason': '梳理职责表达',
        'source_refs': [{'source_id': 'resume', 'source_revision': 1, 'quote': '接口测试'}],
        'gaps': []}})


def effect_question(index, outcome='answered'):
    return Question(
        id=f'q{index}', slot='effect',
        text='这段经历上线或交付后，带来了哪些实际变化？',
        fingerprint=f'f{index}', outcome=outcome,
    )


def state(*, effect_state_value=None, direct=False):
    doc = ExperienceDoc(
        experience_id='p1', seq=1, kind='project', title='构造项目',
        raw_text=BEFORE, block_id='p1', block_revision=1,
    )
    doc.questions.append(Question(
        id='q0', slot='responsibility', text='你负责哪部分？',
        fingerprint='f0', outcome='answered',
    ))
    if effect_state_value == 'followup':
        doc.questions.append(effect_question(1))
    elif effect_state_value == 'blocked':
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
            draft_blocks=[DraftBlock(block_id='p1', markdown=BEFORE)],
        ),
        focus_doc=doc, anchor_text=BEFORE, focus_block_markdown=BEFORE,
        source_texts={'resume': BEFORE}, source_kinds={'resume': 'resume'},
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

    def test_two_answered_effect_questions_block(self):
        result = check(decision(), state(effect_state_value='blocked'))
        self.assertEqual(result.violation.code, 'missing_effect')
        self.assertFalse(result.violation.retryable)
        self.assertEqual(result.violation.message, MISSING_EFFECT_TEXT)

    def test_declined_effect_blocks(self):
        result = check(decision(), state(effect_state_value='declined'))
        self.assertEqual(result.violation.code, 'missing_effect')
        self.assertFalse(result.violation.retryable)

    def test_active_effect_passes(self):
        self.assertIsNone(check(decision(), state(effect_state_value='active')).violation)

    def test_superseded_effect_is_still_missing(self):
        result = check(decision(), state(effect_state_value='superseded'))
        self.assertEqual(result.violation.code, 'ask_effect')

    def test_direct_rewrite_cannot_skip_effect(self):
        result = check(decision(), state(direct=True))
        self.assertEqual(result.violation.code, 'ask_effect')

    def test_third_effect_question_is_blocked(self):
        guard = state(effect_state_value='blocked')
        step = AskStep(
            action='ask_user', message='除了使用情况，实际带来了什么变化？',
            slot='effect',
        )
        result = check(step, guard)
        self.assertEqual(result.violation.code, 'missing_effect')
        self.assertFalse(result.violation.retryable)


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
        elif kwargs.get('effect') == 'blocked':
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

    def test_fallback_stops_when_effect_is_blocked(self):
        self.assertEqual(_fallback_question(self.doc(effect='blocked')), (None, None))
        self.assertEqual(_fallback_question(self.doc(effect='declined')), (None, None))

    def test_confirmed_effect_skips_to_evidence(self):
        question, slot = _fallback_question(self.doc(effect='active'))
        self.assertEqual(slot, 'evidence')
        self.assertIn('材料', question)

    def test_fallback_limited_records_missing_effect_issue(self):
        repo = FakeRepo(
            session_id=policy.SESSION,
            direction='前端开发实习',
            experience_order=['p1'],
            current_focus_id='p1',
            draft_blocks=[DraftBlock(
                block_id='p1', experience_id='p1', block_revision=1,
                markdown=BEFORE,
            )],
        )
        store = InMemoryStateStore()
        doc = self.doc(effect='declined')
        save_experience(store, policy.SESSION, doc, None)
        llm = FakeLLM(decide_script=[{'action': 'bogus'}, {'action': 'bogus'}])
        result = asyncio.run(run_loop(
            run_id='r_effect', session_id=policy.SESSION, repo=repo,
            store=store, llm=llm, focus_experience_id='p1',
        ))
        self.assertEqual(result.outcome, 'limited')
        self.assertTrue(any(issue.kind == 'missing_effect' for issue in repo.issues))
        self.assertFalse(any(event.type == 'suggestion' for event in result.events))


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
