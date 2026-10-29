"""Outer RouterAgent: one call, two axes, no write access."""
import unittest

from pydantic import ValidationError

from app.agent.schemas import RouterOutput


class RouterOutputSchemaTests(unittest.TestCase):
    def test_minimal_payload_defaults(self):
        out = RouterOutput.model_validate({'intent': 'answer'})
        self.assertEqual(out.intent, 'answer')
        self.assertIsNone(out.disposition)
        self.assertEqual(out.confidence, 0.0)
        self.assertFalse(out.answers_pending)
        self.assertEqual(out.mixed, [])

    def test_two_axes_are_independent(self):
        out = RouterOutput.model_validate({
            'disposition': 'resolve',
            'intent': 'focus',
            'target_experience_id': 'blk_2',
            'confidence': 0.9,
            'pending_resolution': {'decision': 'accept', 'suggestion_id': 'sg_1'},
        })
        self.assertEqual(out.disposition, 'resolve')
        self.assertEqual(out.intent, 'focus')
        self.assertEqual(out.pending_resolution.decision, 'accept')

    def test_rejects_unknown_intent(self):
        with self.assertRaises(ValidationError):
            RouterOutput.model_validate({'intent': 'confirm'})

    def test_rejects_confidence_out_of_range(self):
        with self.assertRaises(ValidationError):
            RouterOutput.model_validate({'intent': 'meta', 'confidence': 1.5})

    def test_mixed_items_are_parsed(self):
        out = RouterOutput.model_validate({
            'mixed': [
                {'disposition': 'suppress', 'quote': '谢谢'},
                {'intent': 'focus', 'target_experience_id': 'blk_2'},
            ]
        })
        self.assertEqual(len(out.mixed), 2)
        self.assertEqual(out.mixed[0].disposition, 'suppress')
        self.assertEqual(out.mixed[1].intent, 'focus')


from app.agent.llm import LLMError
from app.agent_router import (
    FALLBACK_BUDGET, FALLBACK_SEMANTIC, classify,
)


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def route(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ClassifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_payload_returns_output(self):
        llm = ScriptedLLM([{'intent': 'meta', 'confidence': 0.8}])
        result = await classify(llm, user='u')
        self.assertIsNone(result.fallback_reason)
        self.assertEqual(result.output.intent, 'meta')
        self.assertEqual(result.attempts, 1)

    async def test_invalid_payload_is_retried_once(self):
        llm = ScriptedLLM([{'intent': 'not_a_real_action'}, {'intent': 'meta'}])
        result = await classify(llm, user='u')
        self.assertIsNone(result.fallback_reason)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(llm.calls, 2)

    async def test_two_invalid_payloads_fall_back(self):
        llm = ScriptedLLM([{'intent': 'nope'}, {'intent': 'nope'}])
        result = await classify(llm, user='u')
        self.assertIsNone(result.output)
        self.assertEqual(result.fallback_reason, FALLBACK_SEMANTIC)
        self.assertEqual(result.attempts, 2)

    async def test_budget_exhausted_does_not_retry(self):
        llm = ScriptedLLM([LLMError('budget_exhausted', 'no budget', False)])
        result = await classify(llm, user='u')
        self.assertEqual(result.fallback_reason, FALLBACK_BUDGET)
        self.assertEqual(llm.calls, 1)


from app.agent_router import build_user_prompt


class UserPromptTests(unittest.TestCase):
    def test_includes_message_blocks_and_focus(self):
        prompt = build_user_prompt(
            message='第二段改短点',
            blocks=[{'id': 'blk_1', 'seq': 1, 'kind': 'project', 'title': '项目一'},
                    {'id': 'blk_2', 'seq': 2, 'kind': 'project', 'title': '项目二'}],
            focus_id='blk_2',
            question='你在项目二里负责哪部分？',
            suggestions=[],
            recent=['assistant: 好'],
        )
        self.assertIn('第二段改短点', prompt)
        self.assertIn('blk_2', prompt)
        self.assertIn('你在项目二里负责哪部分？', prompt)
        self.assertIn('blk_1', prompt)

    def test_lists_pending_suggestions_with_ids(self):
        prompt = build_user_prompt(
            message='太啰嗦了',
            blocks=[{'id': 'blk_1', 'seq': 1, 'kind': 'project', 'title': '项目一'}],
            focus_id='blk_1',
            question=None,
            suggestions=[{'id': 'sg_9', 'title': '项目一', 'after': '协助项目一测试。'}],
            recent=[],
        )
        self.assertIn('sg_9', prompt)
        self.assertIn('协助项目一测试。', prompt)

    def test_says_none_when_no_pending_question(self):
        prompt = build_user_prompt(
            message='继续', blocks=[], focus_id=None, question=None,
            suggestions=[], recent=[],
        )
        self.assertIn('待答问题：无', prompt)
