"""Deterministic ownership classification before any model call."""
import asyncio
from types import SimpleNamespace
import unittest

from app import web_intake
from app.agent.llm import LLMError
from app.web_citations import goal_state
from app.web_ownership import (_experience_title, _model_ownership, classify_ownership,
                               classify_text, resolve_ownership)
from app.web_target import _put
import test_web_storage

STATE = {'phase': 'awaiting_selection',
         'candidates': [{'candidateId': 'c1'}, {'candidateId': 'c2'}]}


class DeterministicOwnershipTests(unittest.TestCase):
    def test_bare_number_while_selecting_names_candidate(self):
        for text in ('2', '第二段', '第2个岗位', '第二个', '第 2 个'):
            with self.subTest(text=text):
                self.assertEqual(classify_text(text, STATE),
                                 {'decision': 'select_candidate', 'index': 2})

    def test_edit_verbs_win_over_bare_number(self):
        for text, index in (('改第2段', 2), ('第2段经历', 2), ('切换第二段', 2),
                            ('第2段改短点', 2), ('改第3段', 3)):
            with self.subTest(text=text):
                self.assertEqual(classify_text(text, STATE),
                                 {'decision': 'edit_experience', 'index': index})

    def test_ordinary_text_is_not_a_selection(self):
        for text in ('帮我看看简历', '继续', '今天天气不错'):
            with self.subTest(text=text):
                self.assertIsNone(classify_text(text, STATE))

    def test_number_outside_selection_is_not_a_selection(self):
        self.assertIsNone(classify_text('第二段', {'phase': 'ready', 'candidates': []}))

    def test_experience_title_forwards_only_unambiguous_headings(self):
        for markdown, expected in (
                ('三人团队开发校园报名工具，我负责需求整理，协助页面开发。', '未命名经历'),
                ('负责后端接口开发', '未命名经历'),
                ('- 整理用户反馈并合并需求', '未命名经历'),
                ('某大学 计算机科学与技术', '未命名经历'),
                ('# 校园报名工具', '校园报名工具'),
                ('**校园报名工具**', '校园报名工具'),
                ('校园报名工具    2024.03', '校园报名工具')):
            with self.subTest(markdown=markdown):
                self.assertEqual(_experience_title(markdown), expected)

    def test_decline_and_deterministic_requests(self):
        self.assertEqual(classify_text('不查了', None), {'decision': 'decline'})
        self.assertEqual(classify_text('https://example.com/jobs/1', None),
                         {'decision': 'deterministic_web'})
        self.assertEqual(classify_text('重试联网', None),
                         {'decision': 'deterministic_web'})
        self.assertEqual(classify_text('通用修改', None),
                         {'decision': 'deterministic_web'})


class OwnershipRuntimeTests(unittest.TestCase):
    def setUp(self):
        test_web_storage.WebStorageTests.setUp(self)
        # 判定只读会话与接续状态，不绑定具体 run；用户上下文足以完成读写。
        self.context = self.user

    settings = test_web_storage.WebStorageTests.settings
    run_context = test_web_storage.WebStorageTests.run_context

    def awaiting(self, candidates):
        with self.repo._operation(self.context, write=True) as (c, s):
            base = goal_state(c, s)['baseHash']
            state = web_intake.ensure(c, s, base)
            state.update(phase='awaiting_selection', candidates=candidates,
                         candidatePrompt={'messageId': 'prompt-1'},
                         revision=state['revision'] + 1)
            _put(c, self.sid, web_intake.KEY, state)
            return state

    def runtime(self, payload=None):
        calls = []

        async def _json(instruction, value):
            calls.append((instruction, value))
            if isinstance(payload, Exception):
                raise payload
            return payload

        runtime = SimpleNamespace(repo=self.repo, context=self.context, _json=_json)
        runtime.calls = calls
        return runtime

    def turn(self, text, reply=None):
        return {'userMessage': {'content': text}, 'replyToMessageId': reply}

    def test_bare_segment_word_selects_candidate_while_awaiting(self):
        self.settings(True, 1)
        self.awaiting([{'candidateId': 'c1', 'company': 'A', 'role': 'R1'},
                       {'candidateId': 'c2', 'company': 'B', 'role': 'R2'}])
        result = asyncio.run(classify_ownership(self.runtime(), self.turn('第二段')))
        self.assertEqual(result, {'decision': 'select_candidate', 'index': 2})

    def test_bound_reply_without_number_asks_selection_again(self):
        self.settings(True, 1)
        self.awaiting([{'candidateId': 'c1', 'company': 'A', 'role': 'R1'}])
        result = asyncio.run(classify_ownership(self.runtime(), self.turn('继续', 'prompt-1')))
        self.assertEqual(result, {'decision': 'select_candidate', 'index': None})

    def test_bound_reply_beats_edit_verb(self):
        self.settings(True, 1)
        self.awaiting([{'candidateId': 'c1', 'company': 'A', 'role': 'R1'}])
        result = asyncio.run(classify_ownership(self.runtime(), self.turn('改第2段', 'prompt-1')))
        self.assertEqual(result['decision'], 'select_candidate')

    def test_ordinary_text_uses_bounded_classifier_once(self):
        self.settings(True, 1)
        self.awaiting([{'candidateId': 'c1', 'company': 'A', 'role': 'R1'}])
        runtime = self.runtime({'decision': 'normal', 'confidence': 0.9})
        self.assertIsNone(asyncio.run(classify_ownership(runtime, self.turn('帮我看看简历'))))
        self.assertEqual(len(runtime.calls), 1)

    def test_low_confidence_timeout_and_model_error_fall_back(self):
        self.settings(True, 1)
        self.awaiting([{'candidateId': 'c1', 'company': 'A', 'role': 'R1'}])
        for payload in ({'decision': 'select_candidate', 'index': 1, 'confidence': 0.5},
                        LLMError('budget_exhausted', 'no budget', False)):
            with self.subTest(payload=payload):
                self.assertIsNone(asyncio.run(
                    classify_ownership(self.runtime(payload), self.turn('帮我看看简历'))))

    def test_ready_state_never_calls_the_classifier(self):
        self.settings(True, 1)
        with self.repo._operation(self.context, write=True) as (c, s):
            state = web_intake.ensure(c, s, goal_state(c, s)['baseHash'])
            state.update(phase='ready', revision=state['revision'] + 1)
            _put(c, self.sid, web_intake.KEY, state)
        runtime = self.runtime({'decision': 'normal', 'confidence': 0.9})
        self.assertIsNone(asyncio.run(classify_ownership(runtime, self.turn('帮我看看简历'))))
        self.assertEqual(runtime.calls, [])

    def test_resolve_ownership_hands_off_selection_only(self):
        self.settings(True, 1)
        self.awaiting([{'candidateId': 'c1', 'company': 'A', 'role': 'R1'}])
        runtime = self.runtime()
        turn = self.turn('第一段')
        self.assertTrue(asyncio.run(resolve_ownership(runtime, turn)))
        self.assertEqual(runtime.web_ownership['decision'], 'select_candidate')
        self.assertEqual(turn['webOwnership']['index'], 1)

        edit_turn = self.turn('改第2段')
        self.assertFalse(asyncio.run(resolve_ownership(runtime, edit_turn)))
        self.assertEqual(runtime.web_ownership['decision'], 'edit_experience')

    def test_classifier_input_contains_no_resume_body_or_url(self):
        self.settings(True, 1)
        self.awaiting([{'candidateId': 'c1', 'company': 'A', 'role': 'R1'}])
        runtime = self.runtime({'decision': 'select_candidate', 'index': 1, 'confidence': 0.9})
        asyncio.run(classify_ownership(runtime, self.turn('帮我看看简历')))
        payload = runtime.calls[0][1]
        self.assertEqual(set(payload), {'message', 'phase', 'candidates', 'experiences'})
        self.assertNotIn('https://', str(payload))
        self.assertNotIn('resume', str(payload).lower())
        # 首行就是简历正文的段落不能把正文当标题发给模型。
        self.assertNotIn('需求整理', str(payload))
        self.assertEqual(payload['experiences'][0]['title'], '未命名经历')
