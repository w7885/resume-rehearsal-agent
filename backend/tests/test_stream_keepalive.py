"""Real Host persistence with bounded artificial waits; no paid model calls."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import agent_host
import test_agent_integration as integration


class StreamKeepaliveTests(unittest.TestCase):
    for _name in ('setUp', 'request', 'version', 'material', 'chat', 'send', 'count', 'ready', 'conversation'):
        locals()[_name] = getattr(integration.AgentIntegrationTests, _name)

    def raw_stream(self, session, user, key):
        return self.request('POST', f"/api/sessions/{session['sessionId']}/runs", data={
            'requestId':key, 'userMessageId':user['messageId'],
            'expectedStateRevision':self.version(session['sessionId'])})

    def test_idle_model_wait_has_comments_without_persisting_or_resequencing_events(self):
        session, user = self.ready()
        original = agent_host.Runtime._materials
        async def slow(runtime, turn):
            await asyncio.sleep(.06)
            return await original(runtime, turn)
        with patch.object(agent_host, 'STREAM_HEARTBEAT_SECONDS', .01), patch.object(agent_host.Runtime, '_materials', slow):
            response = self.raw_stream(session, user, 'heartbeat')
        self.assertEqual(response['status'], 200)
        packets = response['body'].decode().split('\n\n')
        self.assertTrue(any(p.startswith(': keep-alive') for p in packets))
        events = [json.loads(p.split('data: ',1)[1]) for p in packets if 'data: ' in p]
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn(events[-1]['payload']['outcome'], ('completed','waiting_user'))
        self.assertEqual([e['sequence'] for e in events], list(range(1,len(events)+1)))
        self.assertEqual(self.count('agent_stream_events'), len(events))
        self.assertFalse(self.host.active)

    def test_deadline_is_distinct_from_user_cancel_and_keeps_saved_input(self):
        session, user = self.ready()
        async def wait(runtime, turn):
            await asyncio.Event().wait()
        with patch.object(agent_host, 'RUN_TIMEOUT_SECONDS', .03), patch.object(agent_host, 'STREAM_HEARTBEAT_SECONDS', .01), patch.object(agent_host.Runtime, '_materials', wait):
            response = self.raw_stream(session, user, 'deadline')
        self.assertEqual(response['status'], 200)
        saved = self.host.snapshot(session['sessionId'], 'deadline')
        self.assertEqual((saved['status'], saved['outcome']), ('interrupted','run_timeout'))
        self.assertEqual(saved['events'][-1]['payload']['outcome'], 'run_timeout')
        self.assertEqual(self.conversation(session)['messages'][0]['content'], user['content'])
        self.assertFalse(self.host.active)

    def test_explicit_stop_retains_cancelled_reason_and_closes_runtime(self):
        session, user = self.ready()
        started = asyncio.Event()
        async def wait(runtime, turn):
            started.set()
            await asyncio.Event().wait()
        self.host.start(session['sessionId'], {'requestId':'stop','userMessageId':user['messageId'],
                        'expectedStateRevision':self.version(session['sessionId'])})
        runtime = self.host.active[(session['sessionId'],'stop')]
        async def stop():
            runtime.task = asyncio.create_task(agent_host.run_with_deadline(runtime))
            await started.wait()
            await self.host.cancel(runtime.context.run_id)
            await runtime.task
        with patch.object(agent_host.Runtime, '_materials', wait):
            self.runner.run(stop())
        self.assertEqual(self.host.snapshot(session['sessionId'],'stop')['outcome'], 'cancelled')
        self.assertFalse(self.host.active)

    def test_phase_logs_never_include_exception_details(self):
        runtime = SimpleNamespace(context=SimpleNamespace(run_id='run-synthetic'))
        with self.assertLogs('uvicorn.error', level='INFO') as captured:
            with self.assertRaises(ValueError), agent_host.timed_phase(runtime, 'model_json'):
                raise ValueError('synthetic-private-text-never-log')
        text = '\n'.join(captured.output)
        self.assertIn('phase=model_json elapsed_ms=', text)
        self.assertNotIn('synthetic-private', text)
