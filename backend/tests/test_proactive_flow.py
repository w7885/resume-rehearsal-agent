"""One-section chat continuation with isolated SQLite and scripted clients; no API spend."""
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import db
from app.agent.llm import LLMError
from app.agent.ports import RepoError, SourceRef
from app.agent_host import HostAdapter
from app.agent_budget import ProjectBudget
from app.main import create_app
from app.repository import RepositoryError, RequestContext
from app.resume_structure import block_kinds, structure_blocks
import test_agent_integration as integration

BODY = '项目经历1\n参与项目一测试。\n项目经历2\n参与项目二测试。\n项目经历3\n参与项目三测试。\n'


class ProactiveClient(integration.FakeClient):
    async def route(self, **kwargs):
        return {'action': 'answer', 'scope': 'experience', 'target_experience_id': None, 'confidence': 1}

    async def decide(self, **kwargs):
        self.steps += 1
        if self.steps > 1:
            return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed', 'summary': '建议已生成。'})
        block_id = re.search(r'段落编号（blockId）：(\S+)', kwargs['user'])[1]
        host = self.runtime.host
        host.phases.append(block_id)
        mode = host.modes.get(block_id, 'change')
        if mode == 'fail':
            raise RuntimeError('scripted next-section failure')
        if mode == 'budget':
            raise LLMError('budget_exhausted', 'scripted insufficient budget', False)
        if mode == 'question':
            return SimpleNamespace(data={'action': 'ask_user', 'message': '你在项目二测试中具体执行了哪项测试？', 'slot': 'responsibility', 'related_source_ids': []})
        if mode == 'nochange':
            return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed', 'summary': '现有表达清楚，保留原文。'})
        ctx = self.runtime.repo.get_session_context(self.runtime.context)
        block = next(b for b in ctx['draft']['blocks'] if b['blockId'] == block_id)
        ref = block['sourceRefs'][0]
        return SimpleNamespace(data={'action': 'tool', 'tool': 'propose_change', 'args': {
            'operation': 'replace', 'target': block_id, 'before': block['markdown'],
            'after': block['markdown'].replace('参与', '协助'), 'reason': '梳理原有测试职责', 'gaps': [],
            'source_refs': [{'source_id': ref['sourceId'], 'source_revision': ref['sourceRevision'], 'quote': ref['quote']}]}})


class ProactiveFlowTests(unittest.TestCase):
    setUp = integration.AgentIntegrationTests.setUp
    request = integration.AgentIntegrationTests.request
    version = integration.AgentIntegrationTests.version
    chat = integration.AgentIntegrationTests.chat
    material = integration.AgentIntegrationTests.material
    send = integration.AgentIntegrationTests.send
    analyze = integration.AgentIntegrationTests.analyze
    conversation = integration.AgentIntegrationTests.conversation
    count = integration.AgentIntegrationTests.count

    def ready(self, body=BODY, expected=3):
        session = self.chat()
        repo = self.app.state.business_service.repository
        context = RequestContext(session['sessionId'])
        material = repo.save_material(context, name='构造简历.txt', media_type='text/plain', original_data=body.encode(), role='resume', parse_status='parsed', use_status='active')
        source = repo.save_source(context, text=body, role='resume', origin={'kind': 'material', 'materialId': material['materialId']}, use_status='active')
        repo.create_initial_draft(context, structure_blocks([{'markdown': body, 'sourceRefs': [{'sourceId': source['sourceId'], 'sourceRevision': source['sourceRevision'], 'quote': body}]}]))
        self.material(session, text='测试工程师JD\n岗位职责：参与测试。', role='jd', request_id='jd')
        host = self.app.state.agent_host
        host.phases, host.modes = [], {}
        host.client_factory = lambda runtime, budget: ProactiveClient(runtime)
        blocks = self.conversation(session)['draft']['blocks']
        self.assertEqual(len(blocks), expected)
        return session, host, [b['blockId'] for b in blocks]

    def turn(self, session, text, key):
        user = self.send(session, text, request_id=key)
        return user, self.analyze(session, user, key)

    def pending(self, session):
        return self.app.state.business_service.repository.get_session_context(RequestContext(session['sessionId']))['suggestions']

    def test_accept_advances_once_without_implicitly_applying_next_and_replays_once(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        self.assertEqual(self.conversation(session)['draft']['markdown'], BODY)
        answer, events = self.turn(session, '是', 'yes')
        self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user', events)
        self.assertEqual(host.phases, ids[:2])
        draft = self.conversation(session)['draft']['blocks']
        self.assertIn('协助项目一', draft[0]['markdown'])
        self.assertIn('参与项目二', draft[1]['markdown'])
        self.assertIn('参与项目三', draft[2]['markdown'])
        self.assertEqual([p['target']['blockId'] for p in self.pending(session)], ids[1:2])
        self.analyze(session, answer, 'replay')
        self.assertEqual(host.phases, ids[:2])
        self.assertEqual(self.count('suggestion_decisions'), 1)

    def test_frozen_sections_are_not_interviewed(self):
        body = '李明轩\n13800000000\n教育经历\n示例大学 本科\n项目经历1\n参与项目一测试。\n专业技能\nPython\n'
        session, host, ids = self.ready(body, expected=4)
        kinds = block_kinds([b['markdown'] for b in self.conversation(session)['draft']['blocks']])
        self.assertEqual(kinds, [None, None, 'project', None])
        self.turn(session, '继续', 'first')
        with db.database(self.path) as c:
            docs = [json.loads(row[0]) for row in c.execute("SELECT doc_json FROM agent_state WHERE scope='experience'")]
        self.assertEqual([(d['kind'], d['title']) for d in docs], [('project', '参与项目一测试。')])
        self.assertEqual(host.phases, [ids[2]])
        self.turn(session, '是', 'yes')
        self.assertEqual(host.phases, [ids[2]])
        self.assertIn('已处理 1 段', self.conversation(session)['messages'][-1]['content'])

    def test_scoped_project_list_excludes_internship_entries(self):
        from app.agent_edit_scope import project_blocks
        body = '实习经历\n2024.06—2024.09｜字节跳动｜后端\n- 接口\n项目经历1\n参与项目一测试。\n'
        session, host, ids = self.ready(body, expected=3)
        repo = self.app.state.business_service.repository
        blocks = project_blocks(repo, RequestContext(session['sessionId']))
        self.assertEqual([b['blockId'] for b in blocks], [ids[2]])

    def test_resume_without_experience_gets_a_targeted_prompt(self):
        body = '李明轩\n13800000000\n教育经历\n示例大学 本科\n专业技能\nPython\n'
        session, host, ids = self.ready(body, expected=3)
        self.turn(session, '继续', 'first')
        self.assertIn('还没识别到项目或实习经历', self.conversation(session)['messages'][-1]['content'])
        self.assertEqual(host.phases, [])

    def test_each_accept_moves_forward_then_summarizes_without_extra_call(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        for i in range(3):
            self.turn(session, '是', f'yes-{i}')
        value = self.conversation(session)
        self.assertEqual(host.phases, ids)
        self.assertEqual(self.pending(session), [])
        self.assertIn('本轮简历检查已完成', value['messages'][-1]['content'])
        self.assertEqual(self.count('suggestion_decisions'), 3)
        self.turn(session, '继续', 'after-complete')
        self.assertEqual(host.phases, ids)

    def test_pending_continue_and_pause_neither_accept_nor_generate(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        self.turn(session, '暂停', 'stop')
        self.assertIn('已暂停', self.conversation(session)['messages'][-1]['content'])
        self.turn(session, '继续', 'pending-continue')
        self.assertEqual(host.phases, ids[:1])
        self.assertEqual(self.count('suggestion_decisions'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], BODY)
        self.turn(session, '是', 'yes-after-stop')
        self.assertEqual(host.phases, ids[:2])

    def test_specific_question_keeps_next_focus_until_answered(self):
        session, host, ids = self.ready()
        host.modes[ids[1]] = 'question'
        self.turn(session, '继续', 'first')
        self.turn(session, '是', 'yes')
        self.assertIn('具体执行了哪项测试', self.conversation(session)['messages'][-1]['content'])
        self.assertEqual(self.pending(session), [])
        host.modes[ids[1]] = 'change'
        self.turn(session, '继续', 'resume-question')
        self.assertEqual(host.phases, [ids[0], ids[1], ids[1]])
        self.assertEqual(self.pending(session)[0]['target']['blockId'], ids[1])

    def test_nochange_preserves_original_and_names_next_section(self):
        session, host, ids = self.ready()
        host.modes[ids[0]] = 'nochange'
        self.turn(session, '继续', 'first')
        self.assertEqual(self.conversation(session)['draft']['markdown'], BODY)
        self.assertIn('项目经历2', self.conversation(session)['messages'][-1]['content'])
        self.turn(session, '继续', 'next')
        self.assertEqual(host.phases, ids[:2])

    def test_explicit_scope_completes_without_touching_other_projects(self):
        session, host, ids = self.ready()
        self.turn(session, '请只优化第二个项目，其他正文保持不变。', 'scoped')
        if not self.pending(session):
            self.turn(session, '继续', 'scoped-analysis')
        self.turn(session, '是', 'yes')
        self.assertEqual(host.phases, [ids[1]])
        self.assertIn('指定范围已处理完', self.conversation(session)['messages'][-1]['content'])
        self.assertEqual(self.conversation(session)['draft']['markdown'], BODY.replace('参与项目二', '协助项目二'))

    def test_next_failure_keeps_acceptance_and_retry_skips_saved_section(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        host.modes[ids[1]] = 'fail'
        self.turn(session, '是', 'yes')
        value = self.conversation(session)
        self.assertIn('上一条改写已保存', value['messages'][-1]['content'])
        self.assertIn('协助项目一', value['draft']['markdown'])
        self.assertEqual(self.count('suggestion_decisions'), 1)
        host.modes[ids[1]] = 'change'
        self.turn(session, '继续', 'retry')
        self.assertEqual(host.phases[-1], ids[1])

    def test_next_budget_failure_keeps_accepted_text(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        host.modes[ids[1]] = 'budget'
        self.turn(session, '是', 'yes')
        value = self.conversation(session)
        self.assertIn('额度不足', value['messages'][-1]['content'])
        self.assertIn('已保存', value['messages'][-1]['content'])
        self.assertIn('协助项目一', value['draft']['markdown'])
        self.assertEqual(self.count('suggestion_decisions'), 1)

    def test_restart_keeps_accepted_progress_and_pending_next(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        self.turn(session, '是', 'yes')
        app = create_app(self.path)
        lifecycle = app.router.lifespan_context(app)
        self.runner.run(lifecycle.__aenter__())
        self.addCleanup(lambda: self.runner.run(lifecycle.__aexit__(None, None, None)))
        self.app = app
        new_host = app.state.agent_host
        new_host.phases, new_host.modes = [], {}
        new_host.client_factory = lambda runtime, budget: ProactiveClient(runtime)
        self.turn(session, '是', 'restart-yes')
        self.assertEqual(new_host.phases, ids[2:])
        self.assertEqual(self.count('suggestion_decisions'), 2)

    def test_legacy_acceptance_without_progress_is_not_rewritten(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        self.turn(session, '是', 'yes')
        with db.database(self.path) as connection:
            connection.execute("DELETE FROM agent_state WHERE key LIKE 'host-progress:%'")
        self.turn(session, '是', 'legacy-next')
        self.assertEqual(host.phases, ids)
        self.assertEqual(self.count('suggestion_decisions'), 2)

    def test_reject_keeps_original_and_continue_moves_to_next(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        host.client_factory = lambda runtime, budget: ProactiveClient(runtime, answer='reject')
        self.turn(session, '拒绝', 'reject')
        self.assertEqual(self.conversation(session)['draft']['markdown'], BODY)
        self.assertEqual(host.phases, ids[:1])
        self.turn(session, '继续', 'next')
        self.assertEqual(host.phases, ids[:2])

    def test_zero_budget_does_not_call_model_after_accepting(self):
        session, host, ids = self.ready()
        self.turn(session, '继续', 'first')
        original = ProjectBudget.status
        def exhausted(budget):
            return {**original(budget), 'remainingCents': 0}
        with patch.object(ProjectBudget, 'status', exhausted):
            self.turn(session, '是', 'yes')
        self.assertEqual(host.phases, ids[:1])
        self.assertEqual(self.count('suggestion_decisions'), 1)
        self.assertIn('预算已用完', self.conversation(session)['messages'][-1]['content'])

    def test_host_enforces_one_suggestion_and_current_section_even_if_model_disobeys(self):
        session, host, ids = self.ready()
        user = self.send(session, '继续', request_id='limit')
        host.start(session['sessionId'], {'requestId': 'limit', 'userMessageId': user['messageId'], 'expectedStateRevision': self.version(session['sessionId'])})
        runtime = host.active[(session['sessionId'], 'limit')]
        adapter = HostAdapter(runtime, ids[0], enforce_focus=True)
        block = self.conversation(session)['draft']['blocks'][0]
        ref = block['sourceRefs'][0]
        refs = [SourceRef(source_id=ref['sourceId'], source_revision=ref['sourceRevision'], quote=ref['quote'])]
        with self.assertRaises(RepositoryError) as error:
            adapter.save_suggestion(session['sessionId'], ids[1], 'replace', block['markdown'], '协助测试', '构造越界', refs, [])
        self.assertEqual(error.exception.code, 'scope_violation')
        adapter.save_suggestion(session['sessionId'], ids[0], 'replace', block['markdown'], block['markdown'].replace('参与', '协助'), '梳理职责', refs, [])
        with self.assertRaises(RepoError) as error:
            adapter.save_suggestion(session['sessionId'], ids[0], 'replace', block['markdown'], '再次改写', '重复建议', refs, [])
        self.assertEqual(error.exception.code, 'suggestion_limit')
        self.assertEqual(self.count('suggestions'), 1)

    def test_source_revision_change_requires_review_before_continuing(self):
        session, host, ids = self.ready()
        host.modes[ids[0]] = 'nochange'
        self.turn(session, '继续', 'first')
        repo = self.app.state.business_service.repository
        ctx = RequestContext(session['sessionId'])
        source = next(s for s in repo.get_session_context(ctx)['sources'] if s['role'] == 'resume')
        repo.save_source(ctx, text=BODY.replace('参与项目一', '协助项目一'), role='resume', origin=source['origin'], use_status='active', source_id=source['sourceId'], expected_source_revision=source['sourceRevision'])
        self.turn(session, '继续', 'changed-source')
        self.assertEqual(host.phases, ids[:1])
        self.assertIn('来源', self.conversation(session)['messages'][-1]['content'])


if __name__ == '__main__':
    unittest.main()
