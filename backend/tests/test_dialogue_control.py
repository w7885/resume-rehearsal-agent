"""Intent -> state -> next action, with scripted semantic decisions only."""
import unittest

from app.agent.dialogue import factual_quote
from app.agent.dialogue import context_reply
from app.agent.questions import guided_question
from app.agent.memory import load_experience, load_session, sync_experiences
from app.agent.store import InMemoryStateStore
from app.resume_structure import section_kinds, block_kinds
from fakes import FakeLLM
import test_agent_session_policy as policy


def intent(action='meta', **fields):
    return {'intent': action, 'scope': 'experience', 'confidence': 1,
            'target_experience_id': 'exp_01', **fields}


class DialogueControlTests(unittest.TestCase):
    def ready(self, slot='action'):
        repo = policy.make_repo(direction='开发实习', direction_confirmed=True)
        store = InMemoryStateStore()
        policy.seed_experience(store, asked='你在交易平台中完成了什么任务？', slot=slot)
        return repo, store

    def test_paraphrased_questions_preserve_state_and_never_enter_interview_loop(self):
        for text in ('哪段', '？', '你指的是我哪一项经历', '我没明白你在问什么',
                     '为什么需要这项信息', '这个问题和我的求职方向有什么关系？'):
            with self.subTest(text=text):
                repo, store = self.ready()
                before = load_experience(store, policy.SESSION, 'exp_01')[0]
                llm = FakeLLM(route_script=[intent(reply='我在核对当前交易平台的个人任务。')])
                events = policy.run_turn(repo, store, llm, text)
                after = load_experience(store, policy.SESSION, 'exp_01')[0]
                self.assertEqual(after.questions, before.questions)
                self.assertEqual((after.round, after.no_fact_streak), (before.round, before.no_fact_streak))
                self.assertFalse(any(kind == 'decide' for kind, _ in llm.calls))
                self.assertTrue(any(e.type == 'notice' and '校园二手交易平台' in e.payload['text'] for e in events))
                route_prompt = llm.calls[0][1]['user']
                self.assertIn(before.questions[0].text, route_prompt)
                self.assertIn(before.raw_text, route_prompt)

    def test_genuine_answer_updates_question_then_enters_loop_once(self):
        repo, store = self.ready()
        llm = FakeLLM(route_script=[intent('answer', answers_pending=True)], decide_script=[policy.FINISH])
        policy.run_turn(repo, store, llm, '我负责接口测试。')
        doc = load_experience(store, policy.SESSION, 'exp_01')[0]
        self.assertEqual(doc.questions[0].outcome, 'answered')
        self.assertEqual(sum(kind == 'decide' for kind, _ in llm.calls), 1)
        self.assertEqual(doc.no_fact_streak, 1)

    def test_mixed_answer_and_question_saves_exact_answer_and_responds_first(self):
        repo, store = self.ready()
        quote = '我负责接口测试。'
        llm = FakeLLM(route_script=[intent(answers_pending=True, answer_quote=quote,
                                         reply='我问的是当前交易平台中的测试任务。')])
        policy.run_turn(repo, store, llm, quote + '你现在指的是哪个项目？')
        doc = load_experience(store, policy.SESSION, 'exp_01')[0]
        self.assertEqual(doc.questions[0].outcome, 'answered')
        self.assertEqual(doc.questions[0].answer_quote, quote)
        self.assertEqual(doc.supplement_quotes['m1'], quote)
        self.assertEqual(doc.no_fact_streak, 0)
        self.assertFalse(any(kind == 'decide' for kind, _ in llm.calls))

    def test_supplement_for_another_dimension_does_not_answer_pending_question(self):
        repo, store = self.ready(slot='effect')
        quote = '我使用Python实现查询。'
        llm = FakeLLM(route_script=[intent('answer', answer_quote=quote, answers_pending=False,
                                         reply='这说明了实现方法；当前效果问题仍未回答。')])
        policy.run_turn(repo, store, llm, quote)
        doc = load_experience(store, policy.SESSION, 'exp_01')[0]
        self.assertEqual(doc.questions[0].outcome, 'asked')
        self.assertEqual(doc.supplement_quotes['m1'], quote)
        self.assertEqual(doc.round, 0)

    def test_invalid_or_uncertain_classification_never_defaults_to_answer(self):
        for decision in ({'intent': 'invented'}, intent('answer', confidence=0.2, answers_pending=True),
                         intent('answer'), intent('answer', answers_pending='yes'),
                         intent('focus', target_experience_id='foreign')):
            with self.subTest(decision=decision):
                repo, store = self.ready()
                llm = FakeLLM(route_script=[decision])
                policy.run_turn(repo, store, llm, '请先解释当前的问题')
                doc = load_experience(store, policy.SESSION, 'exp_01')[0]
                self.assertEqual(doc.questions[0].outcome, 'asked')
                self.assertEqual(doc.no_fact_streak, 0)
                self.assertFalse(any(kind == 'decide' for kind, _ in llm.calls))

    def test_question_without_valid_answer_span_cannot_be_consumed_even_if_model_says_answer(self):
        repo, store = self.ready()
        llm = FakeLLM(route_script=[intent('answer', answers_pending=True)])
        policy.run_turn(repo, store, llm, '你说的是哪个任务？')
        self.assertEqual(load_experience(store, policy.SESSION, 'exp_01')[0].questions[0].outcome, 'asked')

    def test_refusal_closes_only_the_current_dimension(self):
        repo, store = self.ready(slot='effect')
        llm = FakeLLM(route_script=[intent('refuse')], decide_script=[policy.FINISH])
        policy.run_turn(repo, store, llm, '我不愿意讨论这项效果。')
        doc = load_experience(store, policy.SESSION, 'exp_01')[0]
        self.assertEqual(doc.questions[0].answer_kind, 'refused')
        self.assertEqual(doc.questions[0].outcome, 'declined')
        self.assertEqual(doc.declined, ['effect'])
        self.assertFalse(doc.skipped)

    def test_correction_does_not_implicitly_answer_an_unrelated_question(self):
        repo, store = self.ready(slot='effect')
        quote = '更正：我只负责查询功能。'
        llm = FakeLLM(route_script=[intent('correct', answer_quote=quote,
                                         reply='已保留职责更正，效果问题仍未回答。')])
        policy.run_turn(repo, store, llm, quote)
        doc = load_experience(store, policy.SESSION, 'exp_01')[0]
        self.assertEqual(doc.questions[0].outcome, 'asked')
        self.assertEqual(doc.supplement_quotes['m1'], quote)

    def test_goal_clarification_does_not_become_a_target_direction(self):
        repo, store = policy.make_repo(), InMemoryStateStore()
        policy.seed_session(store, goal_asked=True, awaiting_goal=True)
        llm = FakeLLM(route_script=[intent(reply='这里指你准备申请的岗位方向。')])
        policy.run_turn(repo, store, llm, '岗位方向是什么意思？')
        session = load_session(store, policy.SESSION)[0]
        self.assertTrue(session.awaiting_goal)
        self.assertEqual(session.goal, '')
        self.assertFalse(any(kind == 'decide' for kind, _ in llm.calls))

    def test_fragment_validation_keeps_negation_hypotheses_and_sentence_boundaries(self):
        for text, quote in [('我不熟悉Python。', '熟悉Python。'),
                            ('如果我负责测试，完成接口验证。', '完成接口验证。'),
                            ('假设是这样？我熟悉Python。', '我熟悉Python。'),
                            ('我负责接口测试。', '负责接口测试。')]:
            with self.subTest(text=text, quote=quote):
                self.assertIsNone(factual_quote(text, quote))
        quote = '我负责接口测试。'
        self.assertEqual(factual_quote(quote + '为什么要问？', quote), quote)

    def test_academic_metadata_does_not_receive_deployment_interview_but_projects_stay_projects(self):
        academic = '校园经历\n学业成绩：绩点3.85，CET6：578；数学竞赛省二等奖。'
        project = '校园经历\n学业成绩：绩点3.85。负责开发竞赛报名系统，组织接口测试。'
        self.assertEqual(section_kinds([academic]), ['education'])
        self.assertEqual(section_kinds([project]), ['project'])
        self.assertEqual(block_kinds([academic]), ['project'])

    def test_reclassified_information_retires_inapplicable_question_without_deleting_history(self):
        repo, store = self.ready(slot='effect')
        repo.draft_blocks[0].markdown='校园经历\n学业成绩：绩点3.85，CET6：578；竞赛省二等奖。'
        old=load_experience(store,policy.SESSION,'exp_01')[0]
        ctx=repo.get_session_context(policy.SESSION)
        sync_experiences(policy.SESSION,ctx,store,policy.SessionDoc(),None)
        new=load_experience(store,policy.SESSION,'exp_01')[0]
        self.assertEqual(new.kind,'education')
        self.assertEqual(new.questions[0].text,old.questions[0].text)
        self.assertEqual(new.questions[0].outcome,'asked')
        self.assertFalse(new.questions[0].active)
        self.assertIsNotNone(new.questions[0].resolution_reason)

    def test_mixed_campus_block_keeps_activity_and_names_its_actual_subentry(self):
        repo,store=self.ready()
        doc=load_experience(store,policy.SESSION,'exp_01')[0]
        doc.raw_text='校园经历\n学业成绩：绩点3.2。\n竞赛奖项：数学竞赛二等奖。\n▸数学建模：负责算法设计和代码实现。'
        doc.title='学业成绩：绩点3.2'
        self.assertEqual(section_kinds([doc.raw_text]),['project'])
        self.assertIn('数学建模',guided_question(doc,'action'))
        self.assertIn('数学建模',context_reply(doc))

    def test_ambiguous_short_replies_use_semantics_instead_of_skip_or_confirm_shortcuts(self):
        for text in ('不知道','没有','好','可以'):
            with self.subTest(text=text):
                repo,store=self.ready()
                llm=FakeLLM(route_script=[intent(reply='先解释当前任务，再确认你想表达的意思。')])
                policy.run_turn(repo,store,llm,text)
                self.assertEqual(load_experience(store,policy.SESSION,'exp_01')[0].questions[0].outcome,'asked')
                self.assertEqual(sum(kind=='route' for kind,_ in llm.calls),1)

    def test_uncertain_intent_cannot_activate_a_fact_quote(self):
        repo,store=self.ready()
        llm=FakeLLM(route_script=[intent('answer',confidence=0.2,answers_pending=True,answer_quote='我负责测试。')])
        policy.run_turn(repo,store,llm,'我负责测试。')
        doc=load_experience(store,policy.SESSION,'exp_01')[0]
        self.assertEqual(doc.questions[0].outcome,'asked')
        self.assertEqual(doc.supplement_quotes,{})

    def test_absence_of_goal_cannot_be_saved_as_a_job_direction(self):
        for text in ('没有','不知道','记不清了'):
            with self.subTest(text=text):
                repo,store=policy.make_repo(),InMemoryStateStore()
                policy.seed_session(store,goal_asked=True,awaiting_goal=True)
                llm=FakeLLM(route_script=[intent('answer',answers_pending=True)])
                policy.run_turn(repo,store,llm,text)
                session=load_session(store,policy.SESSION)[0]
                self.assertTrue(session.awaiting_goal)
                self.assertEqual(session.goal,'')


if __name__ == '__main__':
    unittest.main()
