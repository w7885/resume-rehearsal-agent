"""A handles unique wording revisions through the existing confirmation transaction."""
from types import SimpleNamespace
import unittest

from app import db
from app.agent_revision import wording_feedback
from app.repository import Repository, RequestContext
from app.business_service import BusinessService
import test_goal_and_confirmation as integration

FEEDBACK = '太冗余了，有些表述太随意'


class RevisionClient(integration.TargetClient):
    async def decide(self, **kwargs):
        self.runtime.host.decision_contexts.append(kwargs['user'])
        if '## 当前段修改反馈' not in kwargs['user']:
            return await super().decide(**kwargs)
        if self.proposed:
            return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed'})
        self.proposed = True
        data = self.runtime.repo.get_session_context(self.runtime.context)
        block = data['draft']['blocks'][0]
        return SimpleNamespace(data={'action': 'tool', 'tool': 'propose_change', 'args': {
            'operation': 'replace', 'target': block['blockId'], 'before': block['markdown'],
            'after': '协助整理项目反馈与测试。', 'reason': '按反馈合并重复表述，使用书面表达', 'gaps': [],
            'source_refs': [{'source_id': r['sourceId'], 'source_revision': r['sourceRevision'], 'quote': r['quote']} for r in block['sourceRefs']]}})


class RevisionTests(unittest.TestCase):
    setUp = integration.GoalAndConfirmationTests.setUp
    request = integration.GoalAndConfirmationTests.request
    version = integration.GoalAndConfirmationTests.version
    chat = integration.GoalAndConfirmationTests.chat
    material = integration.GoalAndConfirmationTests.material
    send = integration.GoalAndConfirmationTests.send
    analyze = integration.GoalAndConfirmationTests.analyze
    conversation = integration.GoalAndConfirmationTests.conversation
    count = integration.GoalAndConfirmationTests.count
    ready = integration.GoalAndConfirmationTests.ready
    proposal = integration.GoalAndConfirmationTests.proposal
    run_context = integration.GoalAndConfirmationTests.run_context

    def test_unique_critique_revises_immediately_without_adopting_and_replays_once(self):
        session, _, host, prompt = self.proposal()
        original = self.conversation(session)['draft']['markdown']
        first = Repository(self.path).get_session_context(RequestContext(session['sessionId']))['suggestions'][0]
        host.client_factory = lambda runtime, budget: RevisionClient(runtime)
        user = self.send(session, FEEDBACK, request_id='critique')
        result = self.analyze(session, user, 'critique')
        self.assertEqual(result[-1]['payload']['outcome'], 'waiting_user')
        self.assertEqual(host.decision_calls, 0)
        repo = Repository(self.path)
        current = repo.get_session_context(RequestContext(session['sessionId']))
        self.assertEqual(current['draft']['markdown'], original)
        self.assertEqual(len(current['suggestions']), 1)
        self.assertNotEqual(current['suggestions'][0]['suggestionId'], first['suggestionId'])
        self.assertEqual(current['suggestions'][0]['after'], '协助整理项目反馈与测试。')
        self.assertIn(FEEDBACK, host.decision_contexts[-1])
        self.assertIn(first['after'], host.decision_contexts[-1])
        self.assertEqual(self.conversation(session)['messages'][-1]['assistant']['kind'], 'change_question')
        with db.database(self.path, read_only=True) as c:
            self.assertEqual(tuple(c.execute('SELECT decision,feedback FROM suggestion_decisions WHERE suggestion_id=?', (first['suggestionId'],)).fetchone()), ('revise', FEEDBACK))
            self.assertEqual(c.execute('SELECT count(*) FROM source_blocks WHERE text=?', (FEEDBACK,)).fetchone()[0], 0)
        count = self.count('suggestions')
        calls = len(host.decision_contexts)
        self.analyze(session, user, 'replay')
        self.assertEqual(self.count('suggestions'), count)
        self.assertEqual(len(host.decision_contexts), calls)

    def test_hypothetical_or_quoted_style_feedback_does_not_directly_resolve(self):
        for text in ('如果太冗余了怎么办？', '他说“太冗余了”', '太冗余了，但先不要改'):
            with self.subTest(text=text):
                session, _, host, prompt = self.proposal()
                user = self.send(session, text, request_id='ambiguous')
                self.analyze(session, user, 'ambiguous')
                self.assertGreater(host.decision_calls, 0)
                self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_style_feedback_with_multiple_candidates_does_not_choose_last(self):
        session, trigger, host, _ = self.proposal()
        repo = Repository(self.path)
        context = self.run_context(session)
        first = repo.get_session_context(context)['suggestions'][0]
        second = repo.save_suggestion(context, {'operation': 'replace', 'target': first['target'],
            'before': first['before'], 'after': '参与反馈整理与测试。', 'reason': '另一个候选', 'gaps': [], 'sourceRefs': first['sourceRefs']})
        prompt = BusinessService(repo).save_assistant_message(context, {'requestId': 'multi-style', 'userMessageId': trigger['messageId'],
            'content': '两条候选，请选择。', 'kind': 'change_question', 'suggestionIds': [first['suggestionId'], second['suggestionId']]}, synthetic=True)['data']['message']
        user = self.send(session, FEEDBACK, request_id='multi-feedback', replyToMessageId=prompt['messageId'])
        self.analyze(session, user, 'multi-feedback')
        self.assertGreater(host.decision_calls, 0)
        self.assertEqual(self.count('suggestion_decisions'), 0)


class WordingTests(unittest.TestCase):
    def test_style_critique_is_not_approval(self):
        self.assertTrue(wording_feedback(FEEDBACK))
        self.assertTrue(wording_feedback('请精简些，写得更正式'))
        self.assertFalse(wording_feedback('可以'))
        self.assertFalse(wording_feedback('不改了，虽然有点冗余'))
