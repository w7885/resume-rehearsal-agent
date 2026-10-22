"""A-T11.1 synthetic Host reply/ask bindings; no semantic or model tests."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch
from app import db
from app.business_service import BusinessService
from app.repository import Repository,RepositoryError,RequestContext
import test_api
import test_recognition


class AssistantReplyTests(unittest.TestCase):
    setUp=test_api.ApiTests.setUp
    request=test_api.ApiTests.request
    version=test_api.ApiTests.version
    material=test_api.ApiTests.material
    count=test_api.ApiTests.count
    run_context=test_recognition.RecognitionTests.run_context

    def chat(self,key='create'):
        return self.request('POST','/api/chat/sessions',data={'requestId':key})['json']['data']

    def send(self,session,text,request_id='user',**extra):
        response=self.request('POST',f"/api/sessions/{session['sessionId']}/chat/messages",data={'requestId':request_id,'expectedStateRevision':self.version(session['sessionId']),'content':text,'attachmentIds':[],**extra})
        self.assertEqual(response['status'],201,response)
        return response['json']['data']['message']

    def setup_case(self):
        session=self.chat();self.material(session,text='# 构造简历\n整理 8 条反馈，协助测试。')
        user=self.send(session,'请帮我核对这段经历。')
        self.service=BusinessService(Repository(self.path));context=self.run_context(session)
        current=self.service.repository.get_session_context(context)
        return session,user,context,current

    def proposal(self,context,current,quote='整理 8 条反馈'):
        block=current['draft']['blocks'][0];source=current['sources'][0]
        return self.service.repository.save_suggestion(context,{'operation':'replace','target':{'blockId':block['blockId'],'expectedBlockRevision':block['blockRevision']},'before':block['markdown'],'after':'构造建议：协助整理反馈并测试。','reason':'构造修改理由','gaps':[], 'sourceRefs':[{'sourceId':source['sourceId'],'sourceRevision':source['sourceRevision'],'quote':quote}]})

    def save(self,context,user,kind='reply',ids=None,request_id='assistant',**extra):
        return self.service.save_assistant_message(context,{'requestId':request_id,'userMessageId':user['messageId'],'content':'构造回复：这是保存与关联测试，不是真实模型回复。','kind':kind,'suggestionIds':ids or [],**extra},synthetic=True)['data']['message']

    def conversation(self,session):
        return self.service.repository.get_conversation(RequestContext(session['sessionId']))

    def test_reply_saved_once_restored_and_does_not_change_session_draft_or_facts(self):
        session,user,context,_=self.setup_case();before=self.conversation(session)
        reply=self.save(context,user)
        after=self.conversation(session)
        self.assertEqual(reply['role'],'assistant');self.assertEqual(reply['assistant']['mode'],'synthetic')
        self.assertEqual(reply['assistant']['replyToUserMessageId'],user['messageId'])
        self.assertEqual(before['session'],after['session']);self.assertEqual(before['draft'],after['draft'])
        self.assertEqual(self.count('source_blocks'),1);self.assertEqual(self.count('suggestion_decisions'),0)
        self.assertEqual([m['sequence'] for m in after['messages']],[1,2])
        self.assertFalse(after['capabilities']['agentAvailable']);self.assertFalse(after['capabilities']['decisionExecutionAvailable'])
        self.assertEqual(self.save(context,user),reply);self.assertEqual(self.count('assistant_messages'),1)

    def test_prompt_binds_real_target_and_multiple_quotes_from_same_source(self):
        session,user,context,current=self.setup_case()
        a=self.proposal(context,current);b=self.proposal(context,current,'协助测试')
        reply=self.save(context,user,'change_question',[a['suggestionId'],b['suggestionId']])
        self.assertEqual(reply['assistant']['questionState'],'awaiting_reply')
        self.assertEqual(len(reply['assistant']['sourceRefs']),2)
        with db.database(self.path,read_only=True) as connection:
            snapshots=[json.loads(row[0]) for row in connection.execute('SELECT snapshot_json FROM assistant_suggestion_links ORDER BY suggestion_id')]
        self.assertEqual({p['suggestionId'] for p in snapshots},{a['suggestionId'],b['suggestionId']})
        self.assertTrue(all(p['target']==a['target'] and p['before']==a['before'] for p in snapshots))
        self.assertEqual(self.conversation(session)['draft'],current['draft'])

    def test_selected_reply_keeps_raw_text_and_s_to_s_plus_one_without_applying(self):
        session,user,context,current=self.setup_case();proposal=self.proposal(context,current)
        prompt=self.save(context,user,'change_question',[proposal['suggestionId']])
        reply=self.send(session,'  可以\n',request_id='answer',replyToMessageId=prompt['messageId'])
        self.assertEqual(reply['content'],'  可以\n');self.assertEqual(reply['replyToMessageId'],prompt['messageId'])
        self.assertEqual(reply['savedBeforeRevision'],context.expected_state_revision);self.assertEqual(reply['savedAfterRevision'],context.expected_state_revision+1)
        new_context=self.run_context(session);turn=self.service.get_chat_turn_context(new_context,reply['messageId'])
        self.assertEqual(turn['replyToMessageId'],prompt['messageId']);self.assertEqual(turn['candidatePrompts'][0]['state'],'reply_saved')
        self.assertEqual(self.count('suggestion_decisions'),0);self.assertEqual(self.count('source_blocks'),1)
        self.assertEqual(self.conversation(session)['draft'],current['draft'])
        self.assertEqual(self.service.repository.get_suggestion(RequestContext(session['sessionId']),proposal['suggestionId'])['status'],'pending')

    def test_unselected_reply_has_candidates_but_never_defaults_to_last_question(self):
        session,user,context,_=self.setup_case()
        first=self.save(context,user,'question',request_id='first')
        second=self.save(context,user,'question',request_id='second')
        reply=self.send(session,'可以',request_id='ambiguous')
        turn=self.service.get_chat_turn_context(self.run_context(session),reply['messageId'])
        self.assertIsNone(turn['replyToMessageId'])
        self.assertEqual({p['promptMessageId'] for p in turn['candidatePrompts']},{first['messageId'],second['messageId']})
        self.assertEqual(self.count('suggestion_decisions'),0)

    def test_new_input_and_old_trigger_or_cancelled_run_cannot_publish_reply(self):
        session,user,context,_=self.setup_case();self.send(session,'还有一条补充',request_id='new-input')
        with self.assertRaises(RepositoryError):self.save(context,user)
        with self.assertRaises(RepositoryError) as error:self.save(self.run_context(session),user)
        self.assertEqual(error.exception.code,'stale_state')
        self.assertEqual(self.count('assistant_messages'),0)

    def test_invalid_foreign_processed_or_changed_suggestions_are_not_question_targets(self):
        session,user,context,current=self.setup_case();proposal=self.proposal(context,current)
        for ids in [[],['missing'],[proposal['suggestionId'],proposal['suggestionId']]]:
            with self.assertRaises(RepositoryError):self.save(context,user,'change_question',ids)
        with db.database(self.path) as connection:connection.execute("UPDATE suggestions SET status = 'rejected' WHERE suggestion_id = ?",(proposal['suggestionId'],))
        with self.assertRaises(RepositoryError):self.save(context,user,'change_question',[proposal['suggestionId']])
        self.assertEqual(self.count('assistant_messages'),0)

    def test_source_correction_marks_prompt_outdated_but_preserves_exact_snapshot(self):
        session,user,context,current=self.setup_case();proposal=self.proposal(context,current)
        prompt=self.save(context,user,'change_question',[proposal['suggestionId']])
        source=current['sources'][0]
        self.service.repository.save_source(RequestContext(session['sessionId']),text='构造修正：仅协助，数量待核对',role=source['role'],origin=source['origin'],use_status='active',source_id=source['sourceId'],expected_source_revision=1)
        restored=self.conversation(session)['messages'][-1]
        self.assertEqual(restored['assistant']['questionState'],'outdated')
        self.assertEqual(restored['assistant']['sourceRefs'][0]['sourceRevision'],1)
        self.assertEqual(restored['messageId'],prompt['messageId'])

    def test_attachment_upload_between_prompt_and_reply_is_traceable_but_not_normal_bridge(self):
        session,user,context,_=self.setup_case();prompt=self.save(context,user,'question')
        material=self.material(session,text='构造附件补充',request_id='new-material',role='unknown')['json']['data']['material']
        reply=self.send(session,'请查看这份补充',request_id='reply-with-file',attachmentIds=[material['materialId']],replyToMessageId=prompt['messageId'])
        turn=self.service.get_chat_turn_context(self.run_context(session),reply['messageId'])
        self.assertEqual(turn['candidatePrompts'][0]['state'],'outdated')
        self.assertEqual(reply['replyToMessageId'],prompt['messageId']);self.assertEqual(self.count('suggestion_decisions'),0)

    def test_new_question_after_reply_invalidates_prior_bridge(self):
        session,user,context,_=self.setup_case();prompt=self.save(context,user,'question')
        answer=self.send(session,'可以',request_id='answer',replyToMessageId=prompt['messageId'])
        new_context=self.run_context(session)
        self.assertEqual(self.service.get_chat_turn_context(new_context,answer['messageId'])['candidatePrompts'][0]['state'],'reply_saved')
        self.save(new_context,answer,'question',request_id='new-question')
        self.assertEqual(self.service.get_chat_turn_context(new_context,answer['messageId'])['candidatePrompts'][0]['state'],'outdated')

    def test_no_unbound_host_and_public_user_cannot_forge_assistant_or_decision(self):
        session,user,context,_=self.setup_case()
        with self.assertRaises(RepositoryError):self.save(RequestContext(session['sessionId']),user)
        body={'requestId':'spoof','expectedStateRevision':self.version(session['sessionId']),'content':'已接受','role':'assistant','attachmentIds':[]}
        self.assertEqual(self.request('POST',f"/api/sessions/{session['sessionId']}/chat/messages",data=body)['status'],422)
        with self.assertRaises(RepositoryError):self.save(context,user,decision='accept')
        ordinary=self.save(context,user)
        response=self.request('POST',f"/api/sessions/{session['sessionId']}/chat/messages",data={k:v for k,v in {**body,'requestId':'not-question','replyToMessageId':ordinary['messageId']}.items() if k!='role'})
        self.assertEqual(response['status'],404)

    def test_concurrent_reply_retry_and_cache_failure_are_atomic(self):
        session,user,context,current=self.setup_case();proposal=self.proposal(context,current)
        payload={'requestId':'same-assistant','userMessageId':user['messageId'],'content':'构造询问','kind':'change_question','suggestionIds':[proposal['suggestionId']]}
        real=json.dumps
        def fail(value,*args,**kwargs):
            if isinstance(value,dict) and value.get('ok') and isinstance(value.get('data'),dict) and value['data'].get('message',{}).get('role')=='assistant':raise OSError('synthetic cache failure')
            return real(value,*args,**kwargs)
        with patch('app.business_service.json.dumps',side_effect=fail),self.assertRaises(OSError):self.service.save_assistant_message(context,payload,synthetic=True)
        self.assertEqual(self.count('messages'),1);self.assertEqual(self.count('assistant_suggestion_links'),0)
        with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(lambda _:self.service.save_assistant_message(context,payload,synthetic=True),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.count('messages'),2);self.assertEqual(self.count('assistant_suggestion_links'),1)

    def test_session_delete_cascades_reply_links_and_late_callbacks_cannot_recreate(self):
        session,user,context,current=self.setup_case();proposal=self.proposal(context,current)
        prompt=self.save(context,user,'change_question',[proposal['suggestionId']])
        self.send(session,'不能',request_id='answer',replyToMessageId=prompt['messageId'])
        response=self.request('DELETE',f"/api/sessions/{session['sessionId']}",data={'requestId':'delete','expectedStateRevision':self.version(session['sessionId']),'confirmed':True})
        self.assertEqual(response['status'],200,response)
        for table in ('messages','assistant_messages','assistant_suggestion_links'):self.assertEqual(self.count(table),0)
        with self.assertRaises(RepositoryError):self.save(context,user)

    def test_v8_upgrade_preserves_legacy_raw_messages_and_does_not_invent_reply_links(self):
        with db.database(':memory:') as connection:
            with patch.object(db,'MIGRATIONS',db.MIGRATIONS[:8]),patch.object(db,'SCHEMA_VERSION',8):
                db.initialize_database(connection)
                connection.execute("INSERT INTO sessions(session_id,direction) VALUES ('s','构造')")
                connection.execute("INSERT INTO messages(message_id,session_id,role,content) VALUES ('m','s','user','原话')")
            db.initialize_database(connection)
            row=connection.execute('SELECT * FROM messages').fetchone()
            self.assertEqual(row['content'],'原话');self.assertIsNone(row['reply_to_message_id']);self.assertIsNone(row['saved_after_revision']);self.assertIsNone(row['reply_candidates_json'])
            self.assertEqual(connection.execute('SELECT count(*) FROM assistant_messages').fetchone()[0],0)


if __name__=='__main__':unittest.main()
