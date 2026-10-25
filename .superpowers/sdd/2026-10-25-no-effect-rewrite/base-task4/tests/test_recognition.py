"""Synthetic Host callbacks. No real provider, model or personal DB."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from PIL import Image

from app import db
from app.business_service import BusinessService
from app.page_renderer import RENDER_VERSION
from app.repository import Repository, RepositoryError, RequestContext
import test_api
from test_material_pages import picture


def scans():
    output=BytesIO()
    with Image.new('RGB',(80,100),'white') as a, Image.new('RGB',(80,100),'navy') as b:
        a.save(output,format='PDF',save_all=True,append_images=[b])
    return output.getvalue()


class RecognitionTests(unittest.TestCase):
    setUp=test_api.ApiTests.setUp
    request=test_api.ApiTests.request
    new_session=test_api.ApiTests.new_session
    version=test_api.ApiTests.version
    count=test_api.ApiTests.count
    assert_error=test_api.ApiTests.assert_error

    def upload(self,session,raw=None,name='a.png',media_type='image/png'):
        raw=picture() if raw is None else raw
        self.sequence+=1
        query=urlencode({'requestId':f'upload-{self.sequence}','expectedStateRevision':self.version(session['sessionId']),'name':name,'role':'unknown'})
        response=self.request('POST',f"/api/sessions/{session['sessionId']}/materials?{query}",body=raw,headers={'content-type':media_type})
        self.assertEqual(response['status'],201,response)
        material=response['json']['data']['material']
        return material,hashlib.sha256(raw).hexdigest()

    def prepare(self,session,material,page=1):
        response=self.request('POST',f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/pages/prepare",data={'requestId':f"prep-{material['materialId']}-{page}",'expectedStateRevision':self.version(session['sessionId']),'pageNumber':page})
        self.assertEqual(response['status'],200,response)

    def run_context(self,session):
        sid=session['sessionId']; revision=self.version(sid)
        with db.database(self.path) as connection:
            generation=connection.execute('SELECT coalesce(max(generation),0)+1 FROM runs WHERE session_id = ?',(sid,)).fetchone()[0]
            resume=connection.execute('SELECT current_resume_generation FROM sessions WHERE session_id = ?',(sid,)).fetchone()[0]
            connection.execute("UPDATE runs SET status = 'interrupted' WHERE session_id = ? AND status = 'running'",(sid,))
            rid=f'host-{sid}-{generation}'
            connection.execute("INSERT INTO runs(run_id,session_id,generation,input_state_revision,resume_generation,status) VALUES (?,?,?,?,?,'running')",(rid,sid,generation,revision,resume))
        return RequestContext(sid,revision,rid,generation)

    def setup_image(self):
        session=self.new_session(); material,digest=self.upload(session); self.prepare(session,material)
        self.service=BusinessService(Repository(self.path))
        return session,material,digest,self.run_context(session)

    def payload(self,material,digest,**changes):
        return {'requestId':'result-1','materialId':material['materialId'],'pageNumber':1,'contentHash':digest,'renderVersion':RENDER_VERSION,'recognitionVersion':'synthetic-v1','expectedSourceRevision':0,'status':'success','text':'构造识别文字：协助测试 8 条反馈','role':'resume','errorMessage':'',**changes}

    def test_success_is_pending_source_and_preserves_original_draft_and_running_context(self):
        session,material,digest,context=self.setup_image(); repo=self.service.repository
        before=repo.get_conversation(RequestContext(session['sessionId']))
        result=self.service.save_recognition_result(context,self.payload(material,digest),synthetic=True)['data']
        self.assertEqual(result['mode'],'synthetic'); self.assertEqual(result['currentSource']['role'],'unknown')
        after=repo.get_conversation(RequestContext(session['sessionId']))
        self.assertEqual(before['draft'],after['draft']); self.assertEqual(before['session'],after['session'])
        self.assertEqual(repo.get_session_context(context)['sources'],[])
        self.assertEqual(self.count('source_blocks'),1)
        self.assertEqual(repo.read_material_file(RequestContext(session['sessionId']),material['materialId'])['originalData'],picture())
        report=repo.get_material_detail(RequestContext(session['sessionId']),material['materialId'])['material']
        self.assertEqual(report['parseStatus'],'parsed'); self.assertEqual(report['useStatus'],'pending')
        self.assertEqual(report['recognitionReport']['mode'],'synthetic')

    def test_concurrent_replay_adds_one_snapshot_and_changed_body_conflicts(self):
        _,material,digest,context=self.setup_image(); payload=self.payload(material,digest)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results=list(workers.map(lambda _: self.service.save_recognition_result(context,payload,synthetic=True),range(2)))
        self.assertEqual(results[0],results[1]); self.assertEqual(self.count('recognition_results'),1); self.assertEqual(self.count('source_blocks'),1)
        with self.assertRaises(RepositoryError) as error:
            self.service.save_recognition_result(context,{**payload,'text':'换了文字'},synthetic=True)
        self.assertEqual(error.exception.code,'request_conflict')

    def test_old_run_old_resume_and_wrong_image_versions_cannot_write(self):
        session,material,digest,context=self.setup_image()
        for changes in [{'contentHash':'wrong'},{'renderVersion':'old-render'},{'pageNumber':2},{'role':'made-up'}]:
            with self.assertRaises(RepositoryError):
                self.service.save_recognition_result(context,self.payload(material,digest,**changes),synthetic=True)
        self.run_context(session)
        with self.assertRaises(RepositoryError) as error:
            self.service.save_recognition_result(context,self.payload(material,digest),synthetic=True)
        self.assertEqual(error.exception.code,'cancelled')
        current=self.run_context(session)
        with db.database(self.path) as connection:
            connection.execute('UPDATE attachments SET resume_generation = 99 WHERE attachment_id = ?',(material['materialId'],))
        with self.assertRaises(RepositoryError):
            self.service.save_recognition_result(current,self.payload(material,digest),synthetic=True)
        self.assertEqual(self.count('recognition_results'),0)

    def test_foreign_and_unbound_context_and_unknown_fields_are_rejected(self):
        session,material,digest,context=self.setup_image()
        with self.assertRaises(RepositoryError) as error:
            self.service.save_recognition_result(RequestContext(session['sessionId']),self.payload(material,digest),synthetic=True)
        self.assertEqual(error.exception.code,'scope_violation')
        other=self.new_session(); other_context=self.run_context(other)
        with self.assertRaises(RepositoryError):
            self.service.save_recognition_result(other_context,self.payload(material,digest),synthetic=True)
        for changes in [{'path':'C:/arbitrary.png'},{'status':'failed','text':'不应写入失败文字','errorMessage':'failed'},{'status':'partial','errorMessage':''}]:
            with self.assertRaises(RepositoryError):
                self.service.save_recognition_result(context,self.payload(material,digest,**changes),synthetic=True)
        self.assertEqual(self.count('source_blocks'),0)

    def test_partial_failure_keeps_other_pages_and_user_retry_waits_for_host(self):
        session=self.new_session(); material,digest=self.upload(session,scans(),'scans.pdf','application/pdf')
        for page in (1,2): self.prepare(session,material,page)
        self.service=BusinessService(Repository(self.path)); context=self.run_context(session)
        self.service.save_recognition_result(context,self.payload(material,digest),synthetic=True)
        self.service.save_recognition_result(context,self.payload(material,digest,requestId='failed-page',pageNumber=2,status='failed',text='',errorMessage='构造识别失败'),synthetic=True)
        detail=self.service.repository.get_material_detail(RequestContext(session['sessionId']),material['materialId'])
        self.assertEqual(detail['material']['parseStatus'],'partial'); self.assertEqual(len(detail['sources']),1)
        self.assertEqual(detail['material']['recognitionReport']['failed'],1)
        url=f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/recognition-retry"
        payload={'requestId':'ask-retry','expectedStateRevision':self.version(session['sessionId'])}
        retry=self.request('POST',url,data=payload)['json']['data']
        self.assertEqual(self.request('POST',url,data=payload)['json']['data'],retry)
        self.assertEqual(self.count('recognition_retry_requests'),1)
        self.assertEqual(self.count('recognition_results'),2)
        with self.assertRaises(RepositoryError):
            self.service.save_recognition_result(context,self.payload(material,digest,requestId='late-after-request',expectedSourceRevision=1),synthetic=True)
        current=self.run_context(session)
        for page in (1,2):
            self.service.save_recognition_result(current,self.payload(material,digest,requestId=f'finish-{page}',pageNumber=page,expectedSourceRevision=1 if page==1 else 0,retryId=retry['retryId']),synthetic=True)
        with db.database(self.path) as connection:
            self.assertEqual(connection.execute('SELECT status FROM recognition_retry_requests').fetchone()[0],'completed')
        self.assertEqual(self.service.repository.get_material_detail(RequestContext(session['sessionId']),material['materialId'])['material']['parseStatus'],'parsed')

    def test_human_correction_is_preserved_and_late_result_cannot_override_it(self):
        session,material,digest,context=self.setup_image()
        result=self.service.save_recognition_result(context,self.payload(material,digest),synthetic=True)['data']
        source=result['currentSource']
        url=f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/sources/{source['sourceId']}/revisions"
        response=self.request('POST',url,data={'requestId':'human','expectedStateRevision':self.version(session['sessionId']),'expectedSourceRevision':1,'text':'构造人工修正：数量待核对'})
        self.assertEqual(response['status'],200,response)
        new_context=self.run_context(session)
        cached=self.service.get_cached_recognition(new_context,material['materialId'],1,'synthetic-v1')
        self.assertTrue(cached['cacheHit']); self.assertTrue(cached['result']['userCorrected'])
        self.assertEqual(cached['currentSource']['text'],'构造人工修正：数量待核对')
        with self.assertRaises(RepositoryError) as error:
            self.service.save_recognition_result(new_context,self.payload(material,digest,requestId='late-old',recognitionVersion='synthetic-v2',expectedSourceRevision=1),synthetic=True)
        self.assertEqual(error.exception.code,'stale_state'); self.assertEqual(self.count('source_blocks'),2)
        newer=self.service.save_recognition_result(new_context,self.payload(material,digest,requestId='explicit-new',recognitionVersion='synthetic-v2',expectedSourceRevision=2,text='新的构造识别文字'),synthetic=True)['data']
        self.assertTrue(newer['preservedCorrection']);self.assertTrue(newer['userCorrected'])
        self.assertEqual(newer['currentSource']['text'],'构造人工修正：数量待核对')
        self.assertEqual(newer['text'],'新的构造识别文字');self.assertEqual(self.count('source_blocks'),2)

    def test_request_cache_failure_rolls_back_result_source_and_report(self):
        _,material,digest,context=self.setup_image(); real=json.dumps
        def fail(value,*args,**kwargs):
            if isinstance(value,dict) and value.get('ok') and isinstance(value.get('data'),dict) and 'resultId' in value['data']:
                raise OSError('synthetic cache failure')
            return real(value,*args,**kwargs)
        with patch('app.business_service.json.dumps',side_effect=fail), self.assertRaises(OSError):
            self.service.save_recognition_result(context,self.payload(material,digest),synthetic=True)
        self.assertEqual(self.count('recognition_results'),0); self.assertEqual(self.count('source_blocks'),0)
        self.assertIsNone(self.service.repository.get_material_detail(RequestContext(context.session_id),material['materialId'])['material']['recognitionReport'])

    def test_late_same_run_result_is_rejected_and_independent_retry_intents_are_deduplicated(self):
        session,material,digest,context=self.setup_image()
        self.service.save_recognition_result(context,self.payload(material,digest),synthetic=True)
        with self.assertRaises(RepositoryError) as error:
            self.service.save_recognition_result(context,self.payload(material,digest,requestId='different-late-job'),synthetic=True)
        self.assertEqual(error.exception.code,'stale_state')
        url=f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/recognition-retry"
        first=self.request('POST',url,data={'requestId':'retry-one','expectedStateRevision':self.version(session['sessionId'])})['json']['data']
        version=self.version(session['sessionId'])
        second=self.request('POST',url,data={'requestId':'retry-two','expectedStateRevision':version})['json']['data']
        self.assertEqual(first['retryId'],second['retryId']);self.assertEqual(self.version(session['sessionId']),version)
        self.assertEqual(self.count('recognition_results'),1);self.assertEqual(self.count('recognition_retry_requests'),1)

    def test_reordering_only_images_preserves_nonimages_original_bytes_and_draft(self):
        session=self.new_session(); a,_=self.upload(session,name='a.png')
        pdf_material,_=self.upload(session,scans(),'fixed.pdf','application/pdf')
        b,_=self.upload(session,picture((50,60)),name='b.png')
        repo=Repository(self.path); before=repo.get_conversation(RequestContext(session['sessionId']))
        url=f"/api/sessions/{session['sessionId']}/materials/image-order"
        payload={'requestId':'reorder','expectedStateRevision':self.version(session['sessionId']),'materialIds':[b['materialId'],a['materialId']]}
        result=self.request('POST',url,data=payload)
        self.assertEqual(result['status'],200,result)
        after=result['json']['data']
        self.assertEqual([m['materialId'] for m in after['materials']],[b['materialId'],pdf_material['materialId'],a['materialId']])
        self.assertEqual(before['draft'],after['draft']); self.assertEqual(after['session']['stateRevision'],before['session']['stateRevision']+1)
        self.assertEqual(self.request('POST',url,data=payload)['json'],result['json'])
        self.assertEqual([m['materialId'] for m in repo.get_session_context(RequestContext(session['sessionId']))['materials']],[b['materialId'],pdf_material['materialId'],a['materialId']])
        self.assertEqual(repo.read_material_file(RequestContext(session['sessionId']),a['materialId'])['originalData'],picture())
        for bad in [[a['materialId']],[a['materialId'],a['materialId']],[a['materialId'],'foreign']]:
            self.assertFalse(self.request('POST',url,data={**payload,'requestId':str(bad),'expectedStateRevision':self.version(session['sessionId']),'materialIds':bad})['json']['ok'])

    def test_delete_cascades_callbacks_and_requests_and_late_callback_is_rejected(self):
        session,material,digest,context=self.setup_image()
        self.service.save_recognition_result(context,self.payload(material,digest),synthetic=True)
        self.request('POST',f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/recognition-retry",data={'requestId':'retry','expectedStateRevision':self.version(session['sessionId'])})
        self.request('DELETE',f"/api/sessions/{session['sessionId']}",data={'requestId':'delete','expectedStateRevision':self.version(session['sessionId']),'confirmed':True})
        self.assertEqual(self.count('recognition_results'),0); self.assertEqual(self.count('recognition_retry_requests'),0)
        with self.assertRaises(RepositoryError):
            self.service.save_recognition_result(context,self.payload(material,digest),synthetic=True)

    def test_order_storage_failure_rolls_back_all_positions_and_session_revision(self):
        session=self.new_session();a,_=self.upload(session,name='a.png');b,_=self.upload(session,name='b.png')
        repo=Repository(self.path);context=RequestContext(session['sessionId']);before=repo.get_conversation(context)
        with patch.object(Repository,'_touch',side_effect=OSError('synthetic order failure')):
            response=self.request('POST',f"/api/sessions/{session['sessionId']}/materials/image-order",data={'requestId':'order-fail','expectedStateRevision':self.version(session['sessionId']),'materialIds':[b['materialId'],a['materialId']]})
        self.assertEqual(response['status'],503);self.assertEqual(repo.get_conversation(context),before)

    def test_v7_upgrade_preserves_order_original_and_source_history(self):
        with db.database(':memory:') as connection:
            with patch.object(db,'MIGRATIONS',db.MIGRATIONS[:7]),patch.object(db,'SCHEMA_VERSION',7):
                db.initialize_database(connection)
                connection.execute("INSERT INTO sessions(session_id,direction) VALUES ('s','原方向')")
                connection.execute("INSERT INTO attachments(attachment_id,session_id,name,media_type,content_hash,original_data,original_order) VALUES ('m','s','p.png','image/png','h',x'010203',3)")
            db.initialize_database(connection)
            self.assertEqual(tuple(connection.execute('SELECT original_order,original_data,reading_order,recognition_report_json FROM attachments').fetchone()),(3,b'\x01\x02\x03',None,None))
            self.assertEqual(connection.execute('SELECT count(*) FROM recognition_results').fetchone()[0],0)


if __name__=='__main__': unittest.main()
