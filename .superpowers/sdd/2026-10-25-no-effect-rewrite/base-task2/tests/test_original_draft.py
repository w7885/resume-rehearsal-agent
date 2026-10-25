"""Initial imports are immutable comparison baselines for the current generation."""
import unittest
from app import db
from app.repository import Repository, RequestContext
import test_draft_actions as actions


class OriginalDraftTests(unittest.TestCase):
    setUp = actions.DraftActionsTests.setUp
    request = actions.DraftActionsTests.request
    new_session = actions.DraftActionsTests.new_session
    version = actions.DraftActionsTests.version
    material = actions.DraftActionsTests.material
    message = actions.DraftActionsTests.message
    setup_case = actions.DraftActionsTests.setup_case
    draft = actions.DraftActionsTests.draft
    proposal = actions.DraftActionsTests.proposal
    decision_payload = actions.DraftActionsTests.decision_payload
    decide = actions.DraftActionsTests.decide
    edit = actions.DraftActionsTests.edit
    replacement = actions.DraftActionsTests.replacement

    def original(self, sid=None):
        result = self.request('GET', f"/api/sessions/{sid or self.sid}/conversation")
        self.assertEqual(result['status'], 200, result)
        return result['json']['data']['originalDraft']

    def test_accepting_and_editing_never_overwrite_the_baseline(self):
        self.setup_case()
        first = self.original()
        suggestion = self.proposal()
        self.assertEqual(self.decide(suggestion, self.decision_payload(suggestion))['status'], 200)
        self.assertNotEqual(self.draft()['markdown'], first['markdown'])
        self.assertEqual(self.original(), first)
        self.assertEqual(self.edit('手工修改后的当前稿')['status'], 200)
        self.assertEqual(self.original(), first)

    def test_reordering_retained_import_rows_does_not_reorder_the_baseline(self):
        parts = ['# 教育背景\n学校甲\n\n', '# 项目经历\n参与测试\n\n', '# 技能\nPython\n']
        self.setup_case(parts)
        first = self.original()
        self.assertEqual(self.edit(''.join(reversed(parts)))['status'], 200)
        with db.database(self.path, read_only=True) as connection:
            positions = [r[0] for r in connection.execute("SELECT position FROM draft_blocks WHERE session_id=? AND write_kind='import' ORDER BY rowid", (self.sid,))]
        self.assertNotEqual(positions, [0, 1, 2])
        self.assertEqual(self.original(), first)

    def test_whole_replacement_uses_new_generation_baseline(self):
        session = self.setup_case()
        first = self.original()
        payload = self.replacement(session)
        result = self.request('POST', self.base + '/draft/replacement', data=payload)
        self.assertEqual(result['status'], 200, result)
        new = self.original()
        self.assertEqual(new['resumeGeneration'], first['resumeGeneration'] + 1)
        self.assertEqual(new['markdown'], result['json']['data']['draft']['markdown'])
        self.assertEqual(new['materialIds'], [payload['materialId']])
        self.assertEqual(self.edit('再次优化后的新版', requestId='edit-new')['status'], 200)
        self.assertEqual(self.original(), new)

    def test_source_correction_keeps_original_text_and_exact_reference_revision(self):
        session = self.new_session()
        body = '# 构造上传简历\n参与测试。\n'
        uploaded = self.material(session, text=body)['json']['data']
        self.sid = session['sessionId']
        first = self.original()
        self.assertEqual(first['markdown'], body)
        self.assertEqual(first['materialIds'], [uploaded['material']['materialId']])
        source = uploaded['sources'][0]
        repo = Repository(self.path)
        repo.save_source(RequestContext(self.sid), text='修正后的识别文字', role='resume', origin=source['origin'], use_status='active', source_id=source['sourceId'], expected_source_revision=1)
        self.assertEqual(self.original(), first)
        self.assertEqual(first['sourceRefs'][0]['sourceRevision'], 1)

    def test_manual_only_or_other_session_does_not_fabricate_an_initial_import(self):
        self.setup_case()
        other = self.new_session('另一个会话')
        self.assertIsNone(self.original(other['sessionId']))
        result = self.request('PUT', f"/api/sessions/{other['sessionId']}/draft", data={'requestId': 'manual', 'expectedStateRevision': self.version(other['sessionId']), 'markdown': '只有手工输入的当前稿'})
        self.assertEqual(result['status'], 200)
        self.assertIsNone(self.original(other['sessionId']))
        self.assertEqual(self.original()['sessionId'], self.sid)

    def test_reading_baseline_does_not_mutate_any_business_records(self):
        self.setup_case()
        with db.database(self.path, read_only=True) as connection:
            before = list(connection.iterdump())
        self.original()
        with db.database(self.path, read_only=True) as connection:
            self.assertEqual(list(connection.iterdump()), before)
