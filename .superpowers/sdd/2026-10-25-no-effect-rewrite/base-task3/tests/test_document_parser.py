"""Synthetic parsing fixtures and end-to-end persistence, never personal files."""
from io import BytesIO
import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import zipfile

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import db
from app.document_parser import extract_document, parse_upload
from app.repository import Repository, RequestContext
import test_api

DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def word(body, extra=None):
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{NS}"><w:body>{body}</w:body></w:document>')
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return stream.getvalue()


def paragraph(text):
    return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'


def pdf(pages, encrypted=False):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=595, height=842)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            content = DecodedStreamObject()
            values = list(text) if isinstance(text, tuple) else [text]
            commands = []
            for index, value in enumerate(values):
                escaped = value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
                commands.append(f'BT /F1 12 Tf {50 + index * 290} 780 Td ({escaped}) Tj ET')
            content.set_data('\n'.join(commands).encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(content)
    if encrypted:
        writer.encrypt('synthetic-test-only')
    stream = BytesIO(); writer.write(stream)
    return stream.getvalue()


class ParserTests(unittest.TestCase):
    def test_word_fragmented_runs_table_and_paragraph_order(self):
        raw = word('<w:p><w:r><w:t>协助</w:t></w:r><w:r><w:t>接口测试 2025.06</w:t></w:r></w:p>'
                   '<w:tbl><w:tr><w:tc>' + paragraph('8 条反馈') + '</w:tc><w:tc>' + paragraph('5 项需求') + '</w:tc></w:tr></w:tbl>' + paragraph('无上线收益'))
        value = extract_document(DOCX, raw)
        self.assertEqual(value['status'], 'parsed')
        self.assertEqual([b['text'] for b in value['blocks']], ['协助接口测试 2025.06', '8 条反馈', '5 项需求', '无上线收益'])
        self.assertEqual(value['blocks'][1]['origin']['table'], 1)
        self.assertEqual(value['blocks'][2]['origin']['column'], 2)
        self.assertEqual([b['origin']['paragraph'] for b in value['blocks']], [1, 2, 3, 4])

    def test_word_floating_text_images_and_revisions_are_not_silently_complete(self):
        raw = word(paragraph('正文') + '<w:p><w:ins><w:r><w:t>插入内容</w:t></w:r></w:ins><w:del><w:r><w:delText>删除内容</w:delText></w:r></w:del></w:p>'
                   '<w:p><w:r><w:drawing/><w:pict><w:txbxContent>' + paragraph('浮动文字') + '</w:txbxContent></w:pict></w:r></w:p>')
        value = extract_document(DOCX, raw)
        self.assertEqual(value['status'], 'partial')
        text = str([b['text'] for b in value['blocks']])
        self.assertIn('插入内容', text); self.assertNotIn('删除内容', text); self.assertNotIn('浮动文字', text)
        self.assertTrue({'floating_text', 'images', 'revisions'} <= {w['code'] for w in value['report']['warnings']})

    def test_word_headers_are_retained_but_order_flagged_and_dtd_rejected(self):
        value = extract_document(DOCX, word(paragraph('正文'), {'word/header1.xml': f'<w:hdr xmlns:w="{NS}">{paragraph("联系方式")}</w:hdr>'}))
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(value['blocks'][-1]['text'], '联系方式')
        malicious = BytesIO()
        with zipfile.ZipFile(malicious, 'w') as archive:
            archive.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY x "展开">]><x>&x;</x>')
        self.assertEqual(extract_document(DOCX, malicious.getvalue())['status'], 'failed')

    def test_pdf_page_order_and_mixed_blank_page_are_explicit(self):
        value = extract_document('application/pdf', pdf(['Project A 2025.06 8 feedback', None, 'Project B assisted testing 5 requirements']))
        self.assertEqual(value['status'], 'partial')
        self.assertEqual([b['origin']['page'] for b in value['blocks']], [1, 3])
        self.assertEqual(value['report']['pageCount'], 3)
        self.assertIn('第 2 页', str(value['report']['warnings']))

    def test_same_synthetic_facts_survive_md_word_and_text_pdf(self):
        text = 'Assisted API testing 2025.06; 8 feedback; 5 requirements; no launch revenue'
        for value in [parse_upload('text/markdown', text.encode(), text), extract_document(DOCX, word(paragraph(text))), extract_document('application/pdf', pdf([text]))]:
            self.assertEqual(value['status'], 'parsed')
            self.assertEqual(''.join(b['text'] for b in value['blocks']).strip(), text)

    def test_pdf_encrypted_corrupt_and_empty_are_not_success(self):
        encrypted = extract_document('application/pdf', pdf(['hidden'], encrypted=True))
        self.assertEqual(encrypted['status'], 'failed')
        self.assertEqual(encrypted['report']['warnings'][0]['code'], 'encrypted')
        for raw in (b'%PDF-synthetic', pdf([None])):
            self.assertEqual(extract_document('application/pdf', raw)['status'], 'failed')

    def test_pdf_physical_columns_are_flagged_instead_of_claiming_complete_order(self):
        value = extract_document('application/pdf', pdf([('Project A 2025.06', 'Project B assisted 8')]))
        self.assertEqual(value['status'], 'partial')
        self.assertIn('reading_order', [item['code'] for item in value['report']['warnings']])
        self.assertIn('Project A', value['blocks'][0]['text']); self.assertIn('Project B', value['blocks'][0]['text'])

    def test_limits_and_worker_timeout_return_honest_partial_or_failed(self):
        with patch('app.document_parser.MAX_PAGES', 1):
            value = extract_document('application/pdf', pdf(['first', 'second']))
        self.assertEqual(value['status'], 'partial'); self.assertEqual(len(value['blocks']), 1)
        with patch('app.document_parser.MAX_TEXT', 5):
            value = extract_document(DOCX, word(paragraph('short') + paragraph('too long')))
        self.assertEqual(value['status'], 'partial'); self.assertEqual(value['blocks'][0]['text'], 'short')
        with patch('app.document_parser.subprocess.run', side_effect=subprocess.TimeoutExpired('synthetic', 6)):
            value = parse_upload('application/pdf', b'%PDF-synthetic')
        self.assertEqual(value['status'], 'failed'); self.assertEqual(value['report']['warnings'][0]['code'], 'parser_limit')


class ParsingApiTests(unittest.TestCase):
    setUp = test_api.ApiTests.setUp
    request = test_api.ApiTests.request
    new_session = test_api.ApiTests.new_session
    version = test_api.ApiTests.version
    count = test_api.ApiTests.count
    assert_error = test_api.ApiTests.assert_error

    def upload(self, session, raw, name, media_type, role='unknown', request_id='parse-upload'):
        query = urlencode({'name': name, 'role': role, 'requestId': request_id, 'expectedStateRevision': self.version(session['sessionId'])})
        return self.request('POST', f"/api/sessions/{session['sessionId']}/materials?{query}", body=raw, headers={'content-type': media_type})

    def detail(self, session, material):
        return self.request('GET', f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}")['json']['data']

    def test_unknown_word_extracts_in_order_without_fact_promotion_or_draft(self):
        session = self.new_session()
        raw = word(paragraph('协助接口测试') + paragraph('2025.06 / 8 条反馈 / 5 项需求'))
        response = self.upload(session, raw, '构造.docx', DOCX)
        self.assertEqual(response['status'], 201, response)
        value = response['json']['data']; material = value['material']
        self.assertEqual(material['parseStatus'], 'parsed'); self.assertEqual(material['useStatus'], 'pending')
        self.assertFalse(value['draftCreated'])
        restored = self.detail(session, material)
        self.assertEqual([s['text'] for s in restored['sources']], ['协助接口测试', '2025.06 / 8 条反馈 / 5 项需求'])
        self.assertEqual(restored['material']['parseReport'], material['parseReport'])
        repo = Repository(self.path)
        self.assertEqual(repo.get_session_context(RequestContext(session['sessionId']))['sources'], [])
        download = self.request('GET', f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/file")
        self.assertEqual(download['body'], raw)

    def test_partial_pdf_preserves_original_page_numbers_and_stays_pending(self):
        session = self.new_session()
        value = self.upload(session, pdf(['first 2025.06', None, 'third assisted 8']), '构造.pdf', 'application/pdf', role='resume')['json']['data']
        self.assertEqual(value['material']['parseStatus'], 'partial')
        self.assertEqual(value['material']['useStatus'], 'pending')
        self.assertFalse(value['draftCreated'])
        self.assertEqual([s['origin']['page'] for s in self.detail(session, value['material'])['sources']], [1, 3])

    def test_failed_pdf_retains_original_and_retry_does_not_duplicate(self):
        session = self.new_session()
        raw = pdf(['hidden'], encrypted=True)
        rev = self.version(session['sessionId'])
        query = urlencode({'name':'encrypted.pdf','requestId':'same','expectedStateRevision':rev})
        url = f"/api/sessions/{session['sessionId']}/materials?{query}"
        first = self.request('POST', url, body=raw, headers={'content-type':'application/pdf'})
        second = self.request('POST', url, body=raw, headers={'content-type':'application/pdf'})
        self.assertEqual(first['json'], second['json'])
        self.assertEqual(first['json']['data']['material']['parseStatus'], 'failed')
        self.assertEqual(self.count('attachments'), 1); self.assertEqual(self.count('source_blocks'), 0)

    def test_correction_new_version_replay_original_bytes_and_exact_old_reference(self):
        session = self.new_session()
        raw = word(paragraph('负责接口开发'))
        value = self.upload(session, raw, 'correction.docx', DOCX, role='resume')['json']['data']
        material, source = value['material'], value['sources'][0]
        repo = Repository(self.path); context = RequestContext(session['sessionId'])
        draft_before = repo.get_conversation(context)['draft']
        payload = {'requestId':'correct','expectedStateRevision':self.version(session['sessionId']),'expectedSourceRevision':1,'text':'协助接口测试'}
        url = f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/sources/{source['sourceId']}/revisions"
        first = self.request('POST', url, data=payload)
        self.assertEqual(first['status'], 200, first)
        self.assertEqual(self.request('POST', url, data=payload)['json'], first['json'])
        self.assertEqual(first['json']['data']['source']['sourceRevision'], 2)
        after = repo.get_conversation(context)
        self.assertEqual(after['draft'], draft_before); self.assertTrue(after['draftReviewRequired'])
        self.assertEqual(repo.read_source_snapshot(context, source['sourceId'], 1)['source']['text'], '负责接口开发')
        self.assertEqual(repo.read_material_file(context, material['materialId'])['originalData'], raw)
        self.assertEqual([s['sourceRevision'] for s in self.detail(session, material)['sources']], [1, 2])
        wrong = self.new_session()
        self.assert_error(self.request('POST', url.replace(session['sessionId'], wrong['sessionId']), data={**payload,'requestId':'foreign','expectedStateRevision':1}), 403, 'scope_violation')
        self.assert_error(self.request('POST', url, data={**payload,'requestId':'stale','expectedStateRevision':self.version(session['sessionId'])}), 409, 'stale_state')

    def test_source_failure_rolls_back_original_report_and_partial_blocks(self):
        session = self.new_session()
        with patch.object(Repository, 'save_source', side_effect=OSError('synthetic storage failure')):
            response = self.upload(session, word(paragraph('正文')), 'rollback.docx', DOCX)
        self.assertEqual(response['status'], 503, response)
        self.assertEqual(self.count('attachments'), 0); self.assertEqual(self.count('source_blocks'), 0)

    def test_unknown_correction_does_not_promote_facts_and_failure_rolls_back_new_snapshot(self):
        session = self.new_session()
        value = self.upload(session, word(paragraph('构造原文')), 'unknown.docx', DOCX)['json']['data']
        material, source = value['material'], value['sources'][0]
        url = f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/sources/{source['sourceId']}/revisions"
        payload = {'requestId':'correction-fail','expectedStateRevision':self.version(session['sessionId']),'expectedSourceRevision':1,'text':'构造修正'}
        real = Repository.save_source
        def fail_after_write(repo, *args, **kwargs):
            real(repo, *args, **kwargs)
            raise OSError('synthetic correction failure')
        with patch.object(Repository, 'save_source', fail_after_write):
            self.assertEqual(self.request('POST', url, data=payload)['status'], 503)
        self.assertEqual(len(self.detail(session, material)['sources']), 1)
        self.assertEqual(self.request('POST', url, data=payload)['status'], 200)
        repo = Repository(self.path); context = RequestContext(session['sessionId'])
        self.assertEqual(repo.get_session_context(context)['sources'], [])
        self.assertEqual(repo.get_conversation(context)['draft']['markdown'], '')
        self.assertEqual(self.detail(session, material)['material']['useStatus'], 'pending')

    def test_v5_migration_preserves_originals_and_marks_reports_unknown(self):
        with db.database(':memory:') as connection:
            with patch.object(db, 'MIGRATIONS', db.MIGRATIONS[:5]), patch.object(db, 'SCHEMA_VERSION', 5):
                db.initialize_database(connection)
                connection.execute("INSERT INTO sessions(session_id,direction) VALUES ('old','原方向')")
                connection.execute("INSERT INTO attachments(attachment_id,session_id,name,media_type,content_hash,original_data,original_order) VALUES ('m','old','old.pdf','application/pdf','hash',x'010203',0)")
            db.initialize_database(connection)
            row = connection.execute('SELECT * FROM attachments').fetchone()
            self.assertEqual(row['original_data'], b'\x01\x02\x03'); self.assertIsNone(row['parse_report_json'])


if __name__ == '__main__':
    unittest.main()
