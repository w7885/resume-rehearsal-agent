"""Session display names persist without modifying resume facts or direction."""
from concurrent.futures import ThreadPoolExecutor
import unittest

from app import db
from app.business_service import BusinessService
from app.repository import Repository
import test_chat as chat_module


class SessionRenameTests(unittest.TestCase):
    setUp = chat_module.ChatTests.setUp
    request = chat_module.ChatTests.request
    version = chat_module.ChatTests.version
    chat = chat_module.ChatTests.chat
    conversation = chat_module.ChatTests.conversation
    send = chat_module.ChatTests.send
    material = chat_module.ChatTests.material
    count = chat_module.ChatTests.count
    assert_error = chat_module.ChatTests.assert_error

    def rename(self, session, title='测试会话名称', request_id='rename', revision=None):
        return self.request('PATCH', f"/api/sessions/{session['sessionId']}/title", data={
            'title':title, 'requestId':request_id,
            'expectedStateRevision':revision if revision is not None else self.version(session['sessionId'])})

    def test_title_is_persisted_and_only_display_metadata_changes(self):
        session=self.chat(); self.send(session,'我负责接口测试。')
        before=self.conversation(session)
        response=self.rename(session,'  互联网实习简历  ')
        self.assertEqual(response['status'],200,response)
        value=response['json']['data']
        self.assertEqual(value['title'],'互联网实习简历')
        self.assertEqual(value['stateRevision'],before['session']['stateRevision']+1)
        after=BusinessService(Repository(self.path)).repository.get_conversation(chat_module.RequestContext(session['sessionId']))
        self.assertEqual(after['session']['title'],value['title'])
        for key in ('direction','targetDirection','directionStatus','draftRevision','currentResumeGeneration'):
            self.assertEqual(after['session'][key],before['session'][key])
        for key in ('messages','materials','draft'):
            self.assertEqual(after[key],before[key])
        listed=self.request('GET','/api/sessions')['json']['data']
        self.assertEqual(next(s for s in listed if s['sessionId']==session['sessionId'])['title'],value['title'])

    def test_replay_and_concurrent_duplicate_requests_change_revision_only_once(self):
        session=self.chat();revision=self.version(session['sessionId'])
        first=self.rename(session,revision=revision)
        second=self.rename(session,revision=revision)
        self.assertEqual(first,second)
        self.assertEqual(self.version(session['sessionId']),revision+1)
        self.assert_error(self.rename(session,'不同名称',revision=revision),409,'request_conflict')
        # Separate service instances still share the same transactional receipt.
        service=BusinessService(Repository(self.path))
        from app.session_metadata import rename_session
        payload={'title':'并发测试','requestId':'concurrent','expectedStateRevision':revision+1}
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda _:rename_session(service,session['sessionId'],payload),range(2)))
        self.assertEqual(replies[0],replies[1])
        self.assertEqual(self.version(session['sessionId']),revision+2)

    def test_empty_long_multiline_and_invalid_payloads_are_rejected(self):
        session=self.chat();revision=self.version(session['sessionId'])
        for index,title in enumerate(('', '   ','名'*51,'两\n行','有\t制表符','控制\x00字符')):
            with self.subTest(title=repr(title)):
                self.assertEqual(self.rename(session,title,request_id=f'invalid-{index}')['status'],422)
        self.assertEqual(self.version(session['sessionId']),revision)
        self.assertEqual(self.rename(session,'名'*50,request_id='boundary')['status'],200)

    def test_conflict_busy_and_missing_session_do_not_write(self):
        session=self.chat();revision=self.version(session['sessionId'])
        self.assert_error(self.rename(session,revision=revision+1),409,'stale_state')
        with db.database(self.path) as c:
            c.execute("UPDATE sessions SET status='running' WHERE session_id=?",(session['sessionId'],))
        self.assert_error(self.rename(session),409,'run_busy')
        self.assertEqual(self.version(session['sessionId']),revision)
        self.assert_error(self.request('PATCH','/api/sessions/not-a-session/title',data={
            'title':'测试','requestId':'missing','expectedStateRevision':1}),404,'session_not_found')

    def test_custom_title_is_not_replaced_by_the_first_message(self):
        session=self.chat();self.rename(session,'自定义名称')
        self.send(session,'第一条用户消息。')
        self.assertEqual(self.conversation(session)['session']['title'],'自定义名称')

    def test_same_explicit_title_is_noop_and_other_sessions_are_untouched(self):
        target=self.chat();other=self.chat(request_id='other')
        self.rename(target,'固定名称');revision=self.version(target['sessionId'])
        self.assertEqual(self.rename(target,'固定名称',request_id='same-name')['status'],200)
        self.assertEqual(self.version(target['sessionId']),revision)
        self.assertEqual(self.conversation(other)['session']['title'],other['title'])

    def test_failed_write_rolls_back_title_version_and_receipt(self):
        session=self.chat();before=self.conversation(session)
        with db.database(self.path) as c:
            c.execute("CREATE TRIGGER fail_rename BEFORE UPDATE OF chat_title ON sessions BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.assertEqual(self.rename(session)['status'],503)
        self.assertEqual(self.conversation(session),before)
        with db.database(self.path,read_only=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM request_log WHERE operation='rename_session'").fetchone()[0],0)

    def test_method_discovery_is_read_only_and_does_not_require_a_session(self):
        before=self.count('sessions')
        reply=self.request('OPTIONS','/api/sessions/__capability__/title')
        self.assertEqual(reply['status'],204)
        self.assertIn(b'PATCH',reply['headers'][b'allow'])
        self.assertEqual(self.count('sessions'),before)


if __name__=='__main__':
    unittest.main()
