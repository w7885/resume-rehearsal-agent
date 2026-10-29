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
