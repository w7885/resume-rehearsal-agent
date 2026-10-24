"""Reject JD/raw-chat/model-memory evidence for new personal numbers and seniority."""
from types import SimpleNamespace
import unittest

from app.agent.guard import GuardState, check
from app.agent.memory import ExperienceDoc, Fact
from app.agent.ports import SessionContext, SessionInfo, DraftBlock
from app.agent.schemas import StepDecisionAdapter
import test_goal_and_confirmation as integration


class PersonalEvidenceTests(unittest.TestCase):
    def probe(self, after, *, jd='', anchor='', supplement='', quote_supplement=False, memory=False,
              before='参与接口测试。'):
        texts = {'resume': before}; kinds = {'resume': 'resume'}
        refs = [{'source_id': 'resume', 'source_revision': 1, 'quote': '接口测试'}]
        if jd:
            texts['jd'], kinds['jd'] = jd, 'jd'
            refs.append({'source_id': 'jd', 'source_revision': 1, 'quote': jd})
        if supplement:
            texts['supplement'], kinds['supplement'] = supplement, 'supplement'
            if quote_supplement:
                refs.append({'source_id': 'supplement', 'source_revision': 1, 'quote': supplement})
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='project', title='构造项目', raw_text=before, block_id='p1', block_revision=1)
        if memory:
            doc.facts.append(Fact(id='unbacked-fact', slot='result', statement='效率提升30%。'))
        state = GuardState(session=SessionContext(session=SessionInfo(session_id='test', experience_order=['p1']),
                           draft_blocks=[DraftBlock(block_id='p1', markdown=before)]), focus_doc=doc,
                           anchor_text=before+'\n'+anchor, focus_block_markdown=before, source_texts=texts, source_kinds=kinds)
        decision = StepDecisionAdapter.validate_python({'action': 'tool', 'tool': 'propose_change', 'args': {
            'operation': 'replace', 'target': 'p1', 'before': before, 'after': after, 'reason': '构造事实边界测试', 'source_refs': refs, 'gaps': []}})
        return check(decision, state)

    def test_jd_years_are_not_personal_experience(self):
        result = self.probe('参与接口测试，具备3年经验。', jd='任职要求：3年测试经验。')
        self.assertEqual(result.violation.code, 'unsupported_number')

    def test_jd_leadership_does_not_upgrade_user_responsibility(self):
        result = self.probe('主导接口测试。', jd='岗位职责：主导接口测试。')
        self.assertEqual(result.violation.code, 'role_inflation')

    def test_hypothetical_chat_is_not_result_evidence(self):
        result = self.probe('参与接口测试，效率提升30%。', anchor='如果提升30%，可以这样写吗？')
        self.assertEqual(result.violation.code, 'unsupported_number')

    def test_unquoted_personal_source_cannot_back_an_addition(self):
        result = self.probe('参与接口测试，效率提升30%。', supplement='我测得效率提升30%。')
        self.assertEqual(result.violation.code, 'unsupported_number')

    def test_quoted_personal_supplement_can_back_a_number(self):
        result = self.probe('参与接口测试，效率提升30%。', supplement='我测得效率提升30%。', quote_supplement=True)
        self.assertIsNone(result.violation)

    def test_model_memory_without_personal_source_is_not_primary_evidence(self):
        result = self.probe('参与接口测试，效率提升30%。', memory=True)
        self.assertEqual(result.violation.code, 'unsupported_number')

    def test_existing_draft_numbers_and_roles_survive_rewording(self):
        result = self.probe('主导接口功能测试，有3年测试经验。', before='主导接口测试，有3年测试经验。')
        self.assertIsNone(result.violation)


class LeakingClient(integration.TargetClient):
    async def decide(self, **kwargs):
        self.steps += 1
        if self.steps > 1:
            return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed'})
        value = self.runtime.repo.get_session_context(self.runtime.context)
        block = value['draft']['blocks'][0]
        return SimpleNamespace(data={'action': 'tool', 'tool': 'propose_change', 'args': {
            'operation': 'replace', 'target': block['blockId'], 'before': block['markdown'],
            'after': '主导接口测试，具备3年测试经验。', 'reason': '构造错误建议', 'gaps': [],
            'source_refs': [{'source_id': s['sourceId'], 'source_revision': s['sourceRevision'], 'quote': s['text']} for s in value['sources']]}})


class SourceLeakIntegrationTests(unittest.TestCase):
    setUp = integration.GoalAndConfirmationTests.setUp
    request = integration.GoalAndConfirmationTests.request
    version = integration.GoalAndConfirmationTests.version
    chat = integration.GoalAndConfirmationTests.chat
    material = integration.GoalAndConfirmationTests.material
    send = integration.GoalAndConfirmationTests.send
    analyze = integration.GoalAndConfirmationTests.analyze
    conversation = integration.GoalAndConfirmationTests.conversation
    count = integration.GoalAndConfirmationTests.count

    def test_wrong_jd_claim_is_never_saved_as_a_suggestion_or_applied_by_yes(self):
        session = self.chat()
        self.material(session, text='参与接口测试。', role='resume')
        self.material(session, text='测试工程师JD\n任职要求：3年测试经验。岗位职责：主导接口测试。', role='jd', request_id='jd')
        host = self.app.state.agent_host
        host.client_factory = lambda runtime, budget: LeakingClient(runtime)
        user = self.send(session, '按照JD修改简历')
        self.analyze(session, user)
        self.assertEqual(self.count('suggestions'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '参与接口测试。')
        answer = self.send(session, '是', request_id='yes')
        self.analyze(session, answer, 'yes')
        self.assertEqual(self.count('suggestion_decisions'), 0)
        self.assertEqual(self.count('suggestions'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '参与接口测试。')


if __name__ == '__main__':
    unittest.main()
