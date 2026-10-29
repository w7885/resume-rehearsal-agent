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


from app.agent.route import normalize


def _docs():
    from app.agent.memory import ExperienceDoc
    return [
        ExperienceDoc(experience_id='blk_1', seq=1, kind='project', title='项目一', raw_text='参与项目一。'),
        ExperienceDoc(experience_id='blk_2', seq=2, kind='project', title='项目二', raw_text='参与项目二。'),
    ]


class NormalizeTests(unittest.TestCase):
    def test_revise_feedback_must_be_substring_of_quote(self):
        out = RouterOutput.model_validate({
            'disposition': 'resolve',
            'confidence': 0.9,
            'quote': '这段太啰嗦',
            'pending_resolution': {'decision': 'revise', 'suggestion_id': 'sg_1',
                                   'feedback': '希望精简这段内容'},
        })
        result = normalize(out, message='这段太啰嗦', docs=_docs(), phases={}, suggestion_ids={'sg_1'})
        self.assertIsNone(result.pending_resolution.decision)

    def test_revise_feedback_substring_is_kept(self):
        out = RouterOutput.model_validate({
            'disposition': 'resolve',
            'confidence': 0.9,
            'quote': '这段太啰嗦',
            'pending_resolution': {'decision': 'revise', 'suggestion_id': 'sg_1',
                                   'feedback': '太啰嗦'},
        })
        result = normalize(out, message='这段太啰嗦', docs=_docs(), phases={}, suggestion_ids={'sg_1'})
        self.assertEqual(result.pending_resolution.decision, 'revise')

    def test_quote_must_come_from_message(self):
        out = RouterOutput.model_validate({
            'disposition': 'resolve', 'confidence': 0.9, 'quote': '编造的一句',
            'pending_resolution': {'decision': 'revise', 'suggestion_id': 'sg_1',
                                   'feedback': '编造的一句'},
        })
        result = normalize(out, message='这段太啰嗦', docs=_docs(), phases={}, suggestion_ids={'sg_1'})
        self.assertIsNone(result.pending_resolution.decision)

    def test_unknown_suggestion_id_clears_decision(self):
        out = RouterOutput.model_validate({
            'disposition': 'resolve',
            'confidence': 0.9,
            'pending_resolution': {'decision': 'accept', 'suggestion_id': 'sg_ghost'},
        })
        result = normalize(out, message='采用', docs=_docs(), phases={}, suggestion_ids={'sg_1'})
        self.assertIsNone(result.pending_resolution.decision)

    def test_hallucinated_clarification_candidates_are_dropped(self):
        out = RouterOutput.model_validate({
            'intent': 'clarify', 'confidence': 0.9,
            'clarification': {'code': 'multiple_match', 'question': '改哪一段？',
                              'quoted_reference': '那段', 'candidates': []},
        })
        # 模型给的是不存在的 id，Pydantic 无法拒绝，normalize 必须剔除
        out.clarification.candidates = [type('C', (), {'experience_id': 'blk_ghost'})()]
        result = normalize(out, message='那段改短', docs=_docs(), phases={}, suggestion_ids=set())
        ids = [c.experience_id for c in result.clarification.candidates]
        self.assertNotIn('blk_ghost', ids)
        self.assertEqual(ids, ['blk_1', 'blk_2'])

    def test_mixed_with_two_intents_is_downgraded_to_clarify(self):
        out = RouterOutput.model_validate({
            'confidence': 0.9,
            'mixed': [{'intent': 'focus', 'target_experience_id': 'blk_1'},
                      {'intent': 'focus', 'target_experience_id': 'blk_2'}],
        })
        result = normalize(out, message='两段都改', docs=_docs(), phases={}, suggestion_ids=set())
        self.assertEqual(result.intent, 'clarify')
        self.assertEqual(result.mixed, [])

    def test_low_confidence_becomes_meta(self):
        out = RouterOutput.model_validate({'intent': 'focus', 'target_experience_id': 'blk_1',
                                           'confidence': 0.3})
        result = normalize(out, message='嗯', docs=_docs(), phases={}, suggestion_ids=set())
        self.assertEqual(result.intent, 'meta')

    def test_suppress_kind_requires_suppress(self):
        out = RouterOutput.model_validate({'intent': 'meta', 'suppress_kind': 'thanks'})
        result = normalize(out, message='谢谢', docs=_docs(), phases={}, suggestion_ids=set())
        self.assertIsNone(result.suppress_kind)


from app.agent.ports import SessionContext, SessionInfo
from app.agent.route import route_message


class NoCallLLM:
    async def route(self, **kwargs):
        raise AssertionError('injected decision must not call the model')


from types import SimpleNamespace
from unittest.mock import patch

from app.agent_host import Runtime


class DispositionConsumptionTests(unittest.TestCase):
    def _runtime(self):
        return SimpleNamespace(_reply=lambda *a, **k: self.fail('suppress 分支不应自行回复'),
                               _pending=lambda: [])

    def test_model_ack_defers_to_pending_suggestion_guard(self):
        out = RouterOutput.model_validate({'disposition': 'suppress', 'suppress_kind': 'ack'})
        with patch('app.agent_social.handle', return_value=False) as guarded:
            self.assertFalse(Runtime._consume_disposition(
                self._runtime(), {'userMessage': {'content': '好的'}}, out))
        guarded.assert_called_once()
        self.assertEqual(guarded.call_args.kwargs['kind'], 'ack')

    def test_courtesy_is_consumed_when_guard_accepts(self):
        out = RouterOutput.model_validate({'disposition': 'suppress', 'suppress_kind': 'thanks'})
        with patch('app.agent_social.handle', return_value=True):
            self.assertTrue(Runtime._consume_disposition(
                self._runtime(), {'userMessage': {'content': '谢谢'}}, out))

    def test_resume_does_not_end_the_turn(self):
        out = RouterOutput.model_validate({'disposition': 'resume'})
        self.assertFalse(Runtime._consume_disposition(
            self._runtime(), {'userMessage': {'content': '继续'}}, out))


class RouteMessageInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_decision_skips_model(self):
        ctx = SessionContext(session=SessionInfo(session_id='s1'))
        ctx.session.current_focus_id = 'blk_1'
        result = await route_message(
            message='继续', ctx=ctx, docs=_docs(), phases={}, pending=None,
            has_pending_question=False, llm=NoCallLLM(),
            decision=RouterOutput.model_validate({'intent': 'meta', 'confidence': 0.9,
                                                  'reply': '我们按你的节奏来。'}),
        )
        self.assertEqual(result.action, 'meta')
        self.assertEqual(result.reply, '我们按你的节奏来。')
        self.assertTrue(result.used_model)
