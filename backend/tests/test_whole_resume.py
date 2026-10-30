"""Whole-resume review uses section-specific questions, evidence and consent."""
import json
import re
from types import SimpleNamespace
import unittest

from app import db
from app.agent.guard import GuardState, check
from app.agent.memory import ExperienceDoc, effect_state, load_experience
from app.agent.ports import SessionContext, SessionInfo
from app.agent.schemas import AskStep, StepDecisionAdapter
from app.agent_state import GuardedStateStore
from app.agent_edit_scope import resolve_scope
from app.repository import Repository, RequestContext
from app.resume_structure import section_kinds, split_resume
import test_proactive_flow as proactive

BODY = ('基本信息\n姓名：张同学\n邮箱：test@example.com\n'
        '教育背景\n测试大学 本科\n'
        '项目经历1\n参与Python查询脚本开发。\n'
        '专业技能\n了解Python。\n'
        '自我评价\n学习能力强。\n'
        '获奖经历\n校级奖学金。\n')


class SectionClient(proactive.ProactiveClient):
    async def decide(self, **kwargs):
        runtime = self.runtime
        host = runtime.host
        target = re.search(r'段落编号（blockId）：(\S+)', kwargs['user'])[1]
        doc = load_experience(GuardedStateStore(runtime), runtime.sid, target)[0]
        data = runtime.repo.get_session_context(runtime.context)
        block = next(b for b in data['draft']['blocks'] if b['blockId'] == target)
        host.section_contexts.append(kwargs['user'])
        if self.proposed:
            return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed'})
        if doc.kind in {'project', 'work'}:
            if not doc.questions:
                return SimpleNamespace(data={'action': 'ask_user', 'message': '查询脚本中，你本人负责哪部分？', 'slot': 'responsibility'})
            effect = effect_state(doc)
            if effect in {'missing', 'followup'}:
                message = '这段经历上线或交付后，带来了哪些实际变化？' if effect == 'missing' else '除了使用情况，实际带来了什么变化？'
                return SimpleNamespace(data={'action': 'ask_user', 'message': message, 'slot': 'effect'})
            after = block['markdown'].replace('参与', '协助')
        elif doc.kind == 'summary' and host.summary_mode != 'use-project':
            if not doc.questions:
                return SimpleNamespace(data={'action': 'ask_user', 'message': '你希望突出哪项优势？', 'slot': 'strength'})
            if not any(f.slot == 'strength' for f in doc.facts):
                return SimpleNamespace(data={'action': 'tool', 'tool': 'save_facts', 'args': {'experience_id': target,
                    'facts': [{'slot': 'strength', 'statement': '自学Flask并完成查询接口演示'}]}})
            after = '自我评价\n通过自学Flask完成查询接口演示，有动手实践的学习经历。\n'
        elif doc.kind == 'summary':
            after = getattr(host, 'summary_text', '自我评价\n参与Python查询脚本开发，注重通过动手实践学习。\n')
        else:
            after = '## ' + block['markdown']
        refs = [{'source_id': r['sourceId'], 'source_revision': r['sourceRevision'], 'quote': r['quote']} for r in block['sourceRefs']]
        if doc.kind == 'summary':
            if host.summary_mode == 'use-project':
                source = next(s for s in data['sources'] if '参与Python查询脚本开发。' in s['text'])
                refs.append({'source_id': source['sourceId'], 'source_revision': source['sourceRevision'], 'quote': '参与Python查询脚本开发。'})
            else:
                source = next(s for s in data['sources'] if s['role'] == 'supplement' and 'Flask' in s['text'])
                refs.append({'source_id': source['sourceId'], 'source_revision': source['sourceRevision'], 'quote': source['text']})
        self.proposed = True
        return SimpleNamespace(data={'action': 'tool', 'tool': 'propose_change', 'args': {
            'operation': 'replace', 'target': target, 'before': block['markdown'], 'after': after,
            'reason': '按当前段类型整理表达', 'source_refs': refs, 'gaps': []}})


class WholeResumeTests(unittest.TestCase):
    setUp = proactive.ProactiveFlowTests.setUp
    request = proactive.ProactiveFlowTests.request
    version = proactive.ProactiveFlowTests.version
    chat = proactive.ProactiveFlowTests.chat
    material = proactive.ProactiveFlowTests.material
    send = proactive.ProactiveFlowTests.send
    analyze = proactive.ProactiveFlowTests.analyze
    conversation = proactive.ProactiveFlowTests.conversation
    count = proactive.ProactiveFlowTests.count
    ready = proactive.ProactiveFlowTests.ready
    turn = proactive.ProactiveFlowTests.turn
    pending = proactive.ProactiveFlowTests.pending

    def setup_whole(self, body=BODY, expected=6):
        session, host, ids = self.ready(body, expected)
        host.summary_mode, host.section_contexts = 'ask', []
        host.client_factory = lambda runtime, budget: SectionClient(runtime)
        return session, host, ids

    def test_default_review_covers_experience_skills_and_summary_only(self):
        session, host, ids = self.setup_whole()
        original = self.conversation(session)['draft']['markdown']
        self.turn(session, '检查整份简历', 'start')
        self.assertEqual(self.conversation(session)['draft']['markdown'], original)
        repo = Repository(self.path)
        seen = []
        for i in range(25):
            value = self.conversation(session)
            suggestions = self.pending(session)
            if suggestions:
                self.assertEqual(len(suggestions), 1)
                seen.append(suggestions[0]['target']['blockId'])
                self.turn(session, '是', f'accept-{i}')
            elif value['messages'][-1].get('assistant', {}).get('kind') == 'question':
                question = value['messages'][-1]['content']
                answer = ('我用周末自学Flask，完成查询接口演示。' if '优势' in question
                          else '没有' if '实际变化' in question or '使用情况' in question
                          else '我负责查询功能的代码实现。')
                self.turn(session, answer, f'answer-{i}')
            else:
                break
        # 基本信息（ids[0]）、教育背景（ids[1]）、获奖经历（ids[5]）按原文保留，不进入检查顺序。
        self.assertEqual(seen, [ids[2], ids[3], ids[4]])
        self.assertEqual(self.count('suggestion_decisions'), 3)
        self.assertIn('本轮简历检查已完成', self.conversation(session)['messages'][-1]['content'])
        self.assertIn('test@example.com', self.conversation(session)['draft']['markdown'])
        with db.database(self.path, read_only=True) as c:
            docs = [json.loads(r[0]) for r in c.execute("SELECT doc_json FROM agent_state WHERE scope='experience'")]
        for doc in docs:
            if doc['kind'] not in {'project', 'work'}:
                self.assertFalse(any(q['slot'] in {'responsibility', 'action', 'effect'} for q in doc['questions']))
        for b in self.conversation(session)['draft']['blocks']:
            for ref in b['sourceRefs']:
                source = repo.read_source_snapshot(RequestContext(session['sessionId']), ref['sourceId'], ref['sourceRevision'])['source']
                self.assertIn(ref['quote'], source['text'])

    def test_explicitly_named_education_is_still_processed(self):
        session, host, ids = self.setup_whole()
        self.turn(session, '请修改教育背景', 'education')
        self.assertEqual([p['target']['blockId'] for p in self.pending(session)], [ids[1]])
        announced = [m['content'] for m in self.conversation(session)['messages']
                     if m['role'] == 'assistant' and (m.get('content') or '').startswith('现在进入')]
        self.assertEqual(announced, ['现在进入「教育背景」的修改。'])

    def test_named_summary_can_use_project_evidence_without_touching_other_sections(self):
        session, host, ids = self.setup_whole()
        host.summary_mode = 'use-project'
        self.turn(session, '请只修改专业技能', 'skills')
        old = self.pending(session)[0]
        self.turn(session, '请只修改自我评价，写得简洁些', 'summary')
        current = self.pending(session)
        summary = next(p for p in current if p['target']['blockId'] == ids[4])
        self.assertTrue(any(p['suggestionId'] == old['suggestionId'] for p in current))
        self.assertEqual(self.count('suggestion_decisions'), 0)
        self.assertTrue(any('参与Python查询脚本开发。' in r['quote'] for r in summary['sourceRefs']))
        before = self.conversation(session)['draft']['blocks']
        self.turn(session, '是', 'accept')
        after = self.conversation(session)['draft']['blocks']
        self.assertTrue(all(a['markdown'] == b['markdown'] for a, b in zip(before, after) if a['blockId'] != ids[4]))
        self.assertIn('动手实践', next(b['markdown'] for b in after if b['blockId'] == ids[4]))
        host.summary_text = '自我评价\n参与Python查询脚本开发，重视实践学习。\n'
        self.turn(session, '请只修改自我评价，写得简洁些', 'revise-accepted')
        self.assertTrue(any(p['target']['blockId'] == ids[4] for p in self.pending(session)))
        self.assertEqual(self.count('suggestion_decisions'), 1)

    def test_section_aliases_and_project_ordinals_keep_separate_indices(self):
        body = BODY.replace('项目经历1\n', '项目经历：') + '个人优势\n习惯记录和复盘。\n'
        session, host, ids = self.setup_whole(body, 7)
        repo = Repository(self.path)
        for i, (request, expected) in enumerate((('请只改个人优势', {ids[6]}), ('请只修改自我评价', {ids[4]}),
                                               ('自我评价再简短一点', {ids[4]}),
                                               ('请只优化第一个项目', {ids[2]}), ('请只改不存在的一节', set()),
                                               ('请检查整份简历', None))):
            self.send(session, request, request_id=f'scope-{i}')
            self.assertEqual(resolve_scope(repo, RequestContext(session['sessionId'])), expected)

    def test_skip_current_section_suggestion_keeps_original_and_advances(self):
        session, host, ids = self.setup_whole()
        before = self.conversation(session)['draft']['markdown']
        self.turn(session, '检查整份简历', 'start')
        # 经历段先确认职责和实际变化，才给出建议。
        for attempt in range(3):
            last = self.conversation(session)['messages'][-1]
            if (last.get('assistant') or {}).get('kind') != 'question':
                break
            text = '老师录入时间从半小时缩短到五分钟。' if '实际变化' in last['content'] else '我负责查询功能的代码实现。'
            self.turn(session, text, f'answer-{attempt}')
        self.assertEqual(self.pending(session)[0]['target']['blockId'], ids[2])
        self.turn(session, '跳过这一节', 'skip')
        self.assertEqual(self.conversation(session)['draft']['markdown'], before)
        # 下一段是专业技能，直接给建议，不问职责和上线效果。
        self.assertEqual([p['target']['blockId'] for p in self.pending(session)], [ids[3]])
        with db.database(self.path, read_only=True) as c:
            self.assertEqual(c.execute('SELECT decision FROM suggestion_decisions').fetchone()[0], 'reject')


class SectionGuardTests(unittest.TestCase):
    def state(self, kind, text):
        doc = ExperienceDoc(experience_id='p', seq=1, kind=kind, raw_text=text, block_id='p')
        return GuardState(session=SessionContext(session=SessionInfo(session_id='s', experience_order=['p'])),
            focus_doc=doc, focus_block_markdown=text, source_texts={'src': text}, source_kinds={'src': 'resume'})

    def proposal(self, state, after):
        step = StepDecisionAdapter.validate_python({'action':'tool','tool':'propose_change','args':{
            'operation':'replace','target':'p','before':state.focus_block_markdown,'after':after,'reason':'整理',
            'source_refs':[{'source_id':'src','source_revision':1,'quote':state.focus_block_markdown}],'gaps':[]}})
        return check(step, state)

    def test_information_can_be_formatted_without_responsibility_or_effect_questions(self):
        state = self.state('profile', '姓名：张同学 邮箱：test@example.com')
        self.assertIsNone(self.proposal(state, '姓名：张同学｜邮箱：test@example.com').violation)
        self.assertEqual(self.proposal(state, '姓名：张同学｜邮箱：other@example.com').violation.code, 'profile_identity_changed')
        question = AskStep(action='ask_user', message='这段经历上线后有什么效果？', slot='effect')
        self.assertEqual(check(question, state).violation.code, 'section_question')

    def test_language_upgrade_cannot_invent_credentials_or_skill_level(self):
        for kind, before, after in (('education','测试大学 本科','测试大学 博士'),
                                   ('skills','了解Python','精通Python'),
                                   ('summary','参与接口测试','沟通能力强，拥有教师资格证')):
            with self.subTest(kind=kind):
                self.assertEqual(self.proposal(self.state(kind, before), after).violation.code, 'unsupported_section_claim')

    def test_all_sections_and_inline_aliases_are_recognized_losslessly(self):
        parts = split_resume(BODY)
        self.assertEqual(''.join(parts), BODY)
        self.assertEqual(section_kinds(parts), ['profile','education','project','skills','summary','awards'])
        self.assertEqual(section_kinds(['自我优势：习惯复盘']), ['summary'])
        self.assertEqual(section_kinds(['自我优势']), ['summary'])
        self.assertEqual(section_kinds(['姓名：张同学\n邮箱：test@example.com']), ['profile'])
        inline = '基本信息：姓名张同学\n教育背景：测试大学 本科\n自我评价：习惯记录\n个人优势：做事认真\n'
        self.assertEqual(''.join(split_resume(inline)), inline)
        self.assertEqual(section_kinds(split_resume(inline)), ['profile', 'education', 'summary', 'summary'])
        self.assertEqual(section_kinds(split_resume('教育背景：测试大学 本科\n项目经历：参与查询开发\n自我评价：习惯记录\n')), ['education','project','summary'])

    def test_profile_phone_formatting_keeps_the_actual_number(self):
        state = self.state('profile', '电话：13812345678')
        self.assertIsNone(self.proposal(state, '电话：138-1234-5678').violation)
        self.assertEqual(self.proposal(state, '电话：13922223333').violation.code, 'profile_identity_changed')

    def test_projects_still_require_the_existing_interview_gate(self):
        state = self.state('project', '参与接口测试。')
        self.assertEqual(self.proposal(state, '协助接口测试。').violation.code, 'ask_first')
