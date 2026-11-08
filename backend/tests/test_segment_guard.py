"""Each segment must be asked about before rewriting, and only the focus segment."""
import unittest

from app.agent.guard import MAX_SLOT_FOLLOWUPS, GuardState, check
from app.agent.memory import ExperienceDoc, Fact, Question, followup_count
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
    for index, slot in enumerate(followups, start=2):
        doc.questions.append(Question(
            id=f'q{index}', slot=slot, text=f'岗位要求追问{index}？', fingerprint=f'f{index}',
            evidence_ref='req-1', evidence_quote='负责跨部门需求对接'))
    if declined:
        doc.declined = ['responsibility']
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

    def test_followup_count_excludes_first_question(self):
        doc = evidence_state(declined=False).focus_doc
        self.assertEqual(followup_count(doc, 'responsibility'), 0)
        doc.questions.append(Question(id='q2', slot='responsibility',
                                      text='第二问？', fingerprint='f2'))
        self.assertEqual(followup_count(doc, 'responsibility'), 1)
        self.assertEqual(MAX_SLOT_FOLLOWUPS, 2)

    def test_second_same_slot_followup_is_allowed(self):
        self.assertIsNone(
            check(evidence_ask(), evidence_state(followups=['responsibility'])).violation)

    def test_third_same_slot_followup_is_rejected(self):
        result = check(evidence_ask(),
                       evidence_state(followups=['responsibility', 'responsibility']))
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

    def test_non_declined_slot_allows_two_followups_then_blocks(self):
        self.assertIsNone(
            check(evidence_ask(), evidence_state(declined=False)).violation)
        self.assertIsNone(check(
            evidence_ask(message='岗位要求提到「排期推进」，你有推进排期的经历吗？'),
            evidence_state(declined=False, followups=['responsibility'])).violation)
        result = check(
            evidence_ask(message='岗位要求提到「跨部门协作」，你参与过协作吗？'),
            evidence_state(declined=False, followups=['responsibility', 'responsibility']))
        self.assertEqual(result.violation.code, 'question_repeat')

    def test_identical_question_is_rejected_even_with_evidence(self):
        state = evidence_state(declined=False)
        state.focus_doc.questions.append(Question(
            id='q2', slot='responsibility',
            text='岗位要求里提到「负责跨部门需求对接」，你有类似的对接经历吗？',
            fingerprint='f2', evidence_ref='req-1', evidence_quote='负责跨部门需求对接'))
        result = check(evidence_ask(), state)
        self.assertEqual(result.violation.code, 'question_repeat')

    def test_effect_slot_stays_on_effect_gate(self):
        state = evidence_state(declined=False)
        for index in range(2):
            state.focus_doc.questions.append(Question(
                id=f'e{index}', slot='effect', text=f'效果问题{index}？',
                fingerprint=f'ef{index}'))
        result = check(AskStep(action='ask_user',
                               message='这段经历带来了什么实际变化？',
                               slot='effect'), state)
        self.assertEqual(result.violation.code, 'effect_exhausted')

    def test_declined_slot_without_evidence_stays_blocked(self):
        result = check(AskStep(action='ask_user', message='你负责哪部分？',
                               slot='responsibility'), evidence_state())
        self.assertEqual(result.violation.code, 'question_declined')

    def test_unverified_evidence_becomes_a_plain_question(self):
        # 只是借了岗位领域词、并非逐字引用时，按普通问句放行。
        for ask in (evidence_ask(evidence_ref='req-9', slot='action'),
                    evidence_ask(evidence_quote='负责跨部门沟通', slot='action'),
                    evidence_ask(evidence_quote=None, slot='action')):
            with self.subTest(ask=ask):
                self.assertIsNone(check(ask, evidence_state(declined=False)).violation)

    def test_unverified_evidence_cannot_reopen_declined_slot(self):
        result = check(evidence_ask(evidence_ref='req-9'), evidence_state())
        self.assertEqual(result.violation.code, 'question_declined')

    def test_section_slots_cannot_carry_requirement_evidence(self):
        self.assertIsNone(check(evidence_ask(slot='strength'),
                                evidence_state(declined=False)).violation)

    def test_unverified_quote_does_not_exempt_numbers(self):
        ask = evidence_ask(evidence_quote='3年跨部门经验',
                           message='岗位要求提到「3年跨部门经验」，你有这类经历吗？',
                           slot='action')
        self.assertEqual(check(ask, evidence_state(declined=False)).violation.code,
                         'question_new_number')

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
