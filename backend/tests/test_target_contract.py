"""Strict goal contract and privacy-safe refusal diagnostics, no live models."""
import json,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from pydantic import ValidationError
from app.db import database,transaction
from app.agent.llm import LLMError
from app.web_provider import WebProviderError
import test_agent_web as fixture
import test_web_host as host_fixture

SAP='SAP / Full Stack AI-Native Developer Intern / Shanghai / Intern / REF2624S'
VALID={'status':'resolved','company':'SAP','role':'Full Stack AI-Native Developer Intern','location':'Shanghai','employment_type':'intern','posting_id':'REF2624S'}

class TargetContractTests(unittest.TestCase):
    settings=fixture.AgentWebTests.settings
    run_context=fixture.AgentWebTests.run_context
    prepare=fixture.AgentWebTests.prepare
    execute=fixture.AgentWebTests.execute
    lookup=fixture.AgentWebTests.lookup
    def setUp(self):
        fixture.AgentWebTests.setUp(self);self.prepare()
        with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',(SAP,self.sid))
        self.runtime.public_goal=dict(VALID)
    def target(self,changes=None):
        self.runtime.public_goal={**VALID,**(changes or {})};self.bridge.parsed={}
        return self.execute(self.bridge._target(self.bridge.state()[0]))
    def test_original_sap_target_and_public_only_helper_input(self):
        target=self.target()
        self.assertEqual(target.company,'SAP');self.assertEqual(target.posting_id,'REF2624S')
        self.assertEqual(self.bridge.target_check['code'],'resolved')
        self.assertTrue(all(v['anchored'] for v in self.bridge.target_check['fields'].values()))
        instruction,value=self.runtime.calls[-1]
        self.assertEqual(set(value),{'goal','userJD','targetReply'})
        self.assertEqual(value['goal'],SAP);self.assertEqual(value['userJD'],'')
        self.assertIn('必须为resolved',instruction)
        self.assertEqual(self.runtime.sent,[])
    def test_unsupported_status_is_not_treated_as_resolved_or_logged_raw(self):
        self.assertIsNone(self.target({'status':'private-status-do-not-echo'}))
        self.assertEqual(self.bridge.target_check['code'],'unsupported_status')
        self.assertEqual(self.bridge.target_check['model_status'],'unsupported')
        self.assertNotIn('private-status-do-not-echo',json.dumps(self.runtime.events))
        self.assertEqual(self.runtime.sent,[])
    def test_missing_and_ambiguous_do_not_search_even_with_anchored_fields(self):
        for status in ['missing','ambiguous']:
            self.assertIsNone(self.target({'status':status}))
            self.assertEqual(self.bridge.target_check['code'],'model_'+status)
        self.assertEqual(self.runtime.sent,[])
    def test_empty_required_role_is_identified_without_query(self):
        self.assertIsNone(self.target({'role':''}))
        self.assertEqual(self.bridge.target_check['code'],'required_field_missing')
        self.assertEqual(self.bridge.target_check['rejected_field'],'role')
    def test_translated_location_is_still_rejected(self):
        self.assertIsNone(self.target({'location':'上海'}))
        check=self.bridge.target_check
        self.assertEqual(check['code'],'field_not_anchored');self.assertEqual(check['rejected_field'],'location')
        self.assertFalse(check['fields']['location']['anchored']);self.assertNotIn('上海',json.dumps(check,ensure_ascii=False))
    def test_changed_company_case_is_still_rejected(self):
        self.assertIsNone(self.target({'company':'sap'}))
        self.assertEqual(self.bridge.target_check['rejected_field'],'company')
    def test_invented_posting_id_is_still_rejected(self):
        self.assertIsNone(self.target({'posting_id':'REF-OTHER'}))
        self.assertEqual(self.bridge.target_check['rejected_field'],'posting_id')
        self.assertNotIn('REF-OTHER',json.dumps(self.runtime.events))
    def test_unknown_employment_type_cannot_dispatch(self):
        self.assertIsNone(self.target({'employment_type':'unknown'}))
        self.assertEqual(self.bridge.target_check['code'],'type_unconfirmed');self.assertEqual(self.runtime.sent,[])
    def test_type_requires_evidence_in_input(self):
        direction=SAP.replace('Developer Intern','Developer').replace('/ Intern /','/ Fulltime /')
        with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',(direction,self.sid))
        self.assertIsNone(self.target({'role':'Full Stack AI-Native Developer'}))
        self.assertEqual(self.bridge.target_check['code'],'type_not_anchored')
    def test_schema_failure_records_only_safe_markers_and_preserves_error(self):
        with self.assertRaises(ValidationError):self.target({'company':123})
        self.assertEqual(self.bridge.target_check['code'],'schema_invalid')
        self.assertFalse(self.bridge.target_check['fields']['company']['string'])
    def test_extra_schema_keys_and_values_are_not_exposed(self):
        with self.assertRaises(ValidationError):self.target({'secret@example.com':'private-extra-value'})
        check=self.bridge.target_check
        self.assertEqual(check['unexpected_field_count'],1)
        text=json.dumps(self.runtime.events)
        self.assertNotIn('secret@example.com',text);self.assertNotIn('private-extra-value',text)
    def test_contact_filter_remains_and_diagnostics_do_not_echo_contact(self):
        email='private@example.com'
        with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',(SAP.replace('SAP',email),self.sid))
        with self.assertRaises(WebProviderError):self.target({'company':email})
        self.assertEqual(self.bridge.target_check['code'],'public_target_invalid')
        self.assertEqual(self.bridge.target_check['provider_code'],'private_query_rejected')
        self.assertNotIn(email,json.dumps(self.runtime.events));self.assertEqual(self.runtime.sent,[])
    def test_query_length_limit_is_preserved_and_distinguished_from_privacy(self):
        values={'company':'公司'+'A'*20,'role':'工程'+'B'*50,'location':'城市'+'C'*20,'posting_id':'编号'+'D'*20}
        proof=' / '.join([values['company'],values['role'],values['location'],'实习',values['posting_id']])
        with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',(proof,self.sid))
        with self.assertRaises(WebProviderError):self.target(values)
        self.assertEqual(self.bridge.target_check['provider_code'],'query_too_long')
        self.assertEqual(self.runtime.sent,[])
    def test_parsed_target_reuse_does_not_add_a_model_call(self):
        first=self.target();self.runtime.calls.clear()
        again=self.execute(self.bridge._target(self.bridge.state()[0]))
        self.assertEqual(first,again);self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.bridge.target_check['code'],'reused_parsed_target')
    def test_saved_public_target_reuse_does_not_add_a_model_call(self):
        goal={**self.bridge.state()[0],'clarifiedTarget':{'company':'SAP','role':VALID['role'],'location':'Shanghai','employment_type':'实习','posting_id':'REF2624S'}}
        target=self.execute(self.bridge._target(goal))
        self.assertEqual(target.company,'SAP');self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.bridge.target_check['code'],'reused_user_target')
    def test_empty_or_ambiguous_input_does_not_call_model(self):
        goal={**self.bridge.state()[0],'direction':'','jd':''}
        self.assertIsNone(self.execute(self.bridge._target(goal)))
        self.assertEqual(self.bridge.target_check['code'],'input_empty')
        goal['direction']='甲公司或者乙公司'
        self.assertIsNone(self.execute(self.bridge._target(goal)))
        self.assertEqual(self.bridge.target_check['code'],'input_ambiguous');self.assertEqual(self.runtime.calls,[])
    def test_model_error_remains_an_error_without_logging_exception_content(self):
        with patch.object(self.runtime,'_json',new=AsyncMock(side_effect=LLMError('llm_http_error','do-not-log-error-content'))):
            with self.assertRaises(LLMError):self.execute(self.bridge._target(self.bridge.state()[0]))
        self.assertEqual(self.bridge.target_check['code'],'model_call_failed')
        self.assertNotIn('do-not-log-error-content',json.dumps(self.runtime.events))
    def test_clarification_observation_contains_safe_refusal_code(self):
        self.runtime.public_goal={**VALID,'status':'clear'}
        result=self.execute(self.lookup())
        self.assertEqual(result['status'],'clarify_target')
        self.assertEqual(result['targetCheck']['code'],'unsupported_status')
        self.assertEqual(self.runtime.sent,[])

class TargetDiagnosticHostTests(unittest.TestCase):
    setUp=host_fixture.WebHostTests.setUp
    request=host_fixture.WebHostTests.request
    version=host_fixture.WebHostTests.version
    chat=host_fixture.WebHostTests.chat
    material=host_fixture.WebHostTests.material
    send=host_fixture.WebHostTests.send
    analyze=host_fixture.WebHostTests.analyze
    conversation=host_fixture.WebHostTests.conversation
    count=host_fixture.WebHostTests.count
    ready=host_fixture.WebHostTests.ready
    def test_safe_diagnostic_is_persisted_in_real_host_events_and_step_observation(self):
        session,user=self.ready()
        class InvalidStatusClient(host_fixture.WebAwareClient):
            async def complete_json(client,*,system,user,run_id='test'):
                if system.startswith('提取当前明确公开求职目标'):
                    return SimpleNamespace(data={**fixture.goal_data(),'status':'private-status-do-not-echo'})
                return await super().complete_json(system=system,user=user,run_id=run_id)
        self.host.client_factory=lambda runtime,budget:InvalidStatusClient(runtime)
        self.analyze(session,user)
        with database(self.path,read_only=True) as c:
            events=[json.loads(r[0]) for r in c.execute('SELECT payload_json FROM agent_stream_events')]
            docs=[json.loads(r[0]) for r in c.execute("SELECT doc_json FROM agent_state WHERE scope='run'")]
        checks=[event['payload']['targetCheck'] for event in events if event['payload'].get('stage')=='web_target_check']
        self.assertEqual(checks[0]['code'],'unsupported_status')
        self.assertEqual(docs[0]['steps'][0]['observation']['targetCheck']['code'],'unsupported_status')
        self.assertNotIn('private-status-do-not-echo',json.dumps(events+docs))
        self.assertEqual(self.sent,[]);self.assertEqual(self.count('suggestions'),0)

if __name__=='__main__':unittest.main()
