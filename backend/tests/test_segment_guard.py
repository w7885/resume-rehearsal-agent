"""Each segment must be asked about before rewriting, and only the focus segment."""
import unittest

from app.agent.guard import GuardState, check
from app.agent.memory import ExperienceDoc, Fact, Question
from app.agent.ports import DraftBlock, SessionContext, SessionInfo
from app.agent.questions import requirement_question
from app.agent.schemas import AskStep, StepDecisionAdapter

BEFORE = '参与接口测试。'


def decision(target='p1'):
    return StepDecisionAdapter.validate_python({'action': 'tool', 'tool': 'propose_change', 'args': {
        'operation': 'replace', 'target': target, 'before': BEFORE, 'after': '协助完成接口测试。',
        'reason': '梳理职责表达', 'source_refs': [{'source_id': 'resume', 'source_revision': 1, 'quote': '接口测试'}], 'gaps': []}})


def state(*, answered=False, direct=False, effect=False):
    doc = ExperienceDoc(experience_id='p1', seq=1, kind='project', title='构造项目', raw_text=BEFORE, block_id='p1', block_revision=1)
    if answered:
        doc.questions.append(Question(id='q1', slot='responsibility', text='你负责哪部分？', fingerprint='f', outcome='answered'))
    if effect:
        doc.facts.append(Fact(id='f1', slot='effect', statement='接口测试效率提高'))
    return GuardState(
        session=SessionContext(session=SessionInfo(session_id='test', experience_order=['p1', 'p2']),
                               draft_blocks=[DraftBlock(block_id='p1', markdown=BEFORE)]),
        focus_doc=doc, anchor_text=BEFORE, focus_block_markdown=BEFORE,
        source_texts={'resume': BEFORE}, source_kinds={'resume': 'resume'}, direct_rewrite=direct)


class SegmentGuardTests(unittest.TestCase):
    def test_propose_before_any_answer_is_rejected(self):
        self.assertEqual(check(decision(), state()).violation.code, 'ask_first')

    def test_propose_after_an_answer_is_allowed(self):
        self.assertIsNone(check(decision(), state(answered=True, effect=True)).violation)

    def test_propose_cannot_target_another_segment(self):
        result = check(decision('p2'), state(answered=True, effect=True))
        self.assertEqual(result.violation.code, 'wrong_target')

    def test_explicit_direct_rewrite_skips_the_first_question(self):
        self.assertIsNone(check(decision(), state(direct=True, effect=True)).violation)

    def test_direct_rewrite_does_not_bypass_effect_gate(self):
        self.assertEqual(check(decision(), state(direct=True)).violation.code, 'ask_effect')


REQUIREMENTS = [{'requirementId': 'req-1', 'referenceId': 'ref-1',
                 'quote': '负责跨部门需求对接与排期推进'}]


def evidence_state(declined=True, followups=(), answer_kind='legacy'):
    doc = ExperienceDoc(experience_id='p1', seq=1, kind='project',
                        title='构造项目', raw_text=BEFORE, block_id='p1', block_revision=1)
    doc.questions.append(Question(id='q1', slot='responsibility',
                                  text='你负责哪部分？', fingerprint='f',
                                  outcome='declined', answer_kind=answer_kind))
    if declined:
        doc.declined = ['responsibility']
    doc.requirement_followups = list(followups)
    return GuardState(
        session=SessionContext(session=SessionInfo(session_id='test', experience_order=['p1']),
                               draft_blocks=[DraftBlock(block_id='p1', markdown=BEFORE)],
                               web_context={'available': True, 'requirements': REQUIREMENTS}),
        focus_doc=doc, anchor_text=BEFORE, focus_block_markdown=BEFORE,
        source_texts={'resume': BEFORE}, source_kinds={'resume': 'resume'})


def evidence_ask(**overrides):
    value = {'action': 'ask_user',
             'message': '岗位要求里提到「负责跨部门需求对接」，你有类似的对接经历吗？',
             'slot': 'responsibility', 'evidence_ref': 'req-1',
             'evidence_quote': '负责跨部门需求对接'}
    value.update(overrides)
    return AskStep(**value)


class RequirementEvidenceGuardTests(unittest.TestCase):
    def test_evidence_allows_one_declined_slot_followup(self):
        self.assertIsNone(check(evidence_ask(), evidence_state()).violation)

    def test_uncertain_decline_can_be_reopened_with_evidence(self):
        self.assertIsNone(check(evidence_ask(),
                                evidence_state(answer_kind='uncertain')).violation)

    def test_absent_or_refused_decline_cannot_be_reopened_with_evidence(self):
        # “没有”“不想说”是明确缺席或拒答，带证据也不能再问同一个槽。
        for kind in ('absent', 'refused'):
            with self.subTest(kind=kind):
                result = check(evidence_ask(), evidence_state(answer_kind=kind))
                self.assertEqual(result.violation.code, 'question_declined')

    def test_same_slot_cannot_be_followed_up_twice(self):
        result = check(evidence_ask(), evidence_state(followups=['responsibility']))
        self.assertEqual(result.violation.code, 'question_declined')

    def test_other_slot_requirement_followup_is_not_a_repeat(self):
        # 要求型模板只随槽位标签变化，跨槽相似度很高；另一个槽的首次追问
        # 不能被“最近问过类似问题”误拦，每槽各应有一次机会。
        state = evidence_state(followups=['responsibility'])
        first = state.focus_doc.questions[0]
        first.text = requirement_question(REQUIREMENTS[0]['quote'], 'responsibility')
        first.evidence_ref = 'req-1'
        first.evidence_quote = REQUIREMENTS[0]['quote']
        ask = evidence_ask(
            slot='action',
            message=requirement_question(REQUIREMENTS[0]['quote'], 'action'),
            evidence_quote=REQUIREMENTS[0]['quote'])
        self.assertIsNone(check(ask, state).violation)

    def test_non_declined_slot_followup_allowed_only_once(self):
        # partial/uncertain 槽不在 declined 中，但“每槽一次”上限同样生效：
        # 第一次带合法证据放行，换措辞再次追问同一槽要被拦。
        self.assertIsNone(check(evidence_ask(), evidence_state(declined=False)).violation)
        second = evidence_ask(
            message='岗位要求提到「排期推进」，你有推进排期的经历吗？')
        result = check(second, evidence_state(declined=False,
                                              followups=['responsibility']))
        self.assertEqual(result.violation.code, 'question_repeat')

    def test_declined_slot_without_evidence_stays_blocked(self):
        result = check(AskStep(action='ask_user', message='你负责哪部分？',
                               slot='responsibility'), evidence_state())
        self.assertEqual(result.violation.code, 'question_declined')

    def test_unknown_reference_or_non_verbatim_quote_is_rejected(self):
        for ask in (evidence_ask(evidence_ref='req-9'),
                    evidence_ask(evidence_quote='负责跨部门沟通')):
            with self.subTest(ask=ask):
                result = check(ask, evidence_state())
                self.assertEqual(result.violation.code, 'question_evidence_unverified')
                self.assertTrue(result.violation.retryable)

    def test_only_one_evidence_field_is_invalid(self):
        result = check(evidence_ask(evidence_quote=None), evidence_state())
        self.assertEqual(result.violation.code, 'question_evidence_invalid')

    def test_section_slots_cannot_carry_requirement_evidence(self):
        result = check(evidence_ask(slot='strength'), evidence_state())
        self.assertEqual(result.violation.code, 'question_evidence_invalid')
        self.assertFalse(result.violation.retryable)

    def test_evidence_quote_numbers_pass_the_number_check(self):
        state = evidence_state()
        state.session.web_context['requirements'] = [
            {'requirementId': 'req-1', 'referenceId': 'ref-1',
             'quote': '3年接口测试经验'}]
        ask = evidence_ask(evidence_quote='3年接口测试经验',
                           message='岗位要求里提到「3年接口测试经验」，你有相关工作吗？')
        self.assertIsNone(check(ask, state).violation)


if __name__ == '__main__':
    unittest.main()
