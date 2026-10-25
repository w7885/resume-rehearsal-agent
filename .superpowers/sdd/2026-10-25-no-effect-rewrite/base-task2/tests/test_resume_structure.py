"""Lossless structure and scoped chat delivery with fixtures, never provider calls."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from app import db
from app.agent_edit_scope import project_blocks, resolve_scope
from app.agent_host import HostAdapter
from app.agent.ports import SourceRef
from app.repository import Repository, RepositoryError, RequestContext
from app.resume_structure import block_kinds, body_span, heading, split_resume, structure_blocks
from app.business_service import BusinessService
import test_agent_integration as integration
import test_agent_text_intake as intake
import test_assistant_replies

FIXTURE = Path(__file__).resolve().parents[2] / 'tests/datasets/示例1不同级别简历/计算机一般/计算机2.txt'
BODY = FIXTURE.read_text(encoding='utf-8-sig')
INSTRUCTION = '请只优化第二个项目，其他正文保持不变。'
MIXED = '下面是我的简历：\n' + BODY + '\n' + INSTRUCTION
OLD = '- 使用Flask实现课程作业发布、提交和状态查询。'
NEW = '- 使用Flask开发课程作业发布、提交与状态查询功能。'


class SegmentedClient(integration.FakeClient):
    async def complete_json(self, *, system, user, run_id='test'):
        value = json.loads(user)
        if system.startswith('识别用户是否提供'):
            match = next(m for m in value['messages'] if m['content'] == self.runtime.host.test_message)
            lines = [line for line in BODY.splitlines() if line.strip()]
            data = {'status': 'candidate', 'messageId': match['messageId'], 'resumeStart': lines[0], 'resumeEnd': lines[-1]}
            data.update(self.runtime.host.test_boundary_override)
        elif system.startswith('判断用户是否确认'):
            data = {'status': 'confirm', 'messageId': value['candidates'][0]['messageId']}
        else:
            return await super().complete_json(system=system, user=user, run_id=run_id)
        return SimpleNamespace(data=data)

    async def decide(self, **kwargs):
        self.steps += 1
        user = kwargs['user']
        if not self.answered(user):
            return SimpleNamespace(data={'action': 'ask_user', 'message': '这段经历中，你本人负责哪部分？', 'slot': 'responsibility', 'related_source_ids': [], 'rationale': '先确认个人职责'})
        ctx = HostAdapter(self.runtime).get_session_context(self.runtime.sid)
        self.runtime.host.test_visible_blocks = [b.block_id for b in ctx.draft_blocks]
        block = next(b for b in ctx.draft_blocks if OLD in b.markdown)
        if '[effect]' not in user:
            if '这段经历上线或交付后' in user:
                return SimpleNamespace(data={'action': 'tool', 'tool': 'save_facts', 'args': {
                    'experience_id': block.block_id,
                    'facts': [{'slot': 'effect', 'statement': '老师录入时间从半小时缩短到五分钟'}]}, 'rationale': '记录实际效果'})
            return SimpleNamespace(data={'action': 'ask_user', 'message': '这段经历上线或交付后，带来了哪些实际变化？', 'slot': 'effect', 'related_source_ids': [], 'rationale': '确认实际效果'})
        if self.proposed:
            return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed'})
        self.proposed = True
        ref = block.source_refs[0]
        return SimpleNamespace(data={'action': 'tool', 'tool': 'propose_change', 'args': {
            'operation': 'replace', 'target': block.block_id, 'before': block.markdown,
            'after': block.markdown.replace(OLD, NEW), 'reason': '梳理项目职责表述',
            'source_refs': [{'source_id': ref.source_id, 'source_revision': ref.source_revision, 'quote': OLD}], 'gaps': []}})


class StructureUnitTests(unittest.TestCase):
    def test_plain_numbered_projects_and_all_other_text_are_lossless(self):
        parts = split_resume(BODY)
        self.assertEqual(''.join(parts), BODY)
        self.assertEqual(sum('项目经历1' in p for p in parts), 1)
        self.assertEqual(sum('项目经历2' in p for p in parts), 1)
        self.assertFalse(any('项目经历1' in p and '项目经历2' in p for p in parts))
        self.assertEqual(len(parts), 6)

    def test_markdown_crlf_unicode_and_outer_blank_lines_are_preserved(self):
        body = '\r\n# 简历\r\n## 教育背景\r\n测试大学\r\n## 项目一：平台\r\n- 动作甲\r\n## 项目二：系统\r\n- 动作乙\r\n## 专业技能\r\nPython\r\n\r\n'
        parts = split_resume(body)
        self.assertEqual(''.join(parts), body)
        self.assertEqual(body_span(body, '# 简历', 'Python'), (0, len(body)))

    def test_icon_glyph_and_kangxi_headings_split_and_classify(self):
        body = (' 实习经历\n字节跳动｜后端开发\n- 负责接口\n'
                ' 个⼈项⽬\n简历助手\n- 负责检索\n'
                ' 专业技能\nPython\n'
                ' 校园经历\n学生会\n- 负责活动\n')
        parts = split_resume(body)
        self.assertEqual(''.join(parts), body)
        self.assertEqual(len(parts), 4)
        self.assertEqual(heading(' 个⼈项⽬'), '个人项目')
        self.assertEqual(block_kinds(parts), ['work', 'project', None, 'project'])

    def test_dated_entries_inside_internship_section_are_separate(self):
        body = ('实习经历\n2024.06—2024.09｜字节跳动｜后端开发\n- 接口甲\n'
                '2023.06—2023.09｜腾讯｜后端开发\n- 服务乙\n')
        parts = split_resume(body)
        self.assertEqual(''.join(parts), body)
        self.assertEqual(len(parts), 3)
        self.assertFalse(any('接口甲' in p and '服务乙' in p for p in parts))
        self.assertEqual(block_kinds(parts), [None, 'work', 'work'])

    def test_project_dates_under_general_header_are_separate(self):
        body = '项目经历\n2025.01—2025.03｜项目甲｜开发\n- 动作甲\n2025.04—2025.06｜项目乙｜开发\n- 动作乙\n技能证书\nPython'
        parts = split_resume(body)
        self.assertEqual(''.join(parts), body)
        self.assertFalse(any('动作甲' in p and '动作乙' in p for p in parts))

    def test_space_separated_date_rows_inside_sections_are_separate(self):
        body = ('实习经历\n2025.10—2025.12    某公司    后端实习生\n- 接口甲\n'
                '2025.07—2025.08    另一公司    测试实习生\n- 用例乙\n')
        parts = split_resume(body)
        self.assertEqual(''.join(parts), body)
        self.assertEqual(len(parts), 3)
        self.assertFalse(any('接口甲' in p and '用例乙' in p for p in parts))
        self.assertEqual(block_kinds(parts), [None, 'work', 'work'])

    def test_space_separated_date_project_rows_and_titles(self):
        from app.agent.memory import _block_title
        body = ('项目经历\n2024年7月 - 至今    学生信息管理系统    开发成员\n- 动作甲\n'
                '2025.07—2025.08    个人博客网站    个人开发者\n- 动作乙\n技能证书\nPython')
        parts = split_resume(body)
        self.assertEqual(''.join(parts), body)
        self.assertFalse(any('动作甲' in p and '动作乙' in p for p in parts))
        self.assertEqual(block_kinds(parts), [None, 'project', 'project', None])
        self.assertEqual(_block_title(parts[1]), '学生信息管理系统 开发成员')
        self.assertEqual(_block_title(parts[2]), '个人博客网站 个人开发者')

    def test_duplicate_or_rewritten_boundary_cannot_select_a_body(self):
        for content, a, b in [('重复\n正文\n重复', '重复', '重复'), (BODY, '模型改写的开头', BODY.splitlines()[-1])]:
            with self.subTest(a=a), self.assertRaises(RepositoryError):
                body_span(content, a, b)

    def test_instruction_inside_body_is_rejected_instead_of_deleted(self):
        with self.assertRaises(RepositoryError):
            body_span('项目经历1\n开发接口\n请只优化第二个项目\n技能证书\nPython', '项目经历1', 'Python')

    def test_each_split_quote_is_an_exact_substring_of_the_original_source(self):
        result = structure_blocks([{'markdown': BODY, 'sourceRefs': [{'sourceId': 'src', 'sourceRevision': 4, 'quote': BODY}]}])
        self.assertEqual(''.join(b['markdown'] for b in result), BODY)
        for block in result:
            self.assertEqual(block['sourceRefs'][0]['quote'], block['markdown'])
            self.assertIn(block['sourceRefs'][0]['quote'], BODY)


class ScopedChatTests(unittest.TestCase):
    setUp = integration.AgentIntegrationTests.setUp
    request = integration.AgentIntegrationTests.request
    version = integration.AgentIntegrationTests.version
    material = integration.AgentIntegrationTests.material
    chat = integration.AgentIntegrationTests.chat
    send = integration.AgentIntegrationTests.send
    analyze = integration.AgentIntegrationTests.analyze
    conversation = integration.AgentIntegrationTests.conversation
    count = integration.AgentIntegrationTests.count
    answer_goal = integration.AgentIntegrationTests.answer_goal
    run_context = test_assistant_replies.AssistantReplyTests.run_context

    def prepared(self):
        session = self.chat()
        host = self.app.state.agent_host
        host.client_factory = lambda runtime, budget: SegmentedClient(runtime)
        host.test_message = MIXED
        host.test_boundary_override = {}
        user = self.send(session, MIXED)
        self.analyze(session, user)
        return session, host

    def imported(self):
        session, host = self.prepared()
        prompt = self.conversation(session)['messages'][-1]
        user = self.send(session, '将识别出的正文作为本次简历。', request_id='confirm', replyToMessageId=prompt['messageId'])
        events = self.analyze(session, user, 'confirm')
        self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user', events)
        return session, host

    def test_material_upload_segments_only_experience_and_keeps_bytes(self):
        session = self.chat()
        self.material(session, text=BODY, role='resume')
        draft = self.conversation(session)['draft']
        self.assertEqual(draft['markdown'], BODY)
        self.assertEqual(len(draft['blocks']), 6)
        self.assertEqual(block_kinds([b['markdown'] for b in draft['blocks']]),
                         [None, None, 'project', 'project', None, None])

    def test_chat_instruction_stays_in_message_and_out_of_draft_and_evidence(self):
        session, host = self.imported()
        value = self.conversation(session)
        self.assertEqual(value['messages'][0]['content'], MIXED)
        self.assertEqual(value['draft']['markdown'], BODY)
        self.assertEqual(len(value['draft']['blocks']), 6)
        self.assertNotIn(INSTRUCTION, value['draft']['markdown'])
        with db.database(self.path, read_only=True) as c:
            source = c.execute('SELECT text,origin_json FROM source_blocks').fetchone()
            self.assertEqual(source['text'], BODY)
            self.assertEqual(json.loads(source['origin_json'])['lineStart'], 2)

    def test_only_project_two_changes_after_explicit_accept_with_complete_export(self):
        session, host = self.imported()
        before = self.conversation(session)['draft']
        self.answer_goal(session)
        value = self.conversation(session)
        self.assertEqual(value['draft'], before)  # A suggestion is not a committed edit.
        repo = Repository(self.path)
        projects = project_blocks(repo, RequestContext(session['sessionId']))
        self.assertEqual(len(projects), 2)
        self.assertEqual(host.test_visible_blocks, [projects[1]['blockId']])
        prompt = value['messages'][-1]
        self.assertEqual(prompt['assistant']['kind'], 'change_question', value)
        answer = self.send(session, '采用这条改写。', request_id='accept', replyToMessageId=prompt['messageId'])
        events = self.analyze(session, answer, 'accept')
        self.assertEqual(events[-1]['payload']['outcome'], 'completed', events)
        after = self.conversation(session)['draft']
        self.assertEqual(after['markdown'], BODY.replace(OLD, NEW))
        self.assertEqual(len(after['blocks']), len(before['blocks']))
        for old, new in zip(before['blocks'], after['blocks']):
            self.assertEqual(old['blockId'], new['blockId'])
            if old['blockId'] != projects[1]['blockId']:
                self.assertEqual(old, new)
        ref = after['blocks'][3]['sourceRefs'][0]
        source = repo.read_source_snapshot(RequestContext(session['sessionId']), ref['sourceId'], ref['sourceRevision'])
        self.assertEqual(ref['sourceRevision'], 1)
        self.assertIn(ref['quote'], source['source']['text'])
        self.assertNotIn(INSTRUCTION, source['source']['text'])
        # A fresh repository reads the exact complete export payload and stable identifiers.
        self.assertEqual(Repository(self.path).get_draft(RequestContext(session['sessionId'])), after)
        self.analyze(session, answer, 'accept-replay')
        self.assertEqual(self.count('suggestion_decisions'), 1)
        self.assertEqual(self.count('model_budget_reservations'), 0)

    def test_wrong_target_and_other_project_quote_are_rejected_before_save(self):
        session, _ = self.imported()
        context = self.run_context(session)
        repo = Repository(self.path)
        adapter = HostAdapter(SimpleNamespace(repo=repo, context=context, sid=session['sessionId']))
        projects = project_blocks(repo, context)
        for target, quote in [(projects[0], '使用MySQL保存用户信息'), (projects[1], '使用MySQL保存用户信息')]:
            ref = target['sourceRefs'][0]
            with self.subTest(target=target['blockId']), self.assertRaises(RepositoryError) as caught:
                adapter.save_suggestion(session['sessionId'], target['blockId'], 'replace', target['markdown'], target['markdown']+'调整', '测试', [SourceRef(source_id=ref['sourceId'], source_revision=ref['sourceRevision'], quote=quote)], [])
            self.assertEqual(caught.exception.code, 'scope_violation')
        self.assertEqual(self.count('suggestions'), 0)

    def test_invalid_or_missing_body_boundaries_never_import(self):
        session = self.chat()
        host = self.app.state.agent_host
        host.client_factory = lambda runtime, budget: SegmentedClient(runtime)
        host.test_message, host.test_boundary_override = MIXED, {'resumeEnd': '不存在的原文'}
        user = self.send(session, MIXED)
        self.analyze(session, user)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')
        self.assertEqual(self.count('source_blocks'), 0)
        self.assertIn('分开发送', self.conversation(session)['messages'][-1]['content'])

    def test_scope_cannot_treat_a_legacy_combined_block_as_a_single_project(self):
        session = self.chat()
        # Simulate a historical single-block import; no migration silently rewrites it.
        repo = Repository(self.path)
        user = self.send(session, BODY, request_id='legacy-body')
        source = repo.save_source(RequestContext(session['sessionId']), text=BODY, role='resume',
                                  origin={'kind': 'message', 'messageId': user['messageId']}, use_status='active')
        repo.create_initial_draft(RequestContext(session['sessionId']), [{
            'markdown': BODY,
            'sourceRefs': [{'sourceId': source['sourceId'], 'sourceRevision': 1, 'quote': BODY}]}])
        self.assertEqual(len(repo.get_draft(RequestContext(session['sessionId']))['blocks']), 1)
        self.send(session, INSTRUCTION, request_id='legacy-instruction')
        self.assertEqual(resolve_scope(repo, RequestContext(session['sessionId'])), set())

    def test_acceptance_rechecks_scope_even_if_a_bad_suggestion_was_saved_elsewhere(self):
        session, _ = self.imported()
        repo = Repository(self.path)
        projects = project_blocks(repo, RequestContext(session['sessionId']))
        trigger = self.send(session, INSTRUCTION, request_id='bad-proposal-trigger')
        context = self.run_context(session)
        block = projects[0]; ref = block['sourceRefs'][0]
        proposal = repo.save_suggestion(context, {'operation': 'replace',
            'target': {'blockId': block['blockId'], 'expectedBlockRevision': block['blockRevision']},
            'before': block['markdown'], 'after': block['markdown']+'不应写入', 'reason': '构造越界测试', 'gaps': [], 'sourceRefs': [ref]})
        service = BusinessService(repo)
        prompt = service.save_assistant_message(context, {'requestId': 'bad-prompt', 'userMessageId': trigger['messageId'],
            'content': '构造的越界建议', 'kind': 'change_question', 'suggestionIds': [proposal['suggestionId']]}, synthetic=True)['data']['message']
        answer = self.send(session, '采用这条。', request_id='bad-accept', replyToMessageId=prompt['messageId'])
        context = self.run_context(session)
        before = self.conversation(session)['draft']
        with self.assertRaises(RepositoryError) as caught:
            service.apply_resolved_user_decision(context, {'requestId': 'bad-accept-resolution', 'userMessageId': answer['messageId'],
                'userContent': answer['content'], 'status': 'resolved', 'decisions': [{'promptMessageId': prompt['messageId'],
                'suggestionId': proposal['suggestionId'], 'decision': 'accept', 'feedback': ''}]}, synthetic=True)
        self.assertEqual(caught.exception.code, 'scope_violation')
        self.assertEqual(self.conversation(session)['draft'], before)
        self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_tampered_body_scope_cannot_import(self):
        session, _ = self.prepared()
        with db.database(self.path) as c:
            row = c.execute('SELECT prompt_message_id,materials_json FROM agent_material_prompts').fetchone()
            values = json.loads(row['materials_json']); values[0]['bodyEnd'] += 1
            c.execute('UPDATE agent_material_prompts SET materials_json=? WHERE prompt_message_id=?', (json.dumps(values), row['prompt_message_id']))
        prompt = self.conversation(session)['messages'][-1]
        user = self.send(session, '确认正文。', request_id='tampered', replyToMessageId=prompt['messageId'])
        self.analyze(session, user, 'tampered')
        self.assertEqual(self.count('source_blocks'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '')

    def test_positive_target_with_negative_other_clause_still_limits_project_two(self):
        session, _ = self.imported()
        repo = Repository(self.path)
        projects = project_blocks(repo, RequestContext(session['sessionId']))
        self.send(session, '请只修改第二个项目，不要动其他内容。', request_id='negative-other')
        self.assertEqual(resolve_scope(repo, RequestContext(session['sessionId'])), {projects[1]['blockId']})

    def test_exact_project_name_can_change_the_previous_project_limit(self):
        session, _ = self.imported()
        repo = Repository(self.path)
        projects = project_blocks(repo, RequestContext(session['sessionId']))
        self.send(session, '请只优化校园失物招领平台。', request_id='named-first')
        self.assertEqual(resolve_scope(repo, RequestContext(session['sessionId'])), {projects[0]['blockId']})

    def test_new_explicit_project_request_replaces_prior_scope_after_many_turns(self):
        session, _ = self.imported()
        repo = Repository(self.path)
        sid = session['sessionId']
        projects = project_blocks(repo, RequestContext(sid))
        for i in range(52):
            current = repo.get_session(RequestContext(sid))
            repo.save_message(RequestContext(sid, current['stateRevision']), f'普通补充回合 {i}')
        self.assertEqual(resolve_scope(Repository(self.path), RequestContext(sid)), {projects[1]['blockId']})
        self.send(session, '接下来改第一个项目。', request_id='next-first')
        self.assertEqual(resolve_scope(repo, RequestContext(sid)), {projects[0]['blockId']})

    def test_legacy_pending_prompt_is_redetected_without_importing_unscoped_text(self):
        session, _ = self.prepared()
        with db.database(self.path) as c:
            row = c.execute('SELECT prompt_message_id,materials_json FROM agent_material_prompts').fetchone()
            values = json.loads(row['materials_json'])
            for field in ('bodyStart', 'bodyEnd', 'bodyHash'):
                values[0].pop(field)
            c.execute('UPDATE agent_material_prompts SET materials_json=? WHERE prompt_message_id=?', (json.dumps(values), row['prompt_message_id']))
        prompt = self.conversation(session)['messages'][-1]
        user = self.send(session, '确认正文。', request_id='legacy-confirm', replyToMessageId=prompt['messageId'])
        events = self.analyze(session, user, 'legacy-confirm')
        self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user')
        self.assertEqual(self.count('source_blocks'), 0)
        self.assertIn('识别的正文从', self.conversation(session)['messages'][-1]['content'])

    def test_scoped_unknown_project_asks_without_model_rewrite(self):
        session, host = self.imported()
        user = self.send(session, '请只优化第三个项目', request_id='missing-project')
        events = self.analyze(session, user, 'missing-project')
        self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user', events)
        self.assertIn('无法唯一定位', self.conversation(session)['messages'][-1]['content'])
        self.assertEqual(self.count('suggestions'), 0)

    def test_uploaded_markdown_uses_the_same_project_boundaries(self):
        session = self.chat()
        self.material(session, text=BODY, role='unknown')
        host = self.app.state.agent_host
        from test_agent_material_intake import Classifier, classified
        host.client_factory = lambda runtime, budget: Classifier(runtime)
        host.classify_inputs, host.classify_output = [], classified
        user = self.send(session, INSTRUCTION)
        self.analyze(session, user)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], BODY)
        self.assertEqual(len(value['draft']['blocks']), 6)
        self.assertEqual(len(project_blocks(Repository(self.path), RequestContext(session['sessionId']))), 2)

    def test_reference_correct_source_version_remains_exact_after_reload(self):
        session, _ = self.imported()
        value = self.conversation(session)
        repo = Repository(self.path)
        for block in value['draft']['blocks']:
            for ref in block['sourceRefs']:
                response = self.request('GET', f"/api/sessions/{session['sessionId']}/sources/{ref['sourceId']}/revisions/{ref['sourceRevision']}")
                self.assertEqual(response['status'], 200)
                source = response['json']['data']['source']
                self.assertEqual(source['sourceRevision'], ref['sourceRevision'])
                self.assertIn(ref['quote'], source['text'])
        self.assertEqual(repo.get_draft(RequestContext(session['sessionId'])), value['draft'])


if __name__ == '__main__':
    unittest.main()
