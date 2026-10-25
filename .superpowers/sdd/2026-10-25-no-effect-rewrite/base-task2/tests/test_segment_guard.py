"""Each segment must be asked about before rewriting, and only the focus segment."""
import unittest

from app.agent.guard import GuardState, check
from app.agent.memory import ExperienceDoc, Fact, Question
from app.agent.ports import DraftBlock, SessionContext, SessionInfo
from app.agent.schemas import StepDecisionAdapter

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


if __name__ == '__main__':
    unittest.main()
