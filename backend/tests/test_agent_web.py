"""Scripted public-goal/evidence checks with real SQLite and MockTransport."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from pydantic import ValidationError
from app.agent.schemas import SearchTargetContextArgs

import httpx

from app.agent_web import AgentWebBridge
from app.agent_budget import ProjectBudget
from app.db import database, transaction
from app.repository import RepositoryError
from app.web_provider import VolcWebClient, WebSettings
from app.web_search import WebSearchService
import test_web_storage
from test_web_provider import public_resolver, search_response, fetch_response

GOAL='构造公司 广州 工程实习生 实习'
TEXT='构造公司 工程实习生 广州 实习\n岗位职责：负责接口开发和测试。\n岗位要求：熟悉SQL和HTTP。'
TEXT+='\n补充说明：本段只用于构造岗位测试，不是真实招聘信息或用户经历。'*3


def goal_data():
    return {'status':'resolved','company':'构造公司','role':'工程实习','location':'广州','employment_type':'intern','posting_id':''}


def proof_for(ref):
    content=ref['content']
    return {'reference_id':ref['reference_id'],'match':'matched','company_quote':'构造公司',
            'role_quote':'工程实习','location_quote':'广州','type_quote':'实习',
            'posting_id_quote':'','duties_quote':'负责接口开发和测试' if '负责接口开发和测试' in content else '',
            'requirements_quote':'熟悉SQL和HTTP' if '熟悉SQL和HTTP' in content else '',
            'content_quote':'','jd_conflict':False}


class ScriptRuntime:
    def __init__(self,test,handler=None):
        self.repo,self.context,self.sid=test.repo,test.context,test.sid
        self.request_id='script-turn'
        self.budget=ProjectBudget(test.path,self.context.run_id)
        self.calls=[];self.sent=[];self.events=[]
        self.public_goal=goal_data()
        self.proof_transform=None
        def default(request):
            self.sent.append(json.loads(request.content))
            data=fetch_response(text=TEXT) if request.url.path.endswith('/fetch') else search_response(TEXT)
            return httpx.Response(200,json=data)
        settings=WebSettings('synthetic-web-key',True,True)
        client=VolcWebClient(settings,transport=httpx.MockTransport(handler or default),resolver=public_resolver)
        self.host=SimpleNamespace(web_service_factory=lambda runtime:WebSearchService(self.repo,settings,client=client))

    def _emit(self,kind,payload):self.events.append((kind,payload))

    async def _json(self,instruction,value):
        self.calls.append((instruction,value))
        if instruction.startswith('提取当前明确公开求职目标'):
            return dict(self.public_goal)
        if instruction.startswith('核对外部岗位参考'):
            proofs=[proof_for(ref) for ref in value['references']]
            if self.proof_transform:proofs=self.proof_transform(proofs,value)
            return {'assessments':proofs}
        raise AssertionError('unexpected scripted model call')


class AgentWebTests(unittest.TestCase):
    setUp=test_web_storage.WebStorageTests.setUp
    settings=test_web_storage.WebStorageTests.settings
    run_context=test_web_storage.WebStorageTests.run_context

    def prepare(self,handler=None):
        with database(self.path) as c,transaction(c):
            c.execute("UPDATE source_blocks SET use_status='history' WHERE session_id=? AND role='jd'",(self.sid,))
            c.execute("UPDATE sessions SET direction=?,direction_status='provided' WHERE session_id=?",(GOAL,self.sid))
        self.settings(True,1)
        self.context=self.run_context()
        self.runtime=ScriptRuntime(self,handler)
        self.bridge=AgentWebBridge(self.runtime)

    async def lookup(self,purpose='job_requirements'):
        return await self.bridge.lookup(self.bridge.view()['targetId'],purpose)

    def execute(self,coroutine):return asyncio.run(coroutine)

    def test_unknown_hiring_state_with_complete_matching_content_is_usable(self):
        self.prepare()
        before=self.repo.get_session_context(self.context)
        result=self.execute(self.lookup())
        self.assertEqual(result['status'],'found')
        self.assertEqual(result['references'][0]['hiringStatus'],'unconfirmed')
        self.assertIn('未核实',result['references'][0]['hiringNotice'])
        self.assertEqual(len(self.runtime.sent),1)
        self.assertEqual(self.repo.get_session_context(self.context),before)
        self.assertTrue(self.bridge.citation_ids())

    def test_current_cache_reused_across_bridge_instances_without_new_calls(self):
        self.prepare();first=self.execute(self.lookup())
        self.bridge=AgentWebBridge(self.runtime)
        result=self.execute(self.lookup())
        self.assertTrue(result['reused'])
        self.assertEqual(first['references'],result['references'])
        self.assertEqual(len(self.runtime.sent),1)

    def test_existing_complete_user_jd_skips_both_models_and_search(self):
        self.prepare()
        with database(self.path) as c,transaction(c):
            c.execute("UPDATE source_blocks SET use_status='active' WHERE session_id=? AND role='jd'",(self.sid,))
        # The fixture JD is short; append an isolated message-backed complete JD.
        from app.repository import RequestContext
        user=RequestContext(self.sid)
        message=self.repo.save_message(user,'构造公司工程实习JD\n岗位职责：负责接口开发与维护。\n任职要求：熟悉Python、SQL和HTTP，能够完成测试。',kind='message')
        self.repo.save_source(user,text=message['content'],role='jd',origin={'kind':'message','messageId':message['messageId']},use_status='active')
        with database(self.path) as c,transaction(c):
            c.execute("UPDATE source_blocks SET use_status='history' WHERE session_id=? AND role='jd' AND message_id IS NULL",(self.sid,))
        self.context=self.run_context();self.runtime.context=self.context
        result=self.execute(self.lookup())
        self.assertEqual(result['status'],'provided_jd')
        self.assertEqual(self.runtime.calls,[]);self.assertEqual(self.runtime.sent,[])

    def test_wrong_target_id_and_ambiguous_goal_do_not_dispatch(self):
        self.prepare()
        with self.assertRaises(RepositoryError):self.execute(self.bridge.lookup('target-forged','job_requirements'))
        self.runtime.public_goal['status']='ambiguous'
        result=self.execute(self.lookup())
        self.assertTrue(result['needsJD']);self.assertEqual(self.runtime.sent,[])

    def test_unanchored_target_or_contacts_do_not_leave_application(self):
        self.prepare();self.runtime.public_goal['company']='凭空公司'
        result=self.execute(self.lookup());self.assertTrue(result['needsJD']);self.assertEqual(self.runtime.sent,[])
        self.prepare_again_public_contact()
        result=self.execute(self.lookup());self.assertTrue(result['needsJD']);self.assertEqual(self.runtime.sent,[])

    def prepare_again_public_contact(self):
        with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',('13800138000 广州 工程实习生 实习',self.sid))
        self.runtime.public_goal['company']='13800138000'
        self.bridge=AgentWebBridge(self.runtime)

    def test_partial_snippet_fetches_actual_candidate_before_accepting(self):
        sent=[]
        def handler(request):
            sent.append(request.url.path)
            return httpx.Response(200,json=fetch_response(text=TEXT) if request.url.path.endswith('/fetch') else search_response('构造公司 工程实习生 广州 实习'))
        self.prepare(handler);result=self.execute(self.lookup())
        self.assertEqual(result['status'],'found')
        self.assertEqual(sent,['/search_api/global_search','/search_api/fetch'])
        self.assertEqual(result['references'][0]['contentStatus'],'sufficient')

    def test_closed_source_not_used_even_when_model_claims_match(self):
        self.prepare(lambda _:httpx.Response(200,json=search_response(TEXT+'\n当前职位已下线')))
        result=self.execute(self.lookup())
        self.assertTrue(result['needsJD']);self.assertEqual(self.bridge.citation_ids(),[])

    def test_wrong_location_wrong_role_and_fake_quotes_rejected(self):
        self.prepare(lambda _:httpx.Response(200,json=search_response(TEXT.replace('广州','北京'))))
        self.runtime.proof_transform=lambda ps,_:[{**p,'location_quote':'北京'} for p in ps]
        self.assertTrue(self.execute(self.lookup())['needsJD'])
        self.assertEqual(self.bridge.citation_ids(),[])

    def test_forged_assessment_ids_and_duplicate_ids_rejected(self):
        self.prepare()
        self.runtime.proof_transform=lambda ps,_:[{**ps[0],'reference_id':'external-other-session'}]
        self.assertTrue(self.execute(self.lookup())['needsJD'])
        self.assertEqual(self.bridge.citation_ids(),[])
        self.bridge=AgentWebBridge(self.runtime)
        self.runtime.proof_transform=lambda ps,_:[ps[0],ps[0]]
        self.assertTrue(self.execute(self.lookup())['needsJD'])
        self.assertEqual(self.bridge.citation_ids(),[])

    def test_model_jd_conflict_preserves_user_source(self):
        self.prepare()
        from app.repository import RequestContext
        user=RequestContext(self.sid)
        message=self.repo.save_message(user,'岗位补充：仅提供Python要求，地点广州。')
        self.repo.save_source(user,text=message['content'],role='jd',origin={'kind':'message','messageId':message['messageId']},use_status='active')
        self.context=self.run_context();self.runtime.context=self.context
        self.runtime.proof_transform=lambda ps,_:[{**p,'jd_conflict':True} for p in ps]
        before=self.repo.get_session_context(self.context)['sources']
        self.assertTrue(self.execute(self.lookup())['needsJD'])
        self.assertEqual(self.repo.get_session_context(self.context)['sources'],before)
        self.assertEqual(self.bridge.citation_ids(),[])

    def test_target_change_on_response_invalidates_search_before_reference_save(self):
        def handler(_request):
            with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',('别的公司 广州 工程实习',self.sid))
            return httpx.Response(200,json=search_response(TEXT))
        self.prepare(handler)
        with self.assertRaises(RepositoryError):self.execute(self.lookup())
        self.assertEqual(self.repo.list_external_references(self.user),[])

    def test_close_reopen_does_not_restore_old_reference_context(self):
        self.prepare();self.execute(self.lookup())
        self.settings(False,2);self.settings(True,3)
        self.assertEqual(self.bridge.view()['references'],[])

    def test_company_background_uses_fixed_public_query_not_resume(self):
        self.prepare()
        def proofs(ps,value):
            return [{**p,'content_quote':'岗位职责：负责接口开发和测试。'} for p in ps]
        self.runtime.proof_transform=proofs
        result=self.execute(self.lookup('company_background'))
        self.assertEqual(result['status'],'found')
        self.assertEqual(self.runtime.sent[0]['Query'],'构造公司 产品 业务介绍')

    def test_similar_candidates_with_different_requirements_are_not_merged(self):
        def handler(_request):
            data=search_response(TEXT)
            data['Result']['Documents'].append({'Title':'构造公司工程实习','Url':'https://example.com/jobs/2',
                'Snippet':[{'Type':'text','Text':TEXT.replace('SQL','Java')}]})
            return httpx.Response(200,json=data)
        self.prepare(handler)
        def proofs(ps,value):
            return [{**p,'requirements_quote': '熟悉Java和HTTP' if 'Java' in ref['content'] else p['requirements_quote']}
                    for p,ref in zip(ps,value['references'])]
        self.runtime.proof_transform=proofs
        result=self.execute(self.lookup())
        self.assertTrue(result['needsJD']);self.assertEqual(result['code'],'multiple_jobs')

    def test_intern_experience_in_campus_requirements_is_not_intern_recruitment(self):
        text=TEXT.replace('工程实习生','工程岗').replace('广州 实习','广州 校园招聘')+'\n有实习经历优先'
        def handler(_request):
            data=search_response(text);data['Result']['Documents'][0]['Title']='构造公司工程岗 校园招聘'
            return httpx.Response(200,json=data)
        self.prepare(handler)
        with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',('构造公司 广州 工程岗 实习',self.sid))
        self.runtime.public_goal['role']='工程岗'
        self.runtime.proof_transform=lambda ps,_:[{**p,'role_quote':'工程岗'} for p in ps]
        self.assertTrue(self.execute(self.lookup())['needsJD'])

    def test_goal_changes_during_model_assessment_block_acceptance(self):
        self.prepare()
        def proofs(ps,_):
            with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',('新目标公司 广州 工程实习',self.sid))
            return ps
        self.runtime.proof_transform=proofs
        with self.assertRaises(RepositoryError):self.execute(self.lookup())
        with database(self.path,read_only=True) as c:self.assertEqual(c.execute('SELECT count(*) FROM external_assessments').fetchone()[0],0)

    def test_tool_does_not_accept_free_queries_urls_permissions_or_session_ids(self):
        for field in ('query','url','enabled','session_id','api_key'):
            with self.subTest(field=field),self.assertRaises(ValidationError):
                SearchTargetContextArgs.model_validate({'target_id':'target-server','purpose':'job_requirements',field:'not-allowed'})

    def test_unrelated_reply_does_not_become_a_public_target(self):
        self.prepare()
        result=self.execute(self.bridge.consume_target_reply({'userMessage':{'content':'构造公司 广州','messageId':'unrelated'},'candidatePrompts':[]}))
        self.assertFalse(result)
        self.assertEqual(self.runtime.calls,[])

    def test_saved_public_target_survives_resume_generation_but_old_references_do_not(self):
        self.prepare()
        from app.web_citations import goal_state
        from app.web_target import remember_target
        with self.repo._operation(self.context,write=True) as (c,session):
            goal=goal_state(c,session)
            fields={'company':'构造公司','role':'工程实习','location':'广州','employment_type':'实习','posting_id':''}
            remember_target(c,session,base_hash=goal['baseHash'],fields=fields,user_message_id='synthetic-user')
        self.assertEqual(self.execute(self.lookup())['status'],'found')
        with database(self.path) as c,transaction(c):
            c.execute('UPDATE sessions SET current_resume_generation=current_resume_generation+1 WHERE session_id=?',(self.sid,))
        self.context=self.run_context();self.runtime.context=self.context
        view=self.bridge.view()
        self.assertEqual(view['references'],[])
        with self.repo._operation(self.context) as (c,session):
            self.assertEqual(goal_state(c,session)['clarifiedTarget'],fields)

    def test_literal_personal_fact_does_not_drop_negation_or_use_jd_hypotheses(self):
        from app.agent.tools import _literal_fact_source
        from app.agent.ports import SourceText
        from types import SimpleNamespace
        current=SimpleNamespace(sources=[SourceText(source_id='personal',kind='resume',text='我熟悉Python，但我不精通SQL。')])
        self.assertIsNotNone(_literal_fact_source(current,'熟悉Python'))
        self.assertIsNone(_literal_fact_source(current,'精通SQL'))
        self.assertIsNotNone(_literal_fact_source(current,'不精通SQL'))
        current.sources=[SourceText(source_id='hypothesis',kind='supplement',text='如果我精通SQL，能这样写吗？')]
        self.assertIsNone(_literal_fact_source(current,'精通SQL'))
        current.sources=[SourceText(source_id='jd',kind='jd',text='精通SQL')]
        self.assertIsNone(_literal_fact_source(current,'精通SQL'))

    def test_assessment_order_can_prefer_source_without_merging_mirrors(self):
        def handler(_request):
            data=search_response(TEXT,url='https://platform.example.com/jobs/1')
            data['Result']['Documents'].append({'Title':'构造公司工程实习','Url':'https://company.example.com/jobs/1',
                'Snippet':[{'Type':'text','Text':TEXT}]})
            return httpx.Response(200,json=data)
        self.prepare(handler)
        self.runtime.proof_transform=lambda ps,_:list(reversed(ps))
        result=self.execute(self.lookup())
        self.assertEqual(result['status'],'found')
        self.assertEqual(len(result['references']),1)
        self.assertEqual(result['references'][0]['url'],'https://company.example.com/jobs/1')
        evidence_call=next(v for system,v in self.runtime.calls if system.startswith('核对外部岗位参考'))
        self.assertEqual(evidence_call['references'][0]['source_host'],'platform.example.com')
