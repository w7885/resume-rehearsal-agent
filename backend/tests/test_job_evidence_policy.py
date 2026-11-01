"""Job evidence state uses verified job sources, never sufficient personal facts."""
import unittest
from unittest.mock import patch
from app.db import database,transaction
from app.repository import RequestContext
from app.agent_web import AgentWebBridge
import test_agent_web as fixture

class JobEvidencePolicyTests(unittest.TestCase):
    setUp=fixture.AgentWebTests.setUp
    settings=fixture.AgentWebTests.settings
    run_context=fixture.AgentWebTests.run_context
    prepare=fixture.AgentWebTests.prepare
    lookup=fixture.AgentWebTests.lookup
    execute=fixture.AgentWebTests.execute

    def add_jd(self,text):
        user=RequestContext(self.sid)
        message=self.repo.save_message(user,text,kind='message')
        self.repo.save_source(user,text=message['content'],role='jd',origin={'kind':'message','messageId':message['messageId']},use_status='active')
        self.context=self.run_context();self.runtime.context=self.context

    def test_personal_skill_facts_do_not_fill_missing_job_requirements(self):
        self.prepare()
        user=RequestContext(self.sid)
        text='熟悉Python、Git和HTTP接口调用；暂无SQL项目经历。'
        message=self.repo.save_message(user,text,kind='message')
        self.repo.save_source(user,text=text,role='supplement',origin={'kind':'message','messageId':message['messageId']},use_status='active')
        self.context=self.run_context();self.runtime.context=self.context
        view=self.bridge.view()
        self.assertTrue(view['available']);self.assertFalse(view['hasUserJD'])
        self.assertEqual(view['jobEvidenceStatus'],'missing')
        self.assertEqual(self.runtime.calls,[]);self.assertEqual(self.runtime.sent,[])

    def test_complete_user_jd_is_sufficient_without_any_search_or_model_helper(self):
        self.prepare();self.add_jd('岗位职责：负责接口开发与测试。任职要求：熟悉Python和HTTP，能协作完成任务。')
        self.assertEqual(self.bridge.view()['jobEvidenceStatus'],'user_jd')
        result=self.execute(self.lookup())
        self.assertEqual(result['status'],'provided_jd')
        self.assertEqual(self.runtime.calls,[]);self.assertEqual(self.runtime.sent,[])

    def test_partial_jd_is_present_but_not_sufficient_job_evidence(self):
        self.prepare();self.add_jd('岗位要求：熟悉Python。')
        view=self.bridge.view()
        self.assertTrue(view['hasUserJD']);self.assertFalse(view['userJDComplete'])
        self.assertEqual(view['jobEvidenceStatus'],'missing')
        self.assertEqual(self.runtime.sent,[])

    def test_current_verified_job_reference_is_reused_without_another_request(self):
        self.prepare();self.execute(self.lookup())
        before=(len(self.runtime.calls),len(self.runtime.sent))
        self.bridge=AgentWebBridge(self.runtime)
        view=self.bridge.view()
        self.assertEqual(view['jobEvidenceStatus'],'verified_job_reference')
        result=self.execute(self.lookup())
        self.assertTrue(result['reused'])
        self.assertEqual((len(self.runtime.calls),len(self.runtime.sent)),before)

    def test_verified_company_background_does_not_supply_job_requirements(self):
        self.prepare()
        self.runtime.proof_transform=lambda ps,_:[{**p,'content_quote':'岗位职责：负责接口开发和测试。'} for p in ps]
        self.assertEqual(self.execute(self.lookup('company_background'))['status'],'found')
        view=self.bridge.view()
        self.assertTrue(view['references']);self.assertEqual(view['jobEvidenceStatus'],'missing')
        self.assertEqual(view['references'][0]['purpose'],'company_background')

    def test_verified_terminology_does_not_supply_job_requirements(self):
        self.prepare()
        self.runtime.proof_transform=lambda ps,_:[{**p,'content_quote':'岗位职责：负责接口开发和测试。'} for p in ps]
        self.assertEqual(self.execute(self.lookup('terminology'))['status'],'found')
        self.assertEqual(self.bridge.view()['jobEvidenceStatus'],'missing')

    def test_goal_change_invalidates_previously_verified_job_basis(self):
        self.prepare();self.execute(self.lookup())
        self.assertEqual(self.bridge.view()['jobEvidenceStatus'],'verified_job_reference')
        with database(self.path) as c,transaction(c):
            c.execute('UPDATE sessions SET direction=? WHERE session_id=?',('另一家公司 广州 工程实习',self.sid))
        view=self.bridge.view()
        self.assertEqual(view['jobEvidenceStatus'],'missing');self.assertEqual(view['references'],[])

    def test_closed_permission_never_retains_verified_job_context(self):
        self.prepare();self.execute(self.lookup());self.settings(False,2)
        view=self.bridge.view()
        self.assertFalse(view['available']);self.assertEqual(view['references'],[])
        self.assertNotIn('jobEvidenceStatus',view)
        self.assertEqual(self.bridge.citation_ids(),[])

    def test_job_basis_is_exposed_even_when_other_purpose_caches_are_full(self):
        self.prepare()
        groups={purpose:[{'referenceId':purpose+str(i),'purpose':purpose,'content':'构造已核对内容'} for i in range(5)]
                for purpose in ('job_requirements','company_background','terminology')}
        with patch('app.agent_web.cached_references',side_effect=lambda repo,context,purpose:groups[purpose]):
            view=self.bridge.view()
        self.assertEqual(len(view['references']),3)
        self.assertEqual({r['purpose'] for r in view['references']},set(groups))
        self.assertEqual(view['jobEvidenceStatus'],'verified_job_reference')
        self.assertEqual(self.runtime.calls,[]);self.assertEqual(self.runtime.sent,[])

if __name__=='__main__':unittest.main()
