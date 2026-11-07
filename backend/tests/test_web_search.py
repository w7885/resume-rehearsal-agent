"""Offline real-SQLite integration of permissions, limits and named cost records."""
import asyncio
import json
import time
import unittest
from unittest.mock import patch

import httpx

from app.agent_budget import ProjectBudget
from app.db import database, transaction
from app.repository import RepositoryError, RequestContext
from app.web_provider import PublicTarget, VolcWebClient, WebProviderError, WebSettings
from app.web_search import WebSearchService
import test_web_storage
from app import db
from test_web_provider import fetch_response, public_resolver, search_response


class WebSearchTests(unittest.TestCase):
    setUp = test_web_storage.WebStorageTests.setUp
    settings = test_web_storage.WebStorageTests.settings
    run_context = test_web_storage.WebStorageTests.run_context

    def prepare(self, handler=None, settings=None, resolver=public_resolver):
        self.settings(True,1)
        self.context=self.run_context()
        self.target=PublicTarget('构造公司','工程实习','广州','实习')
        self.sent=[]
        def default(request):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200,json=fetch_response() if request.url.path.endswith('/fetch') else search_response())
        settings=settings or WebSettings('unit-key',True,True)
        client=VolcWebClient(settings,transport=httpx.MockTransport(handler or default),resolver=resolver)
        self.web=WebSearchService(self.repo,settings,client=client)

    def run_async(self, coroutine):return asyncio.run(coroutine)

    async def search(self,key='q1',**kwargs):
        return await self.web.search(self.context,self.target,request_id=key,web_settings_revision=2,**kwargs)

    async def fetch(self,key='f1',url='https://example.com/jobs/1'):
        return await self.web.fetch(self.context,self.target,url,request_id=key,web_settings_revision=2)

    def budget(self):return ProjectBudget(self.path,'status').status()

    def test_network_diagnostic_omits_exception_text(self):
        def fail(request):
            raise httpx.ConnectError('secret-token https://private.example', request=request)
        self.prepare(handler=fail)
        with self.assertLogs('app.web_search', level='WARNING') as logs:
            result = self.run_async(self.search())
        self.assertEqual(result['code'], 'provider_network_error')
        self.assertIn('ConnectError', logs.output[0])
        self.assertNotIn('secret-token', str(logs.output))
        self.assertNotIn('private.example', str(logs.output))
        self.assertEqual(self.budget()['reservedCents'], 0)
        self.assertEqual(self.budget()['spentCents'], 2)

    def test_search_fetch_unknown_costs_and_external_data_separation(self):
        self.prepare()
        before=self.repo.get_session_context(self.context)
        async def scenario():
            search=await self.search()
            self.assertEqual(search['references'][0]['contentStatus'],'partial')
            fetched=await self.fetch()
            self.assertEqual(fetched['references'][0]['contentStatus'],'sufficient')
            self.assertEqual(fetched['references'][0]['matchStatus'],'unconfirmed')
            self.assertTrue(fetched['needsJD'])
        self.run_async(scenario())
        self.assertEqual(self.budget()['spentCents'],3)
        self.assertEqual(self.budget()['reservedCents'],0)
        self.assertEqual(self.repo.get_session_context(self.context),before)
        with database(self.path,read_only=True) as c:
            rows=c.execute('SELECT status,actual_cents FROM model_budget_reservations ORDER BY rowid').fetchall()
            self.assertEqual([tuple(x) for x in rows],[('unknown',2),('unknown',1)])

    def test_missing_key_server_disabled_and_unconfirmed_budget_do_not_dispatch(self):
        self.prepare()
        for settings in (WebSettings('',True,True),WebSettings('unit',False,True),WebSettings('unit',True,False)):
            self.web=WebSearchService(self.repo,settings,client=self.web.client)
            result=self.run_async(self.search())
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(self.sent,[])
        self.assertEqual(self.budget()['spentCents'],0)

    def test_off_and_unknown_fetch_url_rejected_before_network_or_charge(self):
        self.prepare()
        with self.assertRaises(WebProviderError):self.run_async(self.fetch(url='https://example.com/undiscovered'))
        self.settings(False,2)
        with self.assertRaises(RepositoryError) as error:self.run_async(self.search())
        self.assertEqual(error.exception.code,'web_disabled')
        self.assertEqual(self.sent,[])
        self.assertEqual(self.budget()['spentCents'],0)

    def test_duplicate_requests_and_limits_persist_across_service_instances(self):
        self.prepare()
        first=self.run_async(self.search())
        replay=self.run_async(self.search())
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['references'],first['references'])
        self.run_async(self.search('q2',short_query=True))
        self.web=WebSearchService(self.repo,self.web.settings,client=self.web.client)
        with self.assertRaises(RepositoryError) as error:self.run_async(self.search('q3'))
        self.assertEqual(error.exception.code,'web_limit')
        for key in ['f1','f2','f3']:self.run_async(self.fetch(key))
        with self.assertRaises(RepositoryError) as error:self.run_async(self.fetch('f4'))
        self.assertEqual(error.exception.code,'web_limit')
        self.assertEqual(len(self.sent),5)
        self.assertEqual(self.budget()['spentCents'],7)

    def test_changed_settings_epoch_cannot_reset_turn_limits(self):
        self.prepare()
        self.run_async(self.search())
        self.settings(False,2)
        self.settings(True,3)
        with self.assertRaises(RepositoryError) as error:
            self.run_async(self.web.search(self.context,self.target,request_id='new-epoch',web_settings_revision=4))
        self.assertEqual(error.exception.code,'stale_state')
        self.assertEqual(len(self.sent),1)

    def test_target_change_or_elapsed_deadline_cannot_restart_lookup(self):
        self.prepare()
        self.run_async(self.search())
        with self.assertRaises(RepositoryError) as error:
            self.run_async(self.web.search(self.context,PublicTarget('其他公司','工程实习'),request_id='other',web_settings_revision=2))
        self.assertEqual(error.exception.code,'stale_state')
        with database(self.path) as c,transaction(c):
            c.execute('UPDATE web_requests SET deadline_at=?',(time.time()-1,))
        with self.assertRaises(RepositoryError) as error:self.run_async(self.search('late'))
        self.assertEqual(error.exception.code,'web_timeout')
        self.assertEqual(len(self.sent),1)

    def test_new_internal_run_shares_original_turn_cap(self):
        self.prepare()
        with database(self.path) as c,transaction(c):
            mid=c.execute("SELECT message_id FROM messages WHERE session_id=? AND role='user' LIMIT 1",(self.sid,)).fetchone()[0]
            c.execute('''INSERT INTO agent_run_requests(session_id,request_id,user_message_id,request_hash,root_run_id,current_run_id,status,resume_generation)
                         VALUES (?,'turn',?,'hash','run-test-001','run-test-001','running',1)''',(self.sid,mid))
        self.run_async(self.search())
        with database(self.path) as c,transaction(c):
            c.execute("UPDATE runs SET status='completed' WHERE run_id='run-test-001'")
            c.execute("INSERT INTO runs(run_id,session_id,generation,input_state_revision,resume_generation,status) VALUES ('run-second',?,2,?,1,'running')",(self.sid,self.context.expected_state_revision))
            c.execute("UPDATE agent_run_requests SET current_run_id='run-second' WHERE session_id=?",(self.sid,))
        self.context=RequestContext(self.sid,self.context.expected_state_revision,'run-second',2)
        self.run_async(self.search('q2'))
        with self.assertRaises(RepositoryError) as error:self.run_async(self.search('q3'))
        self.assertEqual(error.exception.code,'web_limit')
        self.assertEqual(len(self.sent),2)

    def test_exhausted_shared_budget_prevents_http(self):
        self.prepare()
        self.budget()
        with database(self.path) as c,transaction(c):c.execute("UPDATE model_budget_accounts SET spent_cents=2999 WHERE budget_id='project'")
        result=self.run_async(self.search())
        self.assertEqual(result['code'],'budget_exhausted')
        self.assertEqual(self.sent,[])
        self.assertEqual(self.budget()['spentCents'],2999)

    def test_invalid_provider_and_quota_failures_keep_unknown_cost_and_do_not_retry(self):
        sent=[]
        def handler(_request):sent.append(True);return httpx.Response(200,json={'ResponseMetadata':{'Error':{'Code':'10406'}},'Result':None})
        self.prepare(handler)
        result=self.run_async(self.search())
        self.assertEqual(result['code'],'provider_quota_exhausted')
        self.assertEqual(len(sent),1)
        self.assertEqual(self.budget()['spentCents'],2)
        self.assertEqual(self.repo.list_external_references(self.user),[])

    def test_permission_closed_during_http_cancels_and_discards_response(self):
        async def scenario():
            started,cancelled=asyncio.Event(),asyncio.Event()
            async def handler(_request):
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            self.prepare(handler)
            task=asyncio.create_task(self.search())
            await started.wait()
            self.settings(False,2)
            with self.assertRaises(RepositoryError):await asyncio.wait_for(task,1)
            self.assertTrue(cancelled.is_set())
        self.run_async(scenario())
        self.assertEqual(self.repo.list_external_references(self.user),[])
        self.assertEqual(self.budget()['spentCents'],2)

    def test_parent_cancel_after_dispatch_and_permission_change_on_return(self):
        async def scenario():
            started=asyncio.Event()
            async def handler(_request):started.set();await asyncio.Event().wait()
            self.prepare(handler)
            task=asyncio.create_task(self.search())
            await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.run_async(scenario())
        self.assertEqual(self.budget()['spentCents'],2)
        self.assertEqual(self.repo.list_external_references(self.user),[])

    def test_close_exactly_on_response_return_does_not_save_content(self):
        def handler(_request):
            self.settings(False,2)
            return httpx.Response(200,json=search_response())
        self.prepare(handler)
        with self.assertRaises(RepositoryError) as error:self.run_async(self.search())
        self.assertEqual(error.exception.code,'web_disabled')
        self.assertEqual(self.repo.list_external_references(self.user),[])
        self.assertEqual(self.budget()['spentCents'],2)

    def test_bad_provider_format_is_charged_unknown_without_saving_content(self):
        self.prepare(lambda _:httpx.Response(200,content=b'bad JSON'))
        result=self.run_async(self.search())
        self.assertEqual(result['code'],'provider_invalid_response')
        self.assertEqual(self.repo.list_external_references(self.user),[])
        self.assertEqual(self.budget()['spentCents'],2)

    def test_individual_request_wall_timeout_cancels_and_settles_unknown(self):
        async def handler(_request):await asyncio.Event().wait()
        self.prepare(handler)
        with patch('app.web_search.REQUEST_TIMEOUT_SECONDS',0.02):
            result=self.run_async(self.search())
        self.assertEqual(result['code'],'provider_timeout')
        self.assertEqual(self.budget()['spentCents'],2)
        self.assertEqual(self.budget()['reservedCents'],0)

    def test_close_during_dns_before_dispatch_releases_fetch_reservation(self):
        async def scenario():
            self.prepare()
            await self.search()
            started=asyncio.Event()
            async def slow_dns(_host):started.set();await asyncio.Event().wait()
            self.web.client.resolver=slow_dns
            task=asyncio.create_task(self.fetch())
            await started.wait();self.settings(False,2)
            with self.assertRaises(RepositoryError):await asyncio.wait_for(task,1)
        self.run_async(scenario())
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.budget()['spentCents'],2)
        self.assertEqual(self.budget()['reservedCents'],0)

    def test_total_timeout_cancels_io_without_waiting_for_forty_five_seconds(self):
        async def scenario():
            self.prepare()
            cancelled=asyncio.Event()
            async def operation():
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            with self.assertRaises(WebProviderError) as error:
                await self.web._guarded_io(self.context,2,time.time()+0.02,operation)
            self.assertEqual(error.exception.code,'web_timeout')
            self.assertTrue(cancelled.is_set())
        self.run_async(scenario())

    def test_out_of_order_web_calls_do_not_consume_model_fifo(self):
        async def scenario():
            first_ready,release=asyncio.Event(),asyncio.Event()
            async def handler(request):
                query=json.loads(request.content)['Query']
                if '广州' in query:
                    first_ready.set();await release.wait()
                return httpx.Response(200,json=search_response())
            self.prepare(handler)
            model=ProjectBudget(self.path,'model-run')
            self.assertTrue(await model.reserve(10))
            task=asyncio.create_task(self.search('first'))
            await first_ready.wait()
            await self.search('second',short_query=True)
            await model.settle('model-run',3,10)
            release.set();await task
        self.run_async(scenario())
        self.assertEqual(self.budget()['spentCents'],7)
        self.assertEqual(self.budget()['reservedCents'],0)
        with database(self.path,read_only=True) as c:
            self.assertEqual(c.execute("SELECT actual_cents FROM model_budget_reservations WHERE run_id='model-run'").fetchone()[0],3)

    def test_desktop_mobile_status_conflict_is_preserved(self):
        url='https://www.shixiseng.com/intern/inn_synthetic'
        mobile='https://wap.shixiseng.com/intern/inn_synthetic'
        def handler(request):
            if request.url.path.endswith('/global_search'):
                data=search_response('当前职位已下线',url)
                data['Result']['Documents'].append({'Title':'构造岗位','Url':mobile,'Snippet':[{'Type':'text','Text':'岗位职责和要求'}]})
                return httpx.Response(200,json=data)
            return httpx.Response(200,json=fetch_response(url=mobile))
        self.prepare(handler)
        self.run_async(self.search())
        result=self.run_async(self.fetch(url=mobile))
        self.assertEqual(result['references'][0]['hiringStatus'],'conflict')
        self.assertTrue(result['needsJD'])

    def test_one_fetch_failure_does_not_block_another_discovered_page(self):
        def handler(request):
            payload=json.loads(request.content)
            if request.url.path.endswith('/global_search'):
                data=search_response()
                data['Result']['Documents'].append({'Title':'另一来源','Url':'https://example.com/jobs/2','Snippet':[{'Type':'text','Text':'岗位说明'}]})
                return httpx.Response(200,json=data)
            code=403 if payload['Url'].endswith('/1') else 200
            return httpx.Response(200,json=fetch_response(url=payload['Url'],code=code))
        self.prepare(handler)
        async def scenario():
            await self.search()
            first=await self.fetch('f1')
            second=await self.fetch('f2','https://example.com/jobs/2')
            self.assertEqual(first['references'][0]['contentStatus'],'unavailable')
            self.assertEqual(second['references'][0]['contentStatus'],'sufficient')
        self.run_async(scenario())
        self.assertEqual(self.budget()['spentCents'],4)

    def test_restart_recovers_pending_web_cost_and_never_replays_http(self):
        self.prepare()
        request=self.repo.begin_web_request(self.context,request_id='pending',web_settings_revision=2,
            target_key=self.target.key,operation='search',payload={'query':self.target.query()},budget_reservation_id='pending-cost')
        budget=ProjectBudget(self.path,request['scopeRunId'])
        self.assertTrue(budget.reserve_request('pending-cost',2))
        from app.agent_host import AgentHost
        host=AgentHost(self.service)
        host.recover()
        host.recover()
        self.assertEqual(budget.status()['spentCents'],2)
        self.assertEqual(budget.status()['reservedCents'],0)
        with database(self.path,read_only=True) as c:
            self.assertEqual(c.execute("SELECT status FROM web_requests WHERE request_id='pending'").fetchone()[0],'unknown')
        self.assertEqual(self.sent,[])

    def test_empty_provider_results_do_not_trigger_automatic_second_query(self):
        sent=[]
        def handler(_request):
            sent.append(True)
            return httpx.Response(200,json={'ResponseMetadata':{},'Result':{'ErrorCode':0,'TotalDocCount':0,'Documents':None}})
        self.prepare(handler)
        result=self.run_async(self.search())
        self.assertEqual(result['status'],'empty')
        self.assertEqual(len(sent),1)


class NamedBudgetTests(unittest.TestCase):
    setUp=test_web_storage.WebStorageTests.setUp

    def test_named_settlement_is_exact_idempotent_and_survives_new_object(self):
        budget=ProjectBudget(self.path,'root')
        self.assertTrue(budget.reserve_request('one',2))
        self.assertTrue(budget.reserve_request('two',1))
        self.assertFalse(budget.reserve_request('one',2))
        self.assertEqual(budget.settle_request('two',0)['chargedCents'],0)
        self.assertEqual(budget.settle_request('one')['chargedCents'],2)
        restored=ProjectBudget(self.path,'root')
        self.assertEqual(restored.settle_request('one',0)['chargedCents'],2)
        self.assertEqual(restored.status()['spentCents'],2)
        with self.assertRaises(RepositoryError):ProjectBudget(self.path,'other').settle_request('one',0)

    def test_v13_web_records_upgrade_without_changing_reference_content_or_cost(self):
        with db.database(':memory:') as c:
            with patch.object(db,'MIGRATIONS',db.MIGRATIONS[:13]),patch.object(db,'SCHEMA_VERSION',13):
                db.initialize_database(c)
                sid=test_web_storage.seed_fixture(c)
            c.execute('''INSERT INTO web_requests(session_id,request_id,run_id,resume_generation,web_settings_revision,target_key,operation,provider,request_hash,input_json,created_at)
                         VALUES (?,'old','run-test-001',1,2,'target','search','volcengine','hash','{}','2026-10-28T00:00:00Z')''',(sid,))
            c.execute('''INSERT INTO external_references(reference_id,session_id,request_id,title,url,content,retrieved_at,match_status,hiring_status,content_status)
                         VALUES ('external-old',?,'old','old','https://example.com','original','2026-10-28','unconfirmed','unconfirmed','partial')''',(sid,))
            c.execute("INSERT INTO model_budget_accounts(budget_id,limit_cents,spent_cents) VALUES ('project',3000,123)")
            old_reference=tuple(c.execute('SELECT * FROM external_references').fetchone())
            db.initialize_database(c)
            self.assertEqual(tuple(c.execute('SELECT * FROM external_references').fetchone()),old_reference)
            self.assertEqual(c.execute('SELECT scope_run_id FROM web_requests').fetchone()[0],'run-test-001')
            self.assertEqual(c.execute('SELECT spent_cents FROM model_budget_accounts').fetchone()[0],123)
