"""Deletion must not race the active chat Host; budget remains project-wide."""
import unittest
from app import db
from app.agent_budget import ProjectBudget
import test_agent_integration as integration


class SessionDeleteTests(unittest.TestCase):
    setUp = integration.AgentIntegrationTests.setUp
    request = integration.AgentIntegrationTests.request
    version = integration.AgentIntegrationTests.version
    chat = integration.AgentIntegrationTests.chat
    send = integration.AgentIntegrationTests.send
    count = integration.AgentIntegrationTests.count

    def test_active_host_prevents_delete_then_cancel_allows_same_confirmed_revision(self):
        session = self.chat()
        user = self.send(session, '构造测试，不调用模型')
        host = self.app.state.agent_host
        host.client_factory = lambda runtime, budget: integration.FakeClient(runtime)
        row, _ = host.start(session['sessionId'], {'requestId': 'active', 'userMessageId': user['messageId'], 'expectedStateRevision': self.version(session['sessionId'])})
        payload = {'requestId': 'delete', 'expectedStateRevision': self.version(session['sessionId']), 'confirmed': True}
        result = self.request('DELETE', f"/api/sessions/{session['sessionId']}", data=payload)
        self.assertEqual(result['status'], 409, result)
        self.assertEqual(result['json']['error']['code'], 'run_busy')
        self.assertEqual(self.count('sessions'), 1)
        self.runner.run(host.cancel(row['root_run_id']))
        result = self.request('DELETE', f"/api/sessions/{session['sessionId']}", data=payload)
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(self.count('sessions'), 0)

    def test_delete_preserves_global_spend_and_other_session(self):
        target, other = self.chat(), self.chat(key='other')
        budget = ProjectBudget(self.path, 'isolated-budget')
        before = budget.status()
        with db.database(self.path) as connection:
            connection.execute("UPDATE model_budget_accounts SET spent_cents=123 WHERE budget_id='project'")
        result = self.request('DELETE', f"/api/sessions/{target['sessionId']}", data={'requestId': 'delete', 'expectedStateRevision': self.version(target['sessionId']), 'confirmed': True})
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(budget.status(), {**before, 'spentCents': 123, 'remainingCents': before['remainingCents'] - 123})
        self.assertEqual(self.request('GET', f"/api/sessions/{other['sessionId']}")['status'], 200)
