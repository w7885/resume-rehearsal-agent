"""Setting events are not user messages or repeat chat decisions. No live I/O."""
import json
import unittest
import asyncio
from types import SimpleNamespace
import httpx

from app import web_control
from app.db import database, transaction
from app.repository import Repository, RequestContext
import test_web_intake_host as fixture


class ControlTests(unittest.TestCase):
    setUp = fixture.IntakeHostTests.setUp
    request = fixture.IntakeHostTests.request
    version = fixture.IntakeHostTests.version
    chat = fixture.IntakeHostTests.chat
    material = fixture.IntakeHostTests.material
    send = fixture.IntakeHostTests.send
    analyze = fixture.IntakeHostTests.analyze
    conversation = fixture.IntakeHostTests.conversation
    count = fixture.IntakeHostTests.count
    ready = fixture.IntakeHostTests.ready
    user_turn = fixture.IntakeHostTests.user_turn

    def pending(self, session):
        return self.request('GET', f"/api/sessions/{session['sessionId']}/web-continuation")['json']['data']

    def signal(self, session, payload=None):
        pending = self.pending(session)
        body = payload or {key: pending[key] for key in ('requestId', 'webSettingsRevision', 'expectedStateRevision')}
        return self.request('POST', f"/api/sessions/{session['sessionId']}/web-continuation", data=body), body

    def test_toggle_event_explores_without_fake_user_message_or_normal_request(self):
        session = self.ready()
        self.send(session, '请按目标岗位整理技能。', request_id='saved-user')
        before = self.count('messages')
        response, _ = self.signal(session)
        self.assertEqual(response['status'], 200, response)
        value = self.conversation(session)
        self.assertIn('候选', value['messages'][-1]['content'])
        self.assertEqual(self.count('agent_run_requests'), 0)
        with database(self.path) as c:
            self.assertEqual(c.execute("select count(*) from messages where role='user'").fetchone()[0], 1)
        self.assertEqual(len(self.sent), 1)
        self.assertGreater(self.count('messages'), before)

    def test_replayed_event_does_not_search_or_add_messages(self):
        session = self.ready()
        self.send(session, '请按目标岗位整理技能。', request_id='saved-user')
        first, payload = self.signal(session)
        self.assertEqual(first['status'], 200)
        counts = (len(self.sent), self.count('messages'))
        replay, _ = self.signal(session, payload)
        self.assertEqual(replay['status'], 200, replay)
        self.assertEqual((len(self.sent), self.count('messages')), counts)

    def test_unknown_event_or_permission_cannot_start(self):
        session = self.ready()
        self.send(session, '请按目标岗位整理技能。', request_id='saved-user')
        pending = self.pending(session)
        for field, value in [('requestId', 'invented'), ('webSettingsRevision', 99)]:
            payload = {key: pending[key] for key in ('requestId', 'webSettingsRevision', 'expectedStateRevision')}
            payload[field] = value
            response, _ = self.signal(session, payload)
            self.assertEqual(response['json']['error']['code'], 'stale_state')
        self.assertEqual(self.sent, [])

    def test_normal_user_analysis_consumes_pending_toggle(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'normal')
        self.assertEqual(self.pending(session)['status'], 'done')
        self.assertEqual(self.count('agent_run_requests'), 1)
        self.assertEqual(len(self.sent), 1)

    def test_no_user_message_is_a_noop_without_model_or_search(self):
        session = self.ready()
        response, _ = self.signal(session)
        self.assertEqual(response['status'], 200, response)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.host.goal_calls, 0)
        self.assertEqual(self.pending(session)['status'], 'done')

    def test_busy_writer_leaves_event_pending(self):
        session = self.ready()
        self.send(session, '请按目标岗位整理技能。', request_id='saved-user')
        with database(self.path) as c, transaction(c):
            self.host._new_run(c, c.execute('select * from sessions where session_id=?', (session['sessionId'],)).fetchone())
        response, _ = self.signal(session)
        self.assertEqual(response['json']['error']['code'], 'run_busy')
        self.assertEqual(self.pending(session)['status'], 'pending')
        self.assertEqual(self.sent, [])

    def test_closing_invalidates_pending_event(self):
        session = self.ready()
        self.send(session, '请按目标岗位整理技能。', request_id='saved-user')
        pending = self.pending(session)
        self.request('PATCH', f"/api/sessions/{session['sessionId']}/web-settings",
            data={'requestId': 'off', 'enabled': False, 'expectedWebSettingsRevision': 2})
        response, _ = self.signal(session, {key: pending[key] for key in ('requestId', 'webSettingsRevision', 'expectedStateRevision')})
        self.assertEqual(response['json']['error']['code'], 'stale_state')
        self.assertEqual(self.sent, [])

    def test_event_does_not_replay_old_personal_answer_or_decision(self):
        session = self.ready()
        self.send(session, '采用', request_id='old-decision-text')
        response, _ = self.signal(session)
        self.assertEqual(response['status'], 200, response)
        self.assertEqual(self.count('suggestions'), 0)
        self.assertIn('候选', self.conversation(session)['messages'][-1]['content'])

    def test_saved_direct_link_reused_and_replay_keeps_initial_binding(self):
        session = self.ready(direction='')
        self.send(session, 'https://example.com/jobs/7615250934607792437', request_id='saved-link')
        result, payload = self.signal(session)
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Url', self.sent[0])
        self.assertIn('已核对：', '\n'.join(m['content'] for m in self.conversation(session)['messages']))
        self.assertEqual(self.pending(session)['expectedStateRevision'], payload['expectedStateRevision'])
        self.assertEqual(self.signal(session, payload)[0]['status'], 200)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.count('agent_turn_effects'), 0)
        self.assertEqual(self.count('suggestions'), 0)
        with database(self.path) as c:
            self.assertFalse(c.execute("SELECT 1 FROM source_blocks WHERE role='supplement' AND text LIKE '%example.com/jobs/%'").fetchone())

    def test_disable_during_provider_request_discards_results(self):
        session = self.ready()
        self.send(session, '请按目标岗位整理技能。', request_id='saved-user')
        template = self.host.web_service_factory(SimpleNamespace(repo=self.host.repo))
        def closing(request):
            self.sent.append(json.loads(request.content))
            self.host.service.update_web_settings(session['sessionId'], {'requestId': 'close-during-io',
                'enabled': False, 'expectedWebSettingsRevision': 2})
            from test_web_provider import search_response
            return httpx.Response(200, json=search_response())
        template.client.transport = httpx.MockTransport(closing)
        self.host.web_service_factory = lambda runtime: template
        result, _ = self.signal(session)
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.count('external_references'), 0)
        self.assertEqual(self.count('suggestions'), 0)
        self.assertEqual(self.pending(session)['status'], 'idle')

    def test_restart_marks_unfinished_control_interrupted_without_rerun(self):
        session = self.ready()
        self.send(session, '请按目标岗位整理技能。', request_id='saved-user')
        pending = self.pending(session)
        payload = {key: pending[key] for key in ('requestId', 'webSettingsRevision', 'expectedStateRevision')}
        row, created = web_control.start(self.host, session['sessionId'], payload)
        self.assertTrue(created)
        self.host.recover()
        self.assertEqual(self.pending(session)['status'], 'interrupted')
        replay, created = web_control.start(self.host, session['sessionId'], payload)
        self.assertFalse(created)
        self.assertEqual(row, replay)
        self.assertEqual(self.sent, [])

    def test_new_jd_saved_during_search_rejects_late_result_and_resumes(self):
        session = self.ready()
        sid = session['sessionId']
        self.send(session, '请按目标岗位整理技能。', request_id='original-target')
        template = self.host.web_service_factory(SimpleNamespace(repo=self.host.repo))
        original_transport = template.client.transport
        link = 'https://example.com/jobs/7615250934607792437'
        payload = {'requestId': 'new-target', 'content': link, 'attachmentIds': [],
                   'expectedStateRevision': self.version(sid)}
        saved = []

        def late_search(request):
            from test_web_provider import search_response
            saved.append(self.host.service.save_chat_message(sid, payload)['data']['message'])
            self.assertEqual(self.host.service.save_chat_message(sid, payload)['data']['message'], saved[0])
            return httpx.Response(200, json=search_response())

        template.client.transport = httpx.MockTransport(late_search)
        self.host.web_service_factory = lambda runtime: template
        self.assertEqual(self.signal(session)[0]['status'], 200)
        self.assertEqual(len(saved), 1)
        self.assertEqual(self.count('external_references'), 0)
        self.assertEqual(self.count('suggestions'), 0)
        template.client.transport = original_transport
        self.analyze(session, saved[0], 'resume-new-target')
        messages = self.conversation(session)['messages']
        self.assertEqual(sum(m['role'] == 'user' and m['content'] == link for m in messages), 1)
        self.assertIn('已核对：', '\n'.join(m['content'] for m in messages))

    def test_saved_supplement_reuses_verified_jd(self):
        session = self.ready(direction='')
        self.send(session, 'https://example.com/jobs/7615250934607792437', request_id='saved-jd')
        self.assertEqual(self.signal(session)[0]['status'], 200)
        self.assertEqual(len(self.sent), 1)
        sid = session['sessionId']
        supplement = '我在项目里用过 SQL 做报表。'
        payload = {'requestId': 'supplement', 'content': supplement, 'attachmentIds': [],
                   'expectedStateRevision': self.version(sid)}
        saved = self.host.service.save_chat_message(sid, payload)['data']['message']
        self.assertEqual(self.host.service.save_chat_message(sid, payload)['data']['message'], saved)
        self.analyze(session, saved, 'resume-supplement')
        self.assertEqual(len(self.sent), 1)
        messages = self.conversation(session)['messages']
        self.assertEqual(sum(m['role'] == 'user' and m['content'] == supplement for m in messages), 1)
        self.assertTrue(any(supplement in context for context in self.host.decide_contexts))

    def test_candidate_fetch_resumes_with_saved_supplement_and_same_posting(self):
        from test_web_provider import fetch_response, search_response
        from test_agent_web import TEXT
        session = self.ready()
        sid = session['sessionId']
        url = 'https://example.com/jobs/7615250934607792437'
        alternate = url + '?view=detail'
        supplement = '我在项目里用过 SQL 做报表。'
        template = self.host.web_service_factory(SimpleNamespace(repo=self.host.repo))
        calls, saved = [], []

        def provider(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if 'Url' not in payload:
                return httpx.Response(200, json=search_response(TEXT, url=alternate if 'site:' in payload['Query'] else url))
            if payload['Url'] == url:
                saved.append(self.host.service.save_chat_message(sid, {'requestId': 'candidate-supplement',
                    'content': supplement, 'attachmentIds': [], 'expectedStateRevision': choice_revision})['data']['message'])
            return httpx.Response(200, json=fetch_response(url=payload['Url'], text=TEXT))

        template.client.transport = httpx.MockTransport(provider)
        self.host.web_service_factory = lambda runtime: template
        self.user_turn(session, '请按目标岗位整理技能。', 'discover-candidate')
        prompt = self.conversation(session)['messages'][-1]
        choice = self.send(session, '1', request_id='selected', replyToMessageId=prompt['messageId'])
        choice_revision = self.version(sid)
        self.analyze(session, choice, 'read-candidate')
        self.assertEqual(len(saved), 1)
        http_value = self.conversation(session)
        self.assertEqual(http_value['pendingAnalysisMessageId'], saved[0]['messageId'])
        self.assertEqual(Repository(self.path).get_conversation(RequestContext(sid))['pendingAnalysisMessageId'],
                         http_value['pendingAnalysisMessageId'])
        self.analyze(session, saved[0], 'recover-candidate')
        self.assertEqual([p['Url'] for p in calls if 'Url' in p], [url, alternate])
        self.assertEqual(sum('site:' in p.get('Query', '') for p in calls), 1)
        self.assertNotIn('pendingAnalysisMessageId', self.conversation(session))
        self.assertNotIn('pendingAnalysisMessageId', Repository(self.path).get_conversation(RequestContext(sid)))
        self.assertIn('已核对：', '\n'.join(m['content'] for m in self.conversation(session)['messages']))
        self.assertTrue(any(supplement in context for context in self.host.decide_contexts))

    def test_supplement_during_direct_fetch_resumes_same_posting(self):
        from test_web_provider import fetch_response, search_response
        from test_agent_web import TEXT
        session = self.ready(direction='')
        sid = session['sessionId']
        link = 'https://example.com/jobs/7615250934607792437'
        alternate = link + '?view=detail'
        self.send(session, link, request_id='original-jd')
        supplement = '我在项目里用过 SQL 做报表。'
        payload = {'requestId': 'during-fetch', 'content': supplement, 'attachmentIds': [],
                   'expectedStateRevision': self.version(sid)}
        saved, fetches = [], []
        template = self.host.web_service_factory(SimpleNamespace(repo=self.host.repo))

        def provider(request):
            if request.url.path.endswith('/fetch'):
                url = json.loads(request.content)['Url']
                fetches.append(url)
                if url == link:
                    saved.append(self.host.service.save_chat_message(sid, payload)['data']['message'])
                return httpx.Response(200, json=fetch_response(url=url, text=TEXT))
            return httpx.Response(200, json=search_response(TEXT, url=alternate))

        template.client.transport = httpx.MockTransport(provider)
        self.host.web_service_factory = lambda runtime: template
        self.assertEqual(self.signal(session)[0]['status'], 200)
        self.assertEqual(self.count('external_references'), 0)
        self.analyze(session, saved[0], 'resume-after-supplement')
        self.assertEqual(fetches, [link, alternate])
        messages = self.conversation(session)['messages']
        self.assertIn('已核对：', '\n'.join(m['content'] for m in messages))
        self.assertTrue(any(supplement in context for context in self.host.decide_contexts))

    def test_cancel_releases_control_writer_before_normal_run(self):
        session = self.ready()
        old = self.send(session, '请按目标岗位整理技能。', request_id='saved-user')
        pending = self.pending(session)
        payload = {key: pending[key] for key in ('requestId', 'webSettingsRevision', 'expectedStateRevision')}
        web_control.start(self.host, session['sessionId'], payload)
        run_id = self.pending(session)['runId']
        result = asyncio.run(self.host.cancel(run_id))
        self.assertEqual(result['status'], 'interrupted')
        self.analyze(session, old, 'normal-after-stop')
        self.assertEqual(self.count('agent_run_requests'), 1)

    def test_reenable_keeps_episode_allowance_and_replaces_stale_candidates(self):
        session = self.ready()
        self.user_turn(session, '请按目标岗位整理技能。', 'normal')
        sid = session['sessionId']
        self.request('PATCH', f'/api/sessions/{sid}/web-settings',
            data={'requestId': 'off', 'enabled': False, 'expectedWebSettingsRevision': 2})
        self.request('PATCH', f'/api/sessions/{sid}/web-settings',
            data={'requestId': 'on', 'enabled': True, 'expectedWebSettingsRevision': 3})
        self.assertEqual(self.signal(session)[0]['status'], 200)
        self.assertEqual(len(self.sent), 2)
        with database(self.path) as c:
            from app.web_intake import load
            state = load(c, sid)
            self.assertEqual((state['searchCount'], state['permissionRevision']), (2, 4))
        self.assertIn('候选编号', self.conversation(session)['messages'][-1]['content'])
