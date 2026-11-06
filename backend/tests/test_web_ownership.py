"""Deterministic ownership classification before any model call."""
import unittest

from app.web_ownership import classify_text

STATE = {'phase': 'awaiting_selection',
         'candidates': [{'candidateId': 'c1'}, {'candidateId': 'c2'}]}


class DeterministicOwnershipTests(unittest.TestCase):
    def test_bare_number_while_selecting_names_candidate(self):
        for text in ('2', '第二段', '第2个岗位', '第二个', '第 2 个'):
            with self.subTest(text=text):
                self.assertEqual(classify_text(text, STATE),
                                 {'decision': 'select_candidate', 'index': 2})

    def test_edit_verbs_win_over_bare_number(self):
        for text in ('改第2段', '第2段经历', '切换第二段', '第2段改短点'):
            with self.subTest(text=text):
                self.assertEqual(classify_text(text, STATE)['decision'], 'edit_experience')

    def test_ordinary_text_is_not_a_selection(self):
        for text in ('帮我看看简历', '继续', '今天天气不错'):
            with self.subTest(text=text):
                self.assertIsNone(classify_text(text, STATE))

    def test_number_outside_selection_is_not_a_selection(self):
        self.assertIsNone(classify_text('第二段', {'phase': 'ready', 'candidates': []}))

    def test_decline_and_deterministic_requests(self):
        self.assertEqual(classify_text('不查了', None), {'decision': 'decline'})
        self.assertEqual(classify_text('https://example.com/jobs/1', None),
                         {'decision': 'deterministic_web'})
        self.assertEqual(classify_text('重试联网', None),
                         {'decision': 'deterministic_web'})
        self.assertEqual(classify_text('通用修改', None),
                         {'decision': 'deterministic_web'})
