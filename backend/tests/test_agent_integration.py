"""A+B integration in isolated databases, with no provider/network calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from app import db
from app.agent.llm import DeepSeekClient, LLMError, LLMSettings
from app.agent_budget import ProjectBudget
from app.repository import RepositoryError, RequestContext
import test_assistant_replies


class FakeClient:
    def __init__(self,runtime,answer='accept'):
        self.runtime,self.answer,self.steps=runtime,answer,0

    async def complete_json(self,*,system,user,run_id='test'):
        value=json.loads(user)
        if system.startswith('识别附件正文'):
            # Existing ambiguous fixtures exercise the clarification path.
            data={'materials':[{'materialId':m['materialId'],'role':'resume' if '简历' in value['userContent'] and '项目经历' in m['text'] else 'unknown','quote':m['text'],'use':'current','authorizationQuote':''} for m in value['materials']]}
        elif system.startswith('判断用户对附件'):
            data={'status':'resolved','materials':[{'materialId':m['materialId'],'role':'resume'} for m in value['materials']]}
        elif system.startswith('解析用户对改写'):
            prompt=value['candidatePrompts'][0]
            data={'status':'clarify','decisions':[]} if self.answer=='clarify' else {'status':'resolved','decisions':[{'promptMessageId':prompt['promptMessageId'],'suggestionId':prompt['suggestions'][0]['suggestionId'],'decision':self.answer,'feedback':value['userContent'] if self.answer=='revise' else ''}]}
        else:
            raise AssertionError('Unexpected fake JSON request')
        return SimpleNamespace(data=data,prompt_tokens=0,completion_tokens=0,cost_cents=0)

    async def route(self,**kwargs):
        block=self.runtime.repo.get_draft(self.runtime.context)['blocks'][0]
        return {'action':'answer','target_experience_id':block['blockId'],'scope':'experience','confidence':1}

    async def decide(self,**kwargs):
        self.steps+=1
        if self.steps>1:
            data={'action':'finish','outcome':'completed'}
        else:
            context=self.runtime.repo.get_session_context(self.runtime.context)
            block=context['draft']['blocks'][0]; source=context['sources'][0]
            data={'action':'tool','tool':'propose_change','args':{'operation':'replace','target':block['blockId'],'before':block['markdown'],'after':'协助整理反馈。','reason':'梳理职责表达','source_refs':[{'source_id':source['sourceId'],'source_revision':source['sourceRevision'],'quote':'整理反馈'}],'gaps':[]}}
        return SimpleNamespace(data=data,prompt_tokens=0,completion_tokens=0,cost_cents=0)


class AgentIntegrationTests(unittest.TestCase):
    setUp=test_assistant_replies.AssistantReplyTests.setUp
    request=test_assistant_replies.AssistantReplyTests.request
    version=test_assistant_replies.AssistantReplyTests.version
    material=test_assistant_replies.AssistantReplyTests.material
    chat=test_assistant_replies.AssistantReplyTests.chat
    send=test_assistant_replies.AssistantReplyTests.send
    count=test_assistant_replies.AssistantReplyTests.count

    def ready(self,role='resume'):
        session=self.chat()
        self.material(session,text='项目经历：整理反馈，协助测试。',role=role)
        user=self.send(session,'请帮我梳理职责。')
        self.host=self.app.state.agent_host
        self.host.client_factory=lambda runtime,budget:FakeClient(runtime)
        self.service=self.app.state.business_service
        return session,user

    def analyze(self,session,user,key='run'):
        response=self.request('POST',f"/api/sessions/{session['sessionId']}/runs",data={'requestId':key,'userMessageId':user['messageId'],'expectedStateRevision':self.version(session['sessionId'])})
        self.assertEqual(response['status'],200,response)
        return [json.loads(packet.split('data: ',1)[1]) for packet in response['body'].decode().strip().split('\n\n')]

    def conversation(self,session):
        return self.request('GET',f"/api/sessions/{session['sessionId']}/conversation")['json']['data']

    def answer_goal(self,session,key='goal'):
        prompt=self.conversation(session)['messages'][-1]
        self.assertIn('岗位',prompt['content'])
        answer=self.send(session,'前端开发实习',request_id=key,replyToMessageId=prompt['messageId'])
        return self.analyze(session,answer,key)

    def test_suggestion_accepts_whitespace_only_before_difference(self):
        class Trimmed(FakeClient):
            async def decide(self,**kwargs):
                result=await super().decide(**kwargs)
                data=result.data or {}
                args=data.get('args') or {}
                if data.get('tool')=='propose_change':
                    args['before']=args['before'].rstrip()
                return result
        session=self.chat()
        self.material(session,text='项目经历：整理反馈，协助测试。\n',role='resume')
        user=self.send(session,'请帮我梳理职责。')
        self.host=self.app.state.agent_host
        self.host.client_factory=lambda runtime,budget:Trimmed(runtime)
        events=self.analyze(session,user,'trim')
        self.assertEqual(events[-1]['payload']['outcome'],'waiting_user',events)
        self.answer_goal(session)
        self.assertEqual(self.conversation(session)['messages'][-1]['assistant']['kind'],'change_question')

    def test_duplicate_source_refs_are_merged(self):
        class Duplicate(FakeClient):
            async def decide(self,**kwargs):
                result=await super().decide(**kwargs)
                data=result.data or {}
                args=data.get('args') or {}
                if data.get('tool')=='propose_change':
                    ref=args['source_refs'][0]
                    args['source_refs']=[ref,{**ref,'quote':'协助测试'}]
                return result
        session=self.chat()
        self.material(session,text='项目经历：整理反馈，协助测试。',role='resume')
        user=self.send(session,'请帮我梳理职责。')
        self.host=self.app.state.agent_host
        self.host.client_factory=lambda runtime,budget:Duplicate(runtime)
        self.analyze(session,user,'dup')
        self.answer_goal(session,'dup-goal')
        self.assertEqual(self.count('suggestions'),1)
        self.assertEqual(self.conversation(session)['messages'][-1]['assistant']['kind'],'change_question')

    def test_disabled_agent_keeps_saved_chat_and_exposes_registered_routes(self):
        session=self.chat(); user=self.send(session,'构造消息')
        with patch.dict('os.environ',{'AGENT_ENABLED':'0','AGENT_BUDGET_CONFIRMED':'0'}):
            result=self.request('POST',f"/api/sessions/{session['sessionId']}/runs",data={'requestId':'a','userMessageId':user['messageId'],'expectedStateRevision':self.version(session['sessionId'])})
        self.assertEqual(result['status'],503)
        self.assertEqual(self.count('messages'),1)
        self.assertEqual(self.count('runs'),0)
        self.assertIn('/api/sessions/{session_id}/runs',self.app.openapi()['paths'])

    def test_saved_unread_image_reports_disabled_vision_without_claiming_it_was_read(self):
        from types import SimpleNamespace
        from app.agent_host import Runtime
        session = self.chat()
        repo = self.app.state.business_service.repository
        repo.save_material(RequestContext(session['sessionId'], self.version(session['sessionId'])),
                           name='unread.png', media_type='image/png', original_data=b'fixture-image')
        runtime = SimpleNamespace(repo=repo, sid=session['sessionId'], context=RequestContext(session['sessionId']))
        with patch.dict('os.environ', {'VISION_ENABLED': '0'}):
            text = Runtime._missing_draft_message(runtime)
        self.assertIn('图片识别当前未启用', text)
        self.assertIn('图片原件已保存', text)
        self.assertNotIn('会自动识别', text)
        self.assertEqual(self.count('source_blocks'), 0)
        with patch.dict('os.environ', {'VISION_ENABLED': '1', 'DEEPSEEK_API_KEY': 'synthetic-no-call', 'AGENT_BUDGET_CONFIRMED': '1'}):
            text = Runtime._missing_draft_message(runtime)
        self.assertIn('未能从图片提取出可用文字', text)
        self.assertNotIn('当前未启用', text)

    def test_agent_generates_saved_question_then_accepts_exact_reply_once(self):
        session,user=self.ready()
        events=self.analyze(session,user)
        self.assertEqual(events[-1]['payload']['outcome'],'waiting_user',events)
        self.answer_goal(session)
        value=self.conversation(session)
        prompt=value['messages'][-1]
        self.assertEqual(prompt['assistant']['kind'],'change_question',value)
        self.assertEqual(prompt['assistant']['mode'],'synthetic')
        self.assertIn('整理反馈',value['draft']['markdown'])
        answer=self.send(session,'采用这条。',request_id='answer',replyToMessageId=prompt['messageId'])
        result=self.analyze(session,answer,'accept')
        self.assertEqual(result[-1]['payload']['outcome'],'completed',result)
        self.assertEqual(self.conversation(session)['draft']['markdown'],'协助整理反馈。')
        self.assertEqual(self.count('suggestion_decisions'),1)
        self.assertEqual(self.count('chat_decision_resolutions'),1)
        before=self.count('messages')
        # Different run request IDs for the same saved user turn still replay.
        self.analyze(session,answer,'repeat')
        self.assertEqual(self.count('messages'),before)
        self.assertEqual(self.count('suggestion_decisions'),1)

    def test_pending_upload_is_confirmed_in_chat_before_initial_draft(self):
        session,user=self.ready('unknown')
        self.analyze(session,user)
        value=self.conversation(session); prompt=value['messages'][-1]
        self.assertEqual(value['draft']['markdown'],'')
        self.assertIn('岗位描述',prompt['content'])
        answer=self.send(session,'这是要修改的个人简历。',request_id='answer',replyToMessageId=prompt['messageId'])
        events=self.analyze(session,answer,'confirm-material')
        self.assertEqual(events[-1]['payload']['outcome'],'waiting_user',events)
        self.answer_goal(session,'confirm-goal')
        value=self.conversation(session)
        self.assertEqual(value['materials'][0]['role'],'resume')
        self.assertTrue(value['draft']['markdown'])
        self.assertEqual(value['messages'][-1]['assistant']['kind'],'change_question',value)
        self.assertEqual(self.count('source_blocks'),2)
        self.assertEqual(self.count('suggestion_decisions'),0)

    def test_material_prompt_is_not_authority_after_another_upload(self):
        session,user=self.ready('unknown');self.analyze(session,user)
        prompt=self.conversation(session)['messages'][-1]
        self.material(session,text='新的附件',role='unknown',request_id='later')
        answer=self.send(session,'是简历。',request_id='answer',replyToMessageId=prompt['messageId'])
        self.analyze(session,answer,'later-run')
        self.assertEqual(self.conversation(session)['draft']['markdown'],'')

    def test_cancelled_late_client_cannot_write_reply_or_new_suggestion(self):
        session,user=self.ready()
        revision=self.version(session['sessionId'])
        async def check():
            row,_=self.host.start(session['sessionId'],{'requestId':'cancel','userMessageId':user['messageId'],'expectedStateRevision':revision})
            runtime=self.host.active[(session['sessionId'],'cancel')]
            await self.host.cancel(row['root_run_id'])
            with self.assertRaises(RepositoryError):runtime._reply('迟到回复')
            await runtime.run()
            self.assertEqual(self.host.snapshot(session['sessionId'],'cancel')['status'],'interrupted')
        self.runner.run(check())
        self.assertEqual(self.count('assistant_messages'),0)

    def test_restart_interrupts_pending_work_and_never_reexecutes_same_turn(self):
        session,user=self.ready()
        body={'requestId':'restart','userMessageId':user['messageId'],'expectedStateRevision':self.version(session['sessionId'])}
        self.host.start(session['sessionId'],body)
        self.host.recover()
        row,created=self.host.start(session['sessionId'],body)
        self.assertFalse(created);self.assertEqual(row['status'],'interrupted')
        self.assertEqual(self.count('runs'),1)

    def test_conflicting_request_or_spoofed_input_never_reaches_client(self):
        session,user=self.ready()
        self.analyze(session,user)
        body={'requestId':'run','userMessageId':'missing','expectedStateRevision':self.version(session['sessionId'])}
        result=self.request('POST',f"/api/sessions/{session['sessionId']}/runs",data=body)
        self.assertEqual(result['status'],409)
        result=self.request('POST',f"/api/sessions/{session['sessionId']}/runs",data={**body,'message':'伪造同意','requestId':'spoof'})
        self.assertEqual(result['status'],422)

    def test_clarification_does_not_execute_and_new_answer_can_choose(self):
        session,user=self.ready();self.analyze(session,user)
        prompt=self.conversation(session)['messages'][-1]
        answer=self.send(session,'可以。',request_id='ambiguous',replyToMessageId=prompt['messageId'])
        self.host.client_factory=lambda runtime,budget:FakeClient(runtime,'clarify')
        self.analyze(session,answer,'clarify')
        self.assertEqual(self.count('suggestion_decisions'),0)
        clarified=self.conversation(session)['messages'][-1]
        self.assertEqual(clarified['assistant']['kind'],'change_question')
        self.host.client_factory=lambda runtime,budget:FakeClient(runtime)
        answer=self.send(session,'明确采用这一条。',request_id='clear',replyToMessageId=clarified['messageId'])
        self.analyze(session,answer,'clear')
        self.assertEqual(self.count('suggestion_decisions'),1)

    def test_new_user_turn_supersedes_abandoned_running_request(self):
        session,user=self.ready()
        first,_=self.host.start(session['sessionId'],{'requestId':'old','userMessageId':user['messageId'],'expectedStateRevision':self.version(session['sessionId'])})
        answer=self.send(session,'先不要改，我补充一点。',request_id='new-input')
        second,created=self.host.start(session['sessionId'],{'requestId':'new','userMessageId':answer['messageId'],'expectedStateRevision':self.version(session['sessionId'])})
        self.assertTrue(created)
        self.assertNotEqual(first['root_run_id'],second['root_run_id'])
        self.assertEqual(self.host.snapshot(session['sessionId'],'old')['status'],'interrupted')

    def test_memory_is_invalidated_after_manual_edit_without_erasing_history(self):
        from app.agent_state import GuardedStateStore
        session,user=self.ready()
        self.host.start(session['sessionId'],{'requestId':'one','userMessageId':user['messageId'],'expectedStateRevision':self.version(session['sessionId'])})
        first=self.host.active[(session['sessionId'],'one')]
        store=GuardedStateStore(first);store.put(session['sessionId'],'session','memory',{'fact':'old'},None)
        self.service.update_draft(session['sessionId'],{'requestId':'edit','expectedStateRevision':self.version(session['sessionId']),'markdown':'手改后的新内容'})
        with self.assertRaises(RepositoryError):store.get(session['sessionId'],'session','memory')
        user=self.send(session,'继续核对新内容。',request_id='next')
        self.host.start(session['sessionId'],{'requestId':'two','userMessageId':user['messageId'],'expectedStateRevision':self.version(session['sessionId'])})
        fresh=GuardedStateStore(self.host.active[(session['sessionId'],'two')])
        self.assertIsNone(fresh.get(session['sessionId'],'session','memory'))
        with db.database(self.path,read_only=True) as c:self.assertEqual(c.execute('SELECT count(*) FROM agent_state').fetchone()[0],1)

    def test_material_activation_failure_rolls_back_role_source_and_draft(self):
        session,user=self.ready('unknown');self.analyze(session,user)
        prompt=self.conversation(session)['messages'][-1]
        user=self.send(session,'确认这是简历。',request_id='confirm',replyToMessageId=prompt['messageId'])
        self.host.start(session['sessionId'],{'requestId':'fail','userMessageId':user['messageId'],'expectedStateRevision':self.version(session['sessionId'])})
        runtime=self.host.active[(session['sessionId'],'fail')]
        with runtime.repo._operation(runtime.context) as (c,_):
            materials=json.loads(c.execute('SELECT materials_json FROM agent_material_prompts WHERE prompt_message_id=?',(prompt['messageId'],)).fetchone()[0])
        with patch.object(type(runtime.repo),'create_initial_draft',side_effect=OSError('construct write failure')):
            with self.assertRaises(OSError):runtime._activate([{'materialId':materials[0]['materialId'],'role':'resume'}],materials)
        value=self.conversation(session)
        self.assertEqual(value['materials'][0]['role'],'unknown')
        self.assertEqual(value['draft']['markdown'],'')
        self.assertEqual(self.count('source_blocks'),1)

    def test_no_uncommitted_event_is_queued_when_transaction_fails(self):
        session,user=self.ready()
        self.host.start(session['sessionId'],{'requestId':'event','userMessageId':user['messageId'],'expectedStateRevision':self.version(session['sessionId'])})
        runtime=self.host.active[(session['sessionId'],'event')]
        with self.assertRaises(OSError):
            with runtime.repo._operation(runtime.context,write=True) as (connection,_):
                runtime._emit('progress',{'message':'未提交'},connection)
                raise OSError('construct transaction failure')
        self.assertTrue(runtime.queue.empty())
        self.assertEqual(self.count('agent_stream_events'),0)

    def test_http_disconnect_closes_runtime_and_preserves_saved_input(self):
        session,user=self.ready()
        revision=self.version(session['sessionId'])
        body=json.dumps({'requestId':'stream','userMessageId':user['messageId'],'expectedStateRevision':revision}).encode()
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1','scheme':'http','method':'POST',
               'path':f"/api/sessions/{session['sessionId']}/runs",'query_string':b'',
               'headers':[(b'host',b'127.0.0.1:3001'),(b'content-type',b'application/json'),(b'content-length',str(len(body)).encode())]}
        async def disconnect():
            delivered=False
            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered=True
                    return {'type':'http.request','body':body,'more_body':False}
                await asyncio.Event().wait()
            async def send(message):
                if message['type']=='http.response.body' and message.get('body'):
                    raise OSError('construct disconnected HTTP transport')
            with self.assertRaises(Exception):await self.app(scope,receive,send)
        self.runner.run(disconnect())
        self.assertFalse(self.host.active)
        self.assertEqual(self.host.snapshot(session['sessionId'],'stream')['status'],'interrupted')
        self.assertEqual(self.conversation(session)['messages'][0]['content'],user['content'])


class AgentDatabaseTests(unittest.TestCase):
    def test_known_b4_upgrade_preserves_business_and_budget_rows(self):
        with db.database(':memory:') as connection:
            for version,migration in db.MIGRATIONS[:3]:
                db._execute_script(connection,migration.read_text(encoding='utf-8'))
                connection.execute('INSERT INTO schema_version(version) VALUES (?)',(version,))
            db._execute_script(connection,(Path(db.__file__).parent/'migrations/004_agent_state.sql').read_text(encoding='utf-8'))
            connection.execute('INSERT INTO schema_version(version) VALUES (4)')
            connection.execute("INSERT INTO sessions(session_id,direction) VALUES ('b','构造')")
            connection.execute("INSERT INTO messages(message_id,session_id,role,content) VALUES ('m','b','user','原话')")
            connection.execute("INSERT INTO agent_state(session_id,scope,key,doc_json) VALUES ('b','budget','day','{\"spent_cents\":1234}')")
            before=tuple(connection.execute('SELECT * FROM agent_state').fetchone())
            result=db.initialize_database(connection)
            self.assertEqual(result['schemaVersion'],12)
            self.assertEqual(tuple(connection.execute('SELECT * FROM agent_state').fetchone()),before)
            self.assertEqual(connection.execute('SELECT content FROM messages').fetchone()[0],'原话')
            self.assertEqual(connection.execute('SELECT mode FROM sessions').fetchone()[0],'local')
            db.initialize_database(connection)
            self.assertEqual(connection.execute('SELECT count(*) FROM agent_state').fetchone()[0],1)

    def test_unknown_b4_is_rejected_without_erasing_rows(self):
        with db.database(':memory:') as connection:
            with patch.object(db,'MIGRATIONS',db.MIGRATIONS[:3]),patch.object(db,'SCHEMA_VERSION',3):db.initialize_database(connection)
            connection.execute('INSERT INTO schema_version(version) VALUES (4)')
            connection.execute("INSERT INTO sessions(session_id,direction) VALUES ('b','保留')")
            with self.assertRaises(db.SchemaError):db.initialize_database(connection)
            self.assertEqual(connection.execute('SELECT max(version) FROM schema_version').fetchone()[0],4)
            self.assertEqual(connection.execute('SELECT direction FROM sessions').fetchone()[0],'保留')


class BudgetIntegrationTests(unittest.TestCase):
    setUp=test_assistant_replies.AssistantReplyTests.setUp

    def test_legacy_spend_survives_session_deletion_and_cannot_raise_limit(self):
        with db.database(self.path) as c:
            c.execute("INSERT INTO sessions(session_id,direction) VALUES ('b','构造')")
            c.execute("INSERT INTO agent_state(session_id,scope,key,doc_json) VALUES ('b','budget','old-day','{\"spent_cents\":2300,\"reserved_cents\":100}')")
        budget=ProjectBudget(self.path,'run')
        self.assertEqual(budget.status()['remainingCents'],600)
        with db.database(self.path) as c:c.execute("DELETE FROM sessions WHERE session_id='b'")
        self.assertEqual(budget.status()['spentCents'],2400)
        self.assertFalse(self.runner.run(budget.reserve(601)))
        with self.assertRaises(RepositoryError):ProjectBudget(self.path,'other',3001)

    def test_concurrent_reservations_never_allocate_project_limit_twice(self):
        def reserve(index):
            return asyncio.run(ProjectBudget(self.path,f'run-{index}').reserve(2000))
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,[1,2]))
        self.assertEqual(sum(results),1)
        self.assertEqual(ProjectBudget(self.path,'status').status()['reservedCents'],2000)

    def test_provider_cancellation_charges_unknown_reservation(self):
        budget=ProjectBudget(self.path,'cancelled')
        async def test():
            async def handler(request):raise asyncio.CancelledError()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                llm=DeepSeekClient(LLMSettings(api_key='synthetic-no-provider'),budget=budget,client=client)
                with self.assertRaises(asyncio.CancelledError):await llm.complete_json(system='构造',user='构造')
        self.runner.run(test())
        value=budget.status();self.assertEqual(value['reservedCents'],0);self.assertGreater(value['spentCents'],0)

    def test_unknown_usage_never_releases_reservation_as_free(self):
        budget=ProjectBudget(self.path,'missing-usage')
        async def test():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}]}))) as client:
                llm=DeepSeekClient(LLMSettings(api_key='synthetic-no-provider'),budget=budget,client=client)
                with self.assertRaises(LLMError):await llm.complete_json(system='构造',user='构造')
        self.runner.run(test())
        self.assertGreater(budget.status()['spentCents'],0)
        self.assertEqual(budget.status()['reservedCents'],0)

    def test_known_usage_settles_once_and_restart_charges_pending(self):
        budget=ProjectBudget(self.path,'run')
        self.assertTrue(self.runner.run(budget.reserve(100)))
        self.runner.run(budget.settle('run',40,100))
        self.runner.run(budget.settle('run',40,100))
        self.assertEqual(budget.status()['spentCents'],40)
        self.runner.run(budget.reserve(80))
        self.app.state.agent_host.recover()
        self.assertEqual(budget.status()['spentCents'],120)
        self.assertEqual(budget.status()['reservedCents'],0)
