"""Natural questions without automatic name/title quotation prefixes."""
import unittest
from app.agent.guard import GuardState, check
from app.agent.loop import _fallback_question
from app.agent.memory import ExperienceDoc
from app.agent.ports import SessionContext, SessionInfo
from app.agent.schemas import AskStep


class QuestionWordingTests(unittest.TestCase):
    def state(self, title='张明'):
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='project', title=title, raw_text='参与接口测试。')
        return GuardState(session=SessionContext(session=SessionInfo(session_id='test')), focus_doc=doc, anchor_text=doc.raw_text)

    def test_fallback_does_not_prefix_name_missing_title_or_raw_text(self):
        for title in ('张明', '无', '', '校园平台'):
            state = self.state(title)
            question, slot = _fallback_question(state.focus_doc)
            self.assertEqual(question, '这段经历中，你本人负责哪部分？')
            self.assertEqual(slot, 'responsibility')
            self.assertIsNone(check(AskStep(action='ask_user', message=question, slot=slot), state).violation)

    def test_natural_question_needs_no_verbatim_excerpt(self):
        result = check(AskStep(action='ask_user', message='你具体负责哪部分工作？', slot='responsibility'), self.state())
        self.assertIsNone(result.violation)

    def test_model_generated_prefix_is_rejected_for_natural_retry(self):
        for prefix in ('「张明」', '【无】', '[校园平台]', '“参与接口测试”'):
            result = check(AskStep(action='ask_user', message=prefix + '这部分是你负责的吗？', slot='responsibility'), self.state())
            self.assertEqual(result.violation.code, 'question_prefix')

    def test_other_segment_and_repeated_declined_question_checks_remain(self):
        state = self.state()
        state.other_titles = ['商城项目']
        result = check(AskStep(action='ask_user', message='商城项目中你负责哪部分？', slot='responsibility'), state)
        self.assertEqual(result.violation.code, 'question_cross_segment')
        state.focus_doc.declined = ['responsibility']
        result = check(AskStep(action='ask_user', message='你具体负责哪部分工作？', slot='responsibility'), state)
        self.assertEqual(result.violation.code, 'question_declined')
