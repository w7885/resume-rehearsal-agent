"""Attachment orchestration contracts with scripted classification; no provider calls."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import db
from app.agent_material_intake import explicit_replacement
from app.repository import Repository, RequestContext
import test_agent_integration as integration

RESUME = '# 测试简历\n教育经历：测试大学\n项目经历：整理反馈，协助测试。'
NEW = '# 新版简历\n教育经历：测试大学\n项目经历：开发课程作业管理系统。'
JD = '# 招聘要求\nPython 后端实习生，岗位职责：开发接口；任职要求：熟悉 Flask。'
FINISH = {'action': 'finish', 'outcome': 'completed'}


class Classifier(integration.FakeClient):
    async def complete_json(self, *, system, user, run_id='test'):
        value = json.loads(user)
        if system.startswith('识别附件正文'):
            self.runtime.host.classify_inputs.append(value)
            output = self.runtime.host.classify_output
            items = output(value) if callable(output) else output
            return SimpleNamespace(data={'materials': items})
        if system.startswith('识别用户是否提供'):
            return SimpleNamespace(data={'status': 'none'})
        return await super().complete_json(system=system, user=user, run_id=run_id)

    async def decide(self, **kwargs):
        return SimpleNamespace(data=FINISH)


def classified(value, use='current', authorization=''):
    return [{'materialId': m['materialId'], 'role': 'jd' if '招聘要求' in m['text'] else 'resume',
             'quote': m['text'].splitlines()[0], 'use': use, 'authorizationQuote': authorization}
            for m in value['materials']]


class MaterialIntakeTests(unittest.TestCase):
    setUp = integration.AgentIntegrationTests.setUp
    request = integration.AgentIntegrationTests.request
    version = integration.AgentIntegrationTests.version
    material = integration.AgentIntegrationTests.material
    chat = integration.AgentIntegrationTests.chat
    send = integration.AgentIntegrationTests.send
    analyze = integration.AgentIntegrationTests.analyze
    conversation = integration.AgentIntegrationTests.conversation
    count = integration.AgentIntegrationTests.count

    def setup_case(self, *, existing=False, texts=(RESUME,)):
        session = self.chat()
        if existing:
            self.material(session, text=RESUME, role='resume', request_id='old')
        for i, text in enumerate(texts):
            self.material(session, text=text, role='unknown', request_id=f'new-{i}', name=f'附件{i}.md')
        host = self.app.state.agent_host
        host.client_factory = lambda runtime, budget: Classifier(runtime)
        host.classify_inputs = []
        host.classify_output = classified
        return session, host

    def turn(self, session, content='请帮我优化简历', key='first', **extra):
        user = self.send(session, content, request_id=key, **extra)
        events = self.analyze(session, user, key)
        self.assertNotIn(events[-1]['payload']['outcome'], {'agent_failed', 'invalid_input', 'scope_violation', 'stale_state', 'source_not_found'}, events)
        return user, events

    def test_positioned_docx_intake_finds_two_projects_and_keeps_non_experience_out(self):
        from urllib.parse import urlencode
        from app.resume_structure import block_kinds
        from test_document_parser import word, positioned_box, page_section
        session = self.chat()
        host = self.app.state.agent_host
        host.client_factory = lambda runtime, budget: Classifier(runtime)
        host.classify_inputs, host.classify_output = [], classified
        project = ('2025.10—2025.12  学生信息管理系统  开发成员\n使用Python编写查询功能。\n'
                   '2026.03—2026.04  个人博客网站  个人开发者\n实现文章发布。')
        raw = word('<w:p>' + positioned_box(project, 3000) + positioned_box('项目经历', 2000)
                   + positioned_box('教育背景\n测试大学', 1000) + positioned_box('技能证书\n了解Python', 4000)
                   + '</w:p>' + page_section())
        query = urlencode({'name': '坐标简历.docx', 'role': 'unknown', 'requestId': 'upload', 'expectedStateRevision': self.version(session['sessionId'])})
        response = self.request('POST', f"/api/sessions/{session['sessionId']}/materials?{query}", body=raw,
                                headers={'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'})
        self.assertEqual(response['status'], 201, response)
        self.assertFalse(response['json']['data']['draftCreated'])
        self.turn(session)
        value = self.conversation(session)
        blocks = value['draft']['blocks']
        kinds = block_kinds([b['markdown'] for b in blocks])
        projects = [b for b, kind in zip(blocks, kinds) if kind == 'project']
        self.assertEqual(len(projects), 2)
        self.assertIn('学生信息管理系统', projects[0]['markdown'])
        self.assertIn('个人博客网站', projects[1]['markdown'])
        self.assertNotIn('测试大学', ''.join(b['markdown'] for b in projects))
        self.assertNotIn('了解Python', ''.join(b['markdown'] for b in projects))
        repo = Repository(self.path)
        for block in blocks:
            for ref in block['sourceRefs']:
                source = repo.read_source_snapshot(RequestContext(session['sessionId']), ref['sourceId'], ref['sourceRevision'])
                self.assertIn(ref['quote'], source['source']['text'])
        material = value['materials'][0]
        self.assertEqual(material['parseStatus'], 'partial')
        self.assertEqual(repo.read_material_file(RequestContext(session['sessionId']), material['materialId'])['originalData'], raw)

    def test_clear_resume_and_jd_are_classified_from_text_without_type_question(self):
        session, host = self.setup_case(texts=(RESUME, JD))
        self.turn(session)
        value = self.conversation(session)
        self.assertEqual([m['role'] for m in value['materials']], ['resume', 'jd'])
        self.assertEqual(value['draft']['markdown'], RESUME)
        self.assertEqual(host.classify_inputs[0]['materials'][1]['text'], JD)
        self.assertFalse(any('分别是要修改的简历' in m['content'] for m in value['messages']))
        self.assertTrue(any('已识别' in m['content'] for m in value['messages']))
        self.assertNotIn(JD, value['draft']['markdown'])

    def test_ambiguous_attachment_stays_pending_until_answer(self):
        session, host = self.setup_case()
        host.classify_output = []
        self.turn(session)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], '')
        self.assertEqual(value['materials'][0]['useStatus'], 'pending')
        prompt = value['messages'][-1]
        host.classify_output = classified
        self.turn(session, '这是要修改的简历', 'answer', replyToMessageId=prompt['messageId'])
        self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)

    def test_existing_resume_without_replacement_intent_only_asks_use(self):
        session, host = self.setup_case(existing=True, texts=(NEW,))
        self.turn(session, '帮我看看附件')
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], RESUME)
        self.assertIn('替换当前稿，还是仅作补充参考', value['messages'][-1]['content'])
        self.assertEqual(self.count('draft_replacements'), 0)

    def test_explicit_replacement_preserves_history_and_replay_costs_nothing(self):
        session, host = self.setup_case(existing=True, texts=(NEW,))
        text = '用这份完整新版替换当前简历，同人同岗位。'
        host.classify_output = lambda v: classified(v, 'replace', text)
        user, _ = self.turn(session, text)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], NEW)
        self.assertEqual(value['session']['currentResumeGeneration'], 2)
        self.assertEqual(value['materials'][0]['useStatus'], 'history')
        self.assertEqual(self.count('draft_replacements'), 1)
        with db.database(self.path, read_only=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM source_blocks WHERE text=? AND role='unknown'", (NEW,)).fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM model_budget_reservations').fetchone()[0], 0)
        calls = len(host.classify_inputs)
        self.analyze(session, user, 'replay')
        self.assertEqual(len(host.classify_inputs), calls)
        self.assertEqual(self.count('draft_replacements'), 1)

    def test_replacement_answer_after_prompt_does_not_ask_again(self):
        session, host = self.setup_case(existing=True, texts=(NEW,))
        self.turn(session, '看看这份')
        prompt = self.conversation(session)['messages'][-1]
        text = '以这份完整新版为准，替换当前简历。'
        host.classify_output = lambda v: classified(v, 'replace', text)
        self.turn(session, text, 'answer', replyToMessageId=prompt['messageId'])
        self.assertEqual(self.conversation(session)['draft']['markdown'], NEW)
        self.assertEqual(self.count('draft_replacements'), 1)

    def test_model_cannot_authorize_replacement_without_user_quote(self):
        session, host = self.setup_case(existing=True, texts=(NEW,))
        host.classify_output = lambda v: classified(v, 'replace', '替换当前简历')
        self.turn(session, '请看看，不要替换当前简历')
        self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)
        self.assertEqual(self.count('draft_replacements'), 0)

    def test_foreign_id_and_duplicate_id_cannot_activate_anything(self):
        for mode in ('foreign', 'duplicate'):
            with self.subTest(mode=mode):
                session, host = self.setup_case() if mode == 'foreign' else (session, host)
                def bad(v):
                    items = classified(v)
                    if mode == 'foreign':
                        items[0]['materialId'] = 'foreign'
                    else:
                        items += items
                    return items
                host.classify_output = bad
                user = self.send(session, '处理附件', request_id=mode)
                events = self.analyze(session, user, mode)
                self.assertEqual(events[-1]['payload']['outcome'], 'scope_violation')
                self.assertEqual(self.conversation(session)['draft']['markdown'], '')

    def test_multiple_resumes_never_silently_choose_one(self):
        session, host = self.setup_case(texts=(RESUME, NEW))
        self.turn(session)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')
        self.assertEqual(self.count('draft_replacements'), 0)

    def test_new_jd_is_retained_with_explicit_replacement(self):
        session, host = self.setup_case(existing=True, texts=(NEW, JD))
        text = '用完整新版替换当前简历，同人同岗位。'
        host.classify_output = lambda v: classified(v, 'replace', text)
        self.turn(session, text)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], NEW)
        self.assertEqual(value['materials'][-1]['role'], 'jd')
        self.assertEqual(value['materials'][-1]['useStatus'], 'active')

    def test_failed_replacement_rolls_back_roles_and_current_draft(self):
        session, host = self.setup_case(existing=True, texts=(NEW,))
        text = '以完整新版为准替换当前简历。'
        host.classify_output = lambda v: classified(v, 'replace', text)
        with patch.object(Repository, 'replace_draft', side_effect=OSError('simulated disk error')):
            user = self.send(session, text)
            self.analyze(session, user)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], RESUME)
        self.assertEqual(value['materials'][-1]['role'], 'unknown')
        self.assertEqual(self.count('draft_replacements'), 0)

    def test_fabricated_classification_evidence_does_not_activate(self):
        session, host = self.setup_case()
        def fake(v):
            items = classified(v); items[0]['quote'] = '不存在的原句'; return items
        host.classify_output = fake
        self.turn(session)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')

    def test_reference_resume_never_becomes_personal_evidence_or_overwrites_draft(self):
        session, host = self.setup_case(existing=True, texts=(NEW,))
        host.classify_output = lambda v: classified(v, 'reference')
        self.turn(session, '这是一份他人的简历，仅供参考')
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], RESUME)
        self.assertEqual(value['materials'][-1]['role'], 'unknown')
        self.assertEqual(value['materials'][-1]['useStatus'], 'active')
        with db.database(self.path, read_only=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM source_blocks WHERE text=? AND use_status='active'", (NEW,)).fetchone()[0], 0)
        self.assertEqual(self.count('draft_replacements'), 0)

    def test_partial_extraction_cannot_replace_even_with_explicit_request(self):
        session, host = self.setup_case(existing=True, texts=(NEW,))
        with db.database(self.path) as c:
            c.execute("UPDATE attachments SET parse_status='partial' WHERE role='unknown'")
        text = '用完整新版替换当前简历'
        host.classify_output = lambda v: classified(v, 'replace', text)
        self.turn(session, text)
        self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)
        self.assertEqual(self.count('draft_replacements'), 0)

    def unreadable(self):
        with db.database(self.path) as c:
            mid = c.execute('SELECT attachment_id FROM attachments').fetchone()[0]
            c.execute('DELETE FROM source_blocks WHERE attachment_id=?', (mid,))
            c.execute("UPDATE attachments SET parse_status='failed' WHERE attachment_id=?", (mid,))

    def test_partial_document_with_text_is_classified_and_activated(self):
        session, host = self.setup_case()
        with db.database(self.path) as c:
            c.execute("UPDATE attachments SET parse_status='partial', media_type='application/pdf'")
        self.turn(session)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], RESUME)
        self.assertEqual(value['materials'][0]['role'], 'resume')
        self.assertEqual(value['materials'][0]['useStatus'], 'active')
        self.assertTrue(any('仅提取部分内容' in m['content'] for m in value['messages']))

    def test_classifier_quote_may_drop_icon_font_glyphs(self):
        text = ' 实习经历\n 13812345678\n 个人项目\n开发检索工具。'
        session, host = self.setup_case(texts=(text,))
        def real_model(v):
            items = classified(v)
            items[0]['quote'] = '实习经历 13812345678 个人项目'
            return items
        host.classify_output = real_model
        self.turn(session)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], text)
        self.assertEqual(value['materials'][0]['role'], 'resume')

    def test_no_text_attachment_gets_targeted_prompt_without_model_call(self):
        session, host = self.setup_case()
        self.unreadable()
        self.turn(session)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], '')
        self.assertEqual(host.classify_inputs, [])
        self.assertIn('没能从', value['messages'][-1]['content'])
        self.assertIn('附件0.md', value['messages'][-1]['content'])

    def test_user_can_name_the_role_of_an_unreadable_attachment(self):
        session, host = self.setup_case()
        self.unreadable()
        self.turn(session)
        prompt = self.conversation(session)['messages'][-1]
        reply = self.send(session, '这是待修改的简历', request_id='role', replyToMessageId=prompt['messageId'])
        self.analyze(session, reply, 'role')
        value = self.conversation(session)
        self.assertEqual(value['materials'][0]['role'], 'resume')
        self.assertEqual(value['materials'][0]['useStatus'], 'pending')
        self.assertEqual(value['draft']['markdown'], '')
        self.assertIn('仍没有可读正文', value['messages'][-1]['content'])
        self.assertEqual(host.classify_inputs, [])

    def test_user_role_resolves_partial_document_when_model_is_unsure(self):
        session, host = self.setup_case()
        host.classify_output = []
        with db.database(self.path) as c:
            c.execute("UPDATE attachments SET parse_status='partial', media_type='application/pdf'")
        self.turn(session)
        prompt = self.conversation(session)['messages'][-1]
        reply = self.send(session, '这是待修改的简历', request_id='role', replyToMessageId=prompt['messageId'])
        self.analyze(session, reply, 'role')
        self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)

    def test_mixed_readable_and_unreadable_materials_activate_the_readable_one(self):
        session, host = self.setup_case(texts=(RESUME, JD))
        with db.database(self.path) as c:
            mid = c.execute("SELECT attachment_id FROM attachments WHERE name='附件1.md'").fetchone()[0]
            c.execute('DELETE FROM source_blocks WHERE attachment_id=?', (mid,))
            c.execute("UPDATE attachments SET parse_status='failed' WHERE attachment_id=?", (mid,))
        self.turn(session)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], RESUME)
        text = '\n'.join(m['content'] for m in value['messages'])
        self.assertIn('已识别', text)
        self.assertIn('附件1.md', text)
        self.assertIn('没有可读正文', text)

    def test_edit_during_classification_invalidates_late_output(self):
        session, host = self.setup_case()
        def concurrent_edit(v):
            repo = Repository(self.path)
            current = repo.get_session(RequestContext(session['sessionId']))
            repo.save_message(RequestContext(session['sessionId'], current['stateRevision']), '更新了要求')
            return classified(v)
        host.classify_output = concurrent_edit
        user = self.send(session, '处理附件')
        events = self.analyze(session, user)
        self.assertEqual(events[-1]['payload']['outcome'], 'stale_state')
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')

    def test_complete_docx_replacement_keeps_every_source_and_exact_evidence(self):
        session, host = self.setup_case(existing=True, texts=(NEW,))
        # Construct the already-parsed boundary; parser correctness is tested separately.
        with db.database(self.path) as c:
            c.execute("UPDATE attachments SET media_type=?,parse_report_json=? WHERE role='unknown'", (
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                json.dumps({'status': 'parsed', 'warnings': []})))
            mid = c.execute("SELECT attachment_id FROM attachments WHERE role='unknown'").fetchone()[0]
        repo = Repository(self.path)
        current = repo.get_session(RequestContext(session['sessionId']))
        extra = '技能：Python、Flask。'
        repo.save_source(RequestContext(session['sessionId'], current['stateRevision']), text=extra,
                         role='unknown', use_status='pending', origin={'kind': 'material', 'materialId': mid, 'lineStart': 5, 'lineEnd': 5})
        text = '用完整新版替换当前简历'
        host.classify_output = lambda v: classified(v, 'replace', text)
        self.turn(session, text)
        draft = self.conversation(session)['draft']
        self.assertEqual(draft['markdown'], NEW + '\n\n' + extra)
        self.assertEqual(len(draft['blocks']), 4)
        repo = Repository(self.path)
        for block in draft['blocks']:
            for ref in block['sourceRefs']:
                source = repo.read_source_snapshot(RequestContext(session['sessionId']), ref['sourceId'], ref['sourceRevision'])
                self.assertIn(ref['quote'], source['source']['text'])


class AuthorizationTests(unittest.TestCase):
    def test_negative_conditional_question_and_quoted_requests_are_not_authority(self):
        for text in ('不要替换', '如果替换会怎样', '是否替换', '他说替换', '比如替换', '换岗位并替换', '替换？'):
            with self.subTest(text=text):
                self.assertFalse(explicit_replacement(text, text))
        self.assertTrue(explicit_replacement('用完整新版替换当前简历', '用完整新版替换当前简历'))


if __name__ == '__main__':
    unittest.main()
