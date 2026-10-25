"""A-T10.1 synthetic local raster, ownership, caching and recovery checks."""
from io import BytesIO
import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from PIL import Image
from app import db
from app.page_renderer import MAX_EDGE, RENDER_VERSION, failure, prepare_page, render_page
from app.repository import Repository, RepositoryError, RequestContext
import test_api
from test_document_parser import pdf


def picture(size=(80, 40), kind='PNG', orientation=None):
    output = BytesIO()
    with Image.new('RGB', size, 'navy') as image:
        if orientation:
            exif = Image.Exif(); exif[274] = orientation
            image.save(output, format=kind, exif=exif)
        else:
            image.save(output, format=kind)
    return output.getvalue()


class RendererTests(unittest.TestCase):
    def test_scanned_pdf_renders_an_image_without_creating_recognized_text(self):
        output = BytesIO()
        with Image.new('RGB', (120, 160), 'white') as first, Image.new('RGB', (120, 160), 'navy') as second:
            first.save(output, format='PDF', save_all=True, append_images=[second])
        value = render_page('application/pdf', output.getvalue(), 2)
        self.assertEqual(value['status'], 'ready', value)
        self.assertEqual(value['pageCount'], 2)
        self.assertNotIn('text', value)

    def test_real_pdfium_renders_correct_page_and_caps_dimensions(self):
        raw = pdf(['FIRST synthetic', 'SECOND synthetic'])
        first = render_page('application/pdf', raw, 1)
        second = render_page('application/pdf', raw, 2)
        self.assertEqual(first['status'], 'ready', first)
        self.assertEqual(first['pageCount'], 2)
        self.assertNotEqual(first['image'], second['image'])
        self.assertLessEqual(max(first['width'], first['height']), MAX_EDGE)
        self.assertEqual(render_page('application/pdf', raw, 3)['errorCode'], 'invalid_page')

    def test_png_jpeg_preview_copy_is_bounded_and_exif_orientation_applied(self):
        raw = picture((2000, 1000))
        value = render_page('image/png', raw, 1)
        self.assertEqual(value['status'], 'ready', value)
        self.assertEqual((value['width'], value['height']), (1600, 800))
        rotated = render_page('image/jpeg', picture(kind='JPEG', orientation=6), 1)
        self.assertEqual((rotated['width'], rotated['height']), (40, 80))
        self.assertEqual(render_page('image/png', raw, 2)['errorCode'], 'invalid_page')

    def test_corrupt_encrypted_and_timeout_have_no_fake_image(self):
        for media_type, raw in [('image/png', b'\x89PNG\r\n\x1a\ncorrupt'), ('application/pdf', pdf(['secret'], encrypted=True)), ('application/pdf', b'%PDF-broken')]:
            value = render_page(media_type, raw, 1)
            self.assertEqual(value['status'], 'failed'); self.assertNotIn('image', value)
        with patch('app.page_renderer.subprocess.run', side_effect=subprocess.TimeoutExpired('synthetic', 6)):
            self.assertEqual(prepare_page('image/png', picture(), 1)['errorCode'], 'render_timeout')


class PageApiTests(unittest.TestCase):
    setUp = test_api.ApiTests.setUp
    request = test_api.ApiTests.request
    new_session = test_api.ApiTests.new_session
    version = test_api.ApiTests.version
    count = test_api.ApiTests.count
    assert_error = test_api.ApiTests.assert_error

    def upload(self, session, raw, name='synthetic.png', media_type='image/png'):
        query = urlencode({'name':name, 'requestId':'upload', 'expectedStateRevision':self.version(session['sessionId'])})
        response = self.request('POST', f"/api/sessions/{session['sessionId']}/materials?{query}", body=raw, headers={'content-type':media_type})
        self.assertEqual(response['status'], 201, response)
        return response['json']['data']['material']

    def base(self, session, material):
        return f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}"

    def prepare(self, session, material, page=1, request_id='prepare', revision=None):
        return self.request('POST', self.base(session, material)+'/pages/prepare', data={'requestId':request_id, 'expectedStateRevision':self.version(session['sessionId']) if revision is None else revision, 'pageNumber':page})

    def test_metadata_read_is_not_rendering_then_prepare_preserves_bytes_facts_and_versions(self):
        session = self.new_session(); raw = picture(); material = self.upload(session, raw)
        repo = Repository(self.path); context = RequestContext(session['sessionId'])
        before = repo.get_conversation(context)
        manifest = self.request('GET', self.base(session, material)+'/pages')['json']['data']
        self.assertEqual(manifest['pages'][0]['status'], 'pending'); self.assertIsNone(manifest['pageCount'])
        self.assertEqual(self.count('material_pages'), 0)
        response = self.prepare(session, material)
        self.assertEqual(response['status'], 200, response)
        self.assertFalse(response['json']['data']['recognitionAvailable'])
        self.assertEqual(response['json']['data']['pages'][0]['status'], 'ready')
        self.assertEqual(repo.get_conversation(context), before)
        self.assertEqual(repo.read_material_file(context, material['materialId'])['originalData'], raw)
        self.assertEqual(self.count('source_blocks'), 0); self.assertEqual(self.count('runs'), 0)
        self.assertEqual(material['parseStatus'], 'pending')
        image = self.request('GET', self.base(session, material)+'/pages/1/image')
        self.assertEqual(image['status'], 200); self.assertTrue(image['body'].startswith(b'\x89PNG'))
        self.assertEqual(image['headers'][b'cache-control'], b'no-store')

    def test_pdf_native_order_blank_page_and_ready_cache_survive_reopen(self):
        session = self.new_session(); material = self.upload(session, pdf(['one', None, 'three']), 'synthetic.pdf', 'application/pdf')
        for number in [3, 1, 2]:
            response = self.prepare(session, material, number, f'page-{number}')
            self.assertEqual(response['status'], 200, response)
        manifest = self.request('GET', self.base(session, material)+'/pages')['json']['data']
        self.assertEqual([p['page'] for p in manifest['pages']], [1, 2, 3])
        self.assertTrue(all(p['status'] == 'ready' for p in manifest['pages']))
        # Blank page is a valid image, not recognized text or a text parse success.
        self.assertFalse(manifest['recognitionAvailable'])
        with patch('app.page_service.prepare_page', side_effect=AssertionError('must reuse prepared page')):
            response = self.prepare(session, material, 3, 'independent-cache-hit')
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.count('material_pages'), 3)
        image = Repository(self.path).read_material_page(RequestContext(session['sessionId']), material['materialId'], 3)
        self.assertEqual(image['renderVersion'], RENDER_VERSION)

    def test_failed_page_retry_is_same_row_and_other_page_numbers_remain_available(self):
        session = self.new_session(); material = self.upload(session, pdf(['one', 'two']), 'synthetic.pdf', 'application/pdf')
        with patch('app.page_service.prepare_page', return_value=failure('render_timeout', '构造超时', 2)):
            first = self.prepare(session, material)
        self.assertEqual(first['json']['data']['pages'][0]['status'], 'failed')
        self.assertEqual([p['page'] for p in first['json']['data']['pages']], [1, 2])
        self.assert_error(self.request('GET', self.base(session, material)+'/pages/1/image'), 404, 'source_not_found')
        second = self.prepare(session, material, request_id='retry-failed')
        self.assertEqual(second['json']['data']['pages'][0]['status'], 'ready')
        self.assertEqual(self.count('material_pages'), 1)

    def test_replay_is_same_result_and_stale_new_operation_does_not_write(self):
        session = self.new_session(); material = self.upload(session, picture()); revision = self.version(session['sessionId'])
        first = self.prepare(session, material, revision=revision)
        self.request('POST', f"/api/sessions/{session['sessionId']}/messages", data={'requestId':'later-message','expectedStateRevision':revision,'content':'构造新上下文','kind':'skip'})
        second = self.prepare(session, material, revision=revision)
        self.assertEqual(first['json'], second['json'])
        self.assert_error(self.prepare(session, material, request_id='stale-new', revision=revision), 409, 'stale_state')
        self.assertEqual(self.count('material_pages'), 1)

    def test_foreign_missing_invalid_page_and_arbitrary_parameters_are_rejected(self):
        session = self.new_session(); material = self.upload(session, picture()); other = self.new_session()
        self.assert_error(self.request('GET', self.base(other, material)+'/pages'), 404, 'source_not_found')
        self.assert_error(self.prepare(other, material), 404, 'source_not_found')
        for page in (0, 2, 41):
            self.assert_error(self.prepare(session, material, page=page, request_id=f'bad-{page}'), 422, 'invalid_input')
        self.assert_error(self.request('POST', self.base(session, material)+'/pages/prepare', data={'requestId':'path','expectedStateRevision':self.version(session['sessionId']),'pageNumber':1,'path':'C:/other.pdf'}), 422, 'invalid_input')
        self.assertEqual(self.count('material_pages'), 0)

    def test_cache_hash_change_never_serves_prior_image(self):
        session = self.new_session(); material = self.upload(session, picture()); self.prepare(session, material)
        replacement = picture((50, 100))
        with db.database(self.path) as connection:
            connection.execute('UPDATE attachments SET original_data = ?, content_hash = ? WHERE attachment_id = ?', (replacement, hashlib.sha256(replacement).hexdigest(), material['materialId']))
        manifest = self.request('GET', self.base(session, material)+'/pages')['json']['data']
        self.assertIsNone(manifest['pageCount']); self.assertEqual(manifest['pages'][0]['status'], 'pending')
        self.assert_error(self.request('GET', self.base(session, material)+'/pages/1/image'), 404, 'source_not_found')
        self.assertEqual(self.prepare(session, material, request_id='new-hash')['json']['data']['pages'][0]['height'], 100)

    def test_cache_and_request_log_failure_roll_back_without_touching_original(self):
        session = self.new_session(); material = self.upload(session, picture()); real = __import__('app.business_service', fromlist=['json']).json.dumps
        def fail_cache(value, *args, **kwargs):
            if isinstance(value, dict) and value.get('ok') and isinstance(value.get('data'), dict) and 'renderVersion' in value['data']:
                raise OSError('synthetic request cache failure')
            return real(value, *args, **kwargs)
        with patch('app.business_service.json.dumps', side_effect=fail_cache):
            self.assertEqual(self.prepare(session, material)['status'], 503)
        self.assertEqual(self.count('material_pages'), 0)
        self.assertIsNone(self.request('GET', self.base(session, material)+'/pages')['json']['data']['pageCount'])
        self.assertEqual(self.count('attachments'), 1)

    def test_host_context_current_image_allowed_but_old_run_and_history_denied(self):
        session = self.new_session(); material = self.upload(session, picture()); self.prepare(session, material)
        sid = session['sessionId']; revision = self.version(sid)
        with db.database(self.path) as connection:
            connection.execute("INSERT INTO runs(run_id,session_id,generation,input_state_revision,resume_generation,status) VALUES ('host',?,1,?,1,'running')", (sid,revision))
        repo = Repository(self.path); context = RequestContext(sid,revision,'host',1)
        prepared = repo.read_material_page(context, material['materialId'], 1)
        self.assertTrue(prepared['imageData'])
        self.assertEqual(prepared['contentHash'], hashlib.sha256(picture()).hexdigest())
        self.assertEqual(prepared['materialId'], material['materialId'])
        with db.database(self.path) as connection:
            connection.execute("UPDATE attachments SET use_status = 'history' WHERE attachment_id = ?", (material['materialId'],))
        with self.assertRaises(RepositoryError) as error:
            repo.read_material_page(context, material['materialId'], 1)
        self.assertEqual(error.exception.code, 'stale_state')
        with db.database(self.path) as connection:
            connection.execute("UPDATE runs SET status = 'interrupted' WHERE run_id = 'host'")
        with self.assertRaises(RepositoryError) as error:
            repo.read_material_page(context, material['materialId'], 1)
        self.assertEqual(error.exception.code, 'cancelled')
        self.assertEqual(self.request('GET', self.base(session, material)+'/pages/1/image')['status'], 200)

    def test_session_delete_cascades_pages_and_old_retry_cannot_recreate(self):
        session = self.new_session(); material = self.upload(session, picture()); rev = self.version(session['sessionId']); self.prepare(session, material)
        self.request('DELETE', f"/api/sessions/{session['sessionId']}", data={'requestId':'delete','expectedStateRevision':rev,'confirmed':True})
        self.assertEqual(self.count('material_pages'), 0)
        self.assert_error(self.prepare(session, material, revision=rev), 404, 'session_not_found')

    def test_v6_upgrade_keeps_original_bytes_and_has_empty_page_cache(self):
        with db.database(':memory:') as connection:
            with patch.object(db, 'MIGRATIONS', db.MIGRATIONS[:6]), patch.object(db, 'SCHEMA_VERSION', 6):
                db.initialize_database(connection)
                connection.execute("INSERT INTO sessions(session_id,direction) VALUES ('s','构造')")
                connection.execute("INSERT INTO attachments(attachment_id,session_id,name,media_type,content_hash,original_data,original_order) VALUES ('m','s','p.png','image/png','h',x'010203',0)")
            db.initialize_database(connection)
            self.assertEqual(connection.execute('SELECT original_data,page_report_json FROM attachments').fetchone()[:], (b'\x01\x02\x03',None))
            self.assertEqual(connection.execute('SELECT count(*) FROM material_pages').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
