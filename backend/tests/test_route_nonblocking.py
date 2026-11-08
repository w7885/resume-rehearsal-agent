"""Slow read-only routing must not stall heartbeats or outlive cancellation."""
import asyncio
from contextvars import copy_context
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.auth import current_user
from app.agent.ports import RepoError
from app.repository import RepositoryError, RequestContext
import test_agent_integration as integration


class RouteReadTests(unittest.TestCase):
    for _name in ('setUp','request','version','material','chat','send','count','ready'):
        locals()[_name] = getattr(integration.AgentIntegrationTests, _name)

    def prepare(self):
        session, user = self.ready()
        self.host.start(session['sessionId'], {'requestId':'route-read', 'userMessageId':user['messageId'],
                        'expectedStateRevision':self.version(session['sessionId'])})
        runtime = self.host.active[(session['sessionId'],'route-read')]
        runtime.llm = object()
        turn = self.service.get_chat_turn_context(runtime.context, runtime.user_id)
        return runtime, turn

    def test_slow_context_read_does_not_block_other_event_loop_work(self):
        runtime, turn = self.prepare()
        original = runtime._route_input
        started = threading.Event()
        def slow(value):
            started.set()
            time.sleep(.1)
            return original(value)
        async def check():
            task = asyncio.create_task(runtime._route(turn))
            while not started.is_set():
                await asyncio.sleep(.001)
            await asyncio.sleep(.005)
            self.assertFalse(task.done(), 'The event loop could not run during the read')
            await task
        result = SimpleNamespace(output=None,fallback_reason='synthetic',attempts=0)
        with patch.object(runtime,'_route_input',slow), patch('app.agent_router.classify',new=AsyncMock(return_value=result)):
            self.runner.run(check())

    def test_worker_preserves_account_scope_and_does_not_call_model_for_foreign_owner(self):
        runtime, turn = self.prepare()
        marker = current_user.set('synthetic-other-owner')
        try:
            model = AsyncMock()
            with patch('app.agent_router.classify',new=model):
                with self.assertRaises(RepoError):
                    self.runner.run(runtime._route(turn), context=copy_context())
            model.assert_not_awaited()
        finally:
            current_user.reset(marker)

    def test_cancelled_read_never_starts_model_when_worker_finishes_late(self):
        runtime, turn = self.prepare()
        started, release = threading.Event(), threading.Event()
        original = runtime._route_input
        def slow(value):
            started.set()
            self.assertTrue(release.wait(3))
            return original(value)
        async def check():
            task = asyncio.create_task(runtime._route(turn))
            while not started.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            await asyncio.to_thread(lambda: None)
        model = AsyncMock()
        with patch.object(runtime,'_route_input',slow), patch('app.agent_router.classify',new=model):
            try:
                self.runner.run(check())
            finally:
                release.set()
        model.assert_not_awaited()

    def test_concurrent_edit_is_rechecked_before_model_budget_is_used(self):
        runtime, turn = self.prepare()
        original = runtime._route_input
        def changed(value):
            prepared = original(value)
            self.service.repository.save_message(RequestContext(runtime.sid), 'synthetic newer input')
            return prepared
        model = AsyncMock()
        with patch.object(runtime,'_route_input',changed), patch('app.agent_router.classify',new=model):
            with self.assertRaises(RepositoryError):
                self.runner.run(runtime._route(turn))
        model.assert_not_awaited()
