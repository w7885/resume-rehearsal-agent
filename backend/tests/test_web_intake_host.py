"""Default Host intake flow with actual API, SQLite, and scripted provider/model."""
import json
from types import SimpleNamespace
import unittest

import httpx

from app.db import database, transaction
from app import web_intake
import test_agent_integration as integration
import test_web_host as legacy
from test_agent_web import TEXT, proof_for
from test_web_provider import search_response, fetch_response


class IntakeClient(legacy.WebAwareClient):
    async def complete_json(self, *, system, user, run_id='test'):
        value = json.loads(user)
        if system.startswith('解析联网搜索线索'):
            self.runtime.host.goal_calls += 1
            proof = value.get('goal', '') + value.get('targetReply', '')
            return SimpleNamespace(data={'company': '构造公司' if '构造公司' in proof else '',
                'role': '工程实习' if '工程实习' in proof else '', 'direction': '',
                'location': '广州' if '广州' in proof else '',
                'employment_type': '实习' if '实习' in proof else '', 'posting_id': ''})
        if system.startswith('提取公开岗位候选'):
            return SimpleNamespace(data={'candidates': [
                {'referenceId': ref['referenceId'], 'company': '构造公司', 'role': '工程实习',
                 'location': '广州' if '广州' in ref['content'] else '',
                 'employmentType': '实习' if '实习' in ref['content'] else ''} for ref in value['references'][:3]]})
        return await super().complete_json(system=system, user=user, run_id=run_id)


class IntakeHostTests(unittest.TestCase):
    setUp = integration.AgentIntegrationTests.setUp
    request = integration.AgentIntegrationTests.request
    version = integration.AgentIntegrationTests.version
    chat = integration.AgentIntegrationTests.chat
    material = integration.AgentIntegrationTests.material
    send = integration.AgentIntegrationTests.send
    analyze = integration.AgentIntegrationTests.analyze
    conversation = integration.AgentIntegrationTests.conversation
    count = integration.AgentIntegrationTests.count

    def ready(self, *, direction='构造公司 广州 工程实习 实习', with_jd=False):
        session = self.chat()
        self.material(session, text='技能：熟悉Python。', role='resume')
        if with_jd:
            self.material(session, text='构造公司工程实习JD\n岗位职责：负责接口开发与维护并完成必要测试。\n岗位要求：熟悉Python和HTTP，能够阅读代码并协作。', role='jd', request_id='jd')
        with database(self.path) as c, transaction(c):
            c.execute('UPDATE sessions SET direction=?,direction_status=? WHERE session_id=?',
                      (direction or '待确认求职方向', 'provided' if direction else 'pending', session['sessionId']))
        self.request('PATCH', f"/api/sessions/{session['sessionId']}/web-settings",
            data={'requestId': 'enable', 'enabled': True, 'expectedWebSettingsRevision': 1})
        self.host = self.app.state.agent_host
        self.host.goal_calls = 0
        self.host.assessment_calls = 0
        self.host.decide_contexts = []
        self.host.client_factory = lambda runtime, budget: IntakeClient(runtime)
        self.sent = []
        from app.web_provider import VolcWebClient, WebSettings
        from app.web_search import WebSearchService
        from test_web_provider import public_resolver
        def provider(request):
            self.sent.append(json.loads(request.content))
            result = fetch_response(url=json.loads(request.content)['Url'], text=TEXT) if request.url.path.endswith('/fetch') else search_response(TEXT)
            return httpx.Response(200, json=result)
        settings = WebSettings('synthetic-key', True, True)
        client = VolcWebClient(settings, transport=httpx.MockTransport(provider), resolver=public_resolver)
        self.host.web_service_factory = lambda runtime: WebSearchService(runtime.repo, settings, client=client)
        return session

    def user_turn(self, session, text, key, reply=None):
        args = {'request_id': key}
        if reply:
            args['replyToMessageId'] = reply
        user = self.send(session, text, **args)
        events = self.analyze(session, user, key)
        self.assertNotEqual(events[-1]['payload']['outcome'], 'agent_failed', events)
        return user

    def test_explore_waits_for_choice_before_any_section_announcement(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'explore')
        value = self.conversation(session)
        prompt = value['messages'][-1]
        self.assertEqual(prompt['assistant']['kind'], 'question', value)
        self.assertIn('候选', prompt['content'])
        self.assertFalse(any('现在进入' in m['content'] for m in value['messages']))
        self.assertEqual(self.count('suggestions'), 0)
        self.assertEqual(len(self.sent), 1)

    def test_choice_fetches_body_and_reaches_separate_citations(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'explore')
        prompt = self.conversation(session)['messages'][-1]
        self.user_turn(session, '1', 'choose', prompt['messageId'])
        value = self.conversation(session)
        self.assertEqual(value['messages'][-1]['assistant']['kind'], 'change_question', value)
        self.assertTrue(value['messages'][-1]['assistant']['externalReferences'])
        self.assertEqual(len(self.sent), 2)
        messages = [m['content'] for m in value['messages']]
        self.assertLess(next(i for i,t in enumerate(messages) if '已核对：' in t),
                        next(i for i,t in enumerate(messages) if '现在进入' in t))
        with database(self.path) as c:
            self.assertEqual(web_intake.load(c, session['sessionId'])['phase'], 'ready')

    def test_direct_jd_link_fetches_before_target_extraction_and_continues(self):
        session = self.ready(direction='')
        self.user_turn(session, 'https://example.com/jobs/7615250934607792437', 'link')
        value = self.conversation(session)
        self.assertEqual(self.host.goal_calls, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Url', self.sent[0])
        self.assertEqual(value['messages'][-1]['assistant']['kind'], 'change_question', value)
        self.assertTrue(value['messages'][-1]['assistant']['externalReferences'])

    def test_complete_jd_uses_existing_material_without_search(self):
        session = self.ready(with_jd=True)
        self.user_turn(session, '请按目标岗位整理技能。', 'provided')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.host.goal_calls, 0)

    def test_company_only_asks_direction_without_section_announcement(self):
        session = self.ready(direction='构造公司')
        self.user_turn(session, '想看这家公司的岗位。', 'company')
        value = self.conversation(session)
        self.assertIn('哪类岗位', value['messages'][-1]['content'])
        self.assertFalse(any('现在进入' in m['content'] for m in value['messages']))
        self.assertEqual(self.sent, [])

    def test_unselected_continue_never_modifies_or_refetches(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'explore')
        prompt = self.conversation(session)['messages'][-1]
        self.user_turn(session, '继续', 'ambiguous', prompt['messageId'])
        self.assertIn('候选编号', self.conversation(session)['messages'][-1]['content'])
        self.assertEqual(self.count('suggestions'), 0)
        self.assertEqual(len(self.sent), 1)

    def test_generic_choice_keeps_draft_and_proceeds_without_fetch(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'explore')
        prompt = self.conversation(session)['messages'][-1]
        self.user_turn(session, '通用修改', 'generic', prompt['messageId'])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.conversation(session)['messages'][-1]['assistant']['kind'], 'change_question')

    def test_bare_decline_defers_without_repeating_target_question(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'explore')
        prompt = self.conversation(session)['messages'][-1]
        self.user_turn(session, '不查了', 'decline', prompt['messageId'])
        self.assertEqual(len(self.sent), 1)
        with database(self.path) as c:
            self.assertEqual(web_intake.load(c, session['sessionId'])['phase'], 'deferred')
        self.assertNotIn('哪类岗位', self.conversation(session)['messages'][-1]['content'])

    def test_direction_reply_is_used_without_repeating_company_question(self):
        session = self.ready(direction='构造公司')
        self.user_turn(session, '想找岗位。', 'company')
        prompt = self.conversation(session)['messages'][-1]
        self.user_turn(session, '工程实习', 'direction', prompt['messageId'])
        self.assertEqual(len(self.sent), 1)
        self.assertIn('候选', self.conversation(session)['messages'][-1]['content'])
        self.assertEqual(self.count('suggestions'), 0)

    def test_replaying_same_turn_does_not_repeat_search_or_candidate_models(self):
        session = self.ready()
        user = self.user_turn(session, '请按目标岗位整理技能。', 'explore')
        counts = (len(self.sent), self.host.goal_calls, self.host.assessment_calls)
        self.analyze(session, user, 'explore')
        self.assertEqual((len(self.sent), self.host.goal_calls, self.host.assessment_calls), counts)

    def test_failed_link_recovery_stops_without_substituting_similar_job(self):
        session = self.ready()
        template = self.host.web_service_factory(SimpleNamespace(repo=self.app.state.business_service.repository))
        def failing_link(request):
            payload = json.loads(request.content)
            self.sent.append(payload)
            body = fetch_response(url=payload['Url'], code=404) if 'Url' in payload else search_response(TEXT)
            return httpx.Response(200, json=body)
        template.client.transport = httpx.MockTransport(failing_link)
        self.host.web_service_factory = lambda runtime: template
        self.user_turn(session, 'https://example.com/jobs/7615250934607792437', 'failed-link')
        value = self.conversation(session)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1]['Query'], 'site:example.com 7615250934607792437')
        self.assertIn('不会用相似岗位替代', value['messages'][-1]['content'])
        self.assertEqual(self.count('suggestions'), 0)
        self.assertFalse(any('现在进入' in m['content'] for m in value['messages']))

    def test_chinese_choice_is_explicit_and_does_not_become_personal_fact(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'explore')
        prompt = self.conversation(session)['messages'][-1]
        self.user_turn(session, '第一个', 'choose', prompt['messageId'])
        self.assertEqual(self.conversation(session)['messages'][-1]['assistant']['kind'], 'change_question')
        with database(self.path) as c:
            self.assertFalse(c.execute("SELECT 1 FROM source_blocks WHERE session_id=? AND role='supplement' AND text='第一个'",
                                       (session['sessionId'],)).fetchone())

    def test_single_candidate_yes_is_not_swallowed_by_social_router(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'explore')
        prompt = self.conversation(session)['messages'][-1]
        class MustNotRouteChoice(IntakeClient):
            async def route(self, **kwargs):
                raise AssertionError('A bound candidate answer must bypass experience routing')
        self.host.client_factory = lambda runtime, budget: MustNotRouteChoice(runtime)
        self.user_turn(session, '是的', 'yes', prompt['messageId'])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.conversation(session)['messages'][-1]['assistant']['kind'], 'change_question')
