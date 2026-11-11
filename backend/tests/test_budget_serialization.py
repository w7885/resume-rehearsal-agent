"""Budget replies retain integer cents across PostgreSQL aggregate types."""
from contextlib import nullcontext
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock, patch

from app.agent_budget import ProjectBudget


class BudgetSerializationTests(unittest.TestCase):
    def test_postgres_decimal_aggregate_is_json_safe_without_resetting_costs(self):
        for reserved, expected in [(Decimal('0'), 471), (Decimal('125'), 346)]:
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = (reserved,)
            account = {'limit_cents': 491, 'spent_cents': 20}
            with self.subTest(reserved=reserved), \
                    patch('app.agent_budget.database', return_value=nullcontext(connection)), \
                    patch('app.agent_budget.transaction', return_value=nullcontext()), \
                    patch.object(ProjectBudget, '_account', return_value=account):
                value = ProjectBudget('unused-test-target', 'synthetic', 491).status()
            self.assertTrue(all(type(v) is int for v in value.values()))
            self.assertEqual(value['remainingCents'], expected)
            self.assertEqual(value['spentCents'], 20)
            self.assertEqual(json.loads(json.dumps(value)), value)
