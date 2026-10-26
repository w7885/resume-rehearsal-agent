"""Constructed malicious decisions through the actual Host; no provider calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import test_goal_and_confirmation as integration
from app.repository import Repository, RequestContext


class QualityHostBoundaryTests(unittest.TestCase):
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

    def test_complete_original_reaches_pending_proposal_without_reinterview(self):
        original = ('我负责查询模块；使用Python实现姓名模糊查询；'
                    '将查询耗时从10分钟缩短到3分钟。')

        class DirectProposalClient(integration.TargetClient):
            async def decide(self, **kwargs):
                if self.proposed:
                    return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed'})
                self.proposed = True
                data = self.runtime.repo.get_session_context(self.runtime.context)
                block = data['draft']['blocks'][0]
                return SimpleNamespace(data={'action': 'tool', 'tool': 'propose_change', 'args': {
                    'operation': 'replace', 'target': block['blockId'], 'before': block['markdown'],
                    'after': original.replace('我负责', '负责'), 'reason': '整理已有明确事实', 'gaps': [],
                    'source_refs': [{'source_id': r['sourceId'], 'source_revision': r['sourceRevision'],
                                     'quote': r['quote']} for r in block['sourceRefs']]}})

        with patch.object(httpx.AsyncHTTPTransport, 'handle_async_request',
                          side_effect=AssertionError('Offline test: network forbidden')):
            session = self.chat()
            self.material(session, text=original, role='resume')
            self.material(session, text='开发实习JD\n任职要求：Python与SQL。', role='jd', request_id='jd')
            self.app.state.agent_host.client_factory = lambda runtime, budget: DirectProposalClient(runtime)
            user = self.send(session, '请整理这段项目经历。')
            events = self.analyze(session, user)
            self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user')
            value = self.conversation(session)
            self.assertEqual(value['messages'][-1]['assistant']['kind'], 'change_question')
            self.assertFalse(any(m.get('assistant', {}).get('kind') == 'question'
                                 for m in value['messages'] if m.get('assistant')))
            self.assertEqual(value['draft']['markdown'], original)
            current = Repository(self.path).get_session_context(RequestContext(session['sessionId']))
            self.assertEqual(len(current['suggestions']), 1)
            reply = self.send(session, '是', request_id='complete-original-accept')
            self.analyze(session, reply, 'complete-original-accept')
            self.assertEqual(self.conversation(session)['draft']['markdown'], original.replace('我负责', '负责'))

    def test_unsubstantiated_credentials_and_skill_levels_never_reach_confirmation(self):
        base = integration.TargetClient
        for claim in ('已取得执业医师资格证', '熟练掌握Python'):
            with self.subTest(claim=claim):
                class InjectedDecisionClient(base):
                    async def decide(self, **kwargs):
                        result = await super().decide(**kwargs)
                        if result.data.get('tool') == 'propose_change':
                            result.data['args']['after'] = '协助整理反馈，' + claim + '。'
                        return result

                with patch.object(integration, 'TargetClient', InjectedDecisionClient), \
                     patch.object(httpx.AsyncHTTPTransport, 'handle_async_request',
                                  side_effect=AssertionError('Offline test: network forbidden')):
                    session, _, _, _ = self.ready()
                    repo = Repository(self.path)
                    context = RequestContext(session['sessionId'])
                    original = self.conversation(session)['draft']['markdown']
                    self.assertEqual(repo.get_session_context(context)['suggestions'], [])
                    user = self.send(session, '是', request_id='unsupported-claim-yes')
                    self.analyze(session, user, 'unsupported-claim-yes')
                    self.assertEqual(repo.get_session_context(context)['suggestions'], [])
                    self.assertEqual(self.conversation(session)['draft']['markdown'], original)


if __name__ == '__main__':
    unittest.main()
