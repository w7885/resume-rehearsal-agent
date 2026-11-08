"""State binding, fact preservation, evidence selection and guided questions."""
import asyncio
from types import SimpleNamespace
import unittest

from app.agent.context import assemble_context
from app.agent.guard import GuardState, check
from app.agent.memory import ExperienceDoc, Fact, NewFact, Question, merge_facts, save_experience, load_experience
from app.agent.ports import SessionContext, SessionInfo, Message, SourceText, SourceRef, RepoError
from app.agent.questions import guided_question, with_answer_guidance
from app.agent.schemas import AskStep, SaveFactsArgs
from app.agent.store import InMemoryStateStore
from app.agent.tools import _literal_fact_source, _save_facts, ToolContext
from app.agent_answer_sources import factual_answer
from app.business_service import BusinessService
from app.repository import Repository, RequestContext
import test_goal_and_confirmation as integration


class MemoryContractTests(unittest.TestCase):
    def doc(self):
        return ExperienceDoc(experience_id='p1', seq=1, kind='project', raw_text='实现姓名模糊查询和接口测试。')

    def test_literal_fact_source_accepts_one_exact_multi_clause_sentence_only(self):
        context = SessionContext(
            session=SessionInfo(session_id='s'),
            sources=[SourceText(
                source_id='answer',
                kind='supplement',
                text='少做的一步：人工配置改为自动配置，验证周期缩短。实际反馈是技术负责人认可方案。',
            )],
        )
        reference = _literal_fact_source(context, '人工配置改为自动配置，验证周期缩短')
        self.assertEqual(reference.source_id, 'answer')
        self.assertEqual(reference.quote, '人工配置改为自动配置，验证周期缩短')
        self.assertIsNone(_literal_fact_source(
            context, '人工配置改为自动配置，实际反馈是技术负责人认可方案',
        ))

    def test_external_context_saves_exact_multi_clause_personal_answer(self):
        answer = '少做的一步：人工配置改为自动配置，验证周期缩短。'
        statement = '人工配置改为自动配置，验证周期缩短'
        store = InMemoryStateStore()
        save_experience(store, 's', ExperienceDoc(
            experience_id='p1', seq=1, kind='project', raw_text='参与推荐策略验证。',
        ), None)
        context = SimpleNamespace(
            session=SessionInfo(session_id='s', experience_order=['p1']),
            messages=[Message(message_id='m', role='user', text=answer)],
            sources=[SourceText(source_id='answer', kind='supplement', text=answer)],
            web_context={'references': [{'referenceId': 'external-1'}]},
        )
        repo = SimpleNamespace(get_session_context=lambda sid: context)
        args = SaveFactsArgs(experience_id='p1', facts=[
            NewFact(slot='effect', statement=statement),
        ])
        result = asyncio.run(_save_facts(args, ToolContext('s', repo, store, 'm')))
        self.assertEqual(result['active_facts'], 1)
        fact = load_experience(store, 's', 'p1')[0].facts[0]
        self.assertEqual(fact.statement, statement)
        self.assertEqual(fact.source_refs[0].quote, statement)

    def test_unrelated_negation_preserves_all_actions(self):
        doc = self.doc()
        merge_facts(doc, [NewFact(slot='action', statement='按姓名模糊查询'), NewFact(slot='action', statement='删除前确认')])
        merge_facts(doc, [NewFact(slot='action', statement='用sqlite3保存，未使用复杂框架')])
        self.assertEqual(len([f for f in doc.facts if f.status == 'active']), 3)

    def test_correction_requires_explicit_user_intent_and_exact_fact_ids(self):
        doc = self.doc()
        merge_facts(doc, [NewFact(slot='action', statement='使用MySQL保存'), NewFact(slot='action', statement='删除前确认')])
        store = InMemoryStateStore(); save_experience(store, 's', doc, None)
        context = SessionContext(session=SessionInfo(session_id='s', experience_order=['p1']),
            messages=[Message(message_id='m', role='user', text='更正：不是MySQL，而是sqlite3。')])
        repo = SimpleNamespace(get_session_context=lambda sid: context)
        args = SaveFactsArgs(experience_id='p1', facts=[NewFact(slot='action', statement='使用sqlite3保存', supersedes_fact_ids=['f1'])])
        asyncio.run(_save_facts(args, ToolContext('s', repo, store, 'm')))
        updated = load_experience(store, 's', 'p1')[0]
        self.assertEqual(updated.facts[0].status, 'superseded')
        self.assertEqual(updated.facts[1].status, 'active')
        context.messages[0].text = '我还写了查询功能。'
        bad = SaveFactsArgs(experience_id='p1', facts=[NewFact(slot='action', statement='没有查询功能', supersedes_fact_ids=['f2'])])
        with self.assertRaises(RepoError):
            asyncio.run(_save_facts(bad, ToolContext('s', repo, store, 'm')))
        self.assertEqual(load_experience(store, 's', 'p1')[0].facts[1].status, 'active')

    def test_active_fact_keeps_its_older_source_in_context(self):
        doc = self.doc()
        sources = [SourceText(source_id=f's{i}', kind='supplement', text=f'原始回答{i}：整理{i+8}条反馈') for i in range(8)]
        doc.source_ids = [s.source_id for s in sources]
        doc.facts = [Fact(id='f1', slot='action', statement='整理8条反馈', source_refs=[SourceRef(source_id='s0', source_revision=1, quote=sources[0].text)])]
        ctx = SessionContext(session=SessionInfo(session_id='s'), sources=sources)
        output = assemble_context(ctx, [doc], {}, 'p1')
        self.assertIn(sources[0].text, output)
        self.assertIn('原始依据：[s0，版本 1]', output)

    def test_concrete_example_is_allowed_but_hypothesis_is_not(self):
        self.assertTrue(factual_answer('程序支持查询，例如输入学号即可查看联系方式。'))
        self.assertFalse(factual_answer('例如我提高30%效率'))
        self.assertFalse(factual_answer('我实现查询，例如如果提升30%就写进去。'))

    def test_guidance_is_specific_optional_and_checked(self):
        doc = self.doc()
        state = GuardState(session=SessionContext(session=SessionInfo(session_id='s')), focus_doc=doc, anchor_text=doc.raw_text)
        for slot in ('responsibility', 'action', 'context', 'effect', 'tech', 'evidence'):
            with self.subTest(slot=slot):
                text = guided_question(doc, slot)
                self.assertEqual(text.count('？'), 1)
                self.assertLessEqual(len(text), 120)
                result = check(AskStep(action='ask_user', message=text, slot=slot), state)
                self.assertIsNone(result.violation, text)
        question = with_answer_guidance('模糊查询这部分，你做了什么？', doc, 'action')
        self.assertIn('挑一件具体的事说', question)
        doc.questions.append(Question(id='q1', slot='action', text=question, fingerprint='q'))
        repeat = AskStep(action='ask_user', message='模糊查询这部分，你做了什么？可以先选实现或测试。', slot='action')
        self.assertEqual(check(repeat, state).violation.code, 'question_repeat')


class DecisionBindingTests(unittest.TestCase):
    setUp = integration.GoalAndConfirmationTests.setUp
    request = integration.GoalAndConfirmationTests.request
    version = integration.GoalAndConfirmationTests.version
    chat = integration.GoalAndConfirmationTests.chat
    material = integration.GoalAndConfirmationTests.material
    send = integration.GoalAndConfirmationTests.send
    analyze = integration.GoalAndConfirmationTests.analyze
    conversation = integration.GoalAndConfirmationTests.conversation
    count = integration.GoalAndConfirmationTests.count
    ready = integration.GoalAndConfirmationTests.ready
    proposal = integration.GoalAndConfirmationTests.proposal
    run_context = integration.GoalAndConfirmationTests.run_context

    def test_unknown_action_with_unique_target_does_not_ask_which_suggestion(self):
        session, _, host, _ = self.proposal()
        user = self.send(session, '保存', request_id='save')
        self.analyze(session, user, 'save')
        text = self.conversation(session)['messages'][-1]['content']
        self.assertNotIn('哪一条', text)
        self.assertIn('采用', text)
        self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_two_prompts_for_same_suggestion_are_one_target(self):
        session, trigger, host, _ = self.proposal()
        repo = Repository(self.path); context = self.run_context(session)
        proposal = repo.get_session_context(context)['suggestions'][0]
        for i in range(2):
            BusinessService(repo).save_assistant_message(context, {'requestId': f'duplicate-{i}', 'userMessageId': trigger['messageId'],
                'content': '是否采用同一条建议？', 'kind': 'change_question', 'suggestionIds': [proposal['suggestionId']]}, synthetic=True)
        user = self.send(session, '是', request_id='yes')
        self.analyze(session, user, 'yes')
        self.assertEqual(self.count('suggestion_decisions'), 1)
        self.assertEqual(host.decision_calls, 0)


class RequirementContextTests(unittest.TestCase):
    def test_requirements_and_verbatim_rule_are_in_context(self):
        ctx = SessionContext(
            session=SessionInfo(session_id='s'),
            web_context={'available': True, 'jobEvidenceStatus': 'verified_job_reference',
                         'requirements': [{'requirementId': 'req-1', 'referenceId': 'ref-1',
                                           'quote': '负责跨部门需求对接与排期推进'}]},
        )
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='project', raw_text='参与接口测试。')
        output = assemble_context(ctx, [doc], {}, 'p1')
        self.assertIn('req-1', output)
        self.assertIn('负责跨部门需求对接与排期推进', output)
        self.assertIn('岗位要求原文只能逐字引用', output)

    def test_partial_uncertain_fields_are_visible_without_declining(self):
        ctx = SessionContext(session=SessionInfo(session_id='s'))
        doc = ExperienceDoc(experience_id='p1', seq=1, kind='project', raw_text='参与接口测试。')
        doc.questions.append(Question(
            id='q1', slot='effect', text='带来了哪些变化？', fingerprint='f',
            outcome='answered', answer_kind='partial',
            uncertain_fields=['effect'], uncertain_spans=['具体效果忘了']))
        output = assemble_context(ctx, [doc], {}, 'p1')
        self.assertIn('未确认：effect', output)
        self.assertNotIn('已拒绝的维度：effect', output)
