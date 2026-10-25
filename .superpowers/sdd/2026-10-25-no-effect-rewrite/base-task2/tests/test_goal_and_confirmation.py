"""JD target propagation and unambiguous approval using isolated, scripted clients."""
from types import SimpleNamespace
import unittest
from app import db

from app.repository import Repository, RequestContext
from app.business_service import BusinessService
from app.agent_goal import title_from_jd, current_jd
from app.agent.memory import SessionDoc, load_session, save_session
from app.agent.store import InMemoryStateStore
import test_agent_integration as integration
import test_assistant_replies
import test_agent_session_policy as policy

JD = '临床内科医师JD\n岗位职责：规范书写病历，负责住院患者的诊疗。\n任职要求：有执业医师资格证。'
RESUME = '项目经历：整理反馈，协助测试。'


class TargetClient(integration.FakeClient):
    async def complete_json(self, *, system, user, run_id='test'):
        if system.startswith('解析用户对改写'):
            self.runtime.host.decision_calls += 1
            return SimpleNamespace(data={'status': 'clarify', 'decisions': []})
        return await super().complete_json(system=system, user=user, run_id=run_id)

    async def decide(self, **kwargs):
        self.runtime.host.decision_contexts.append(kwargs['user'])
        return await super().decide(**kwargs)


class GoalAndConfirmationTests(unittest.TestCase):
    setUp = integration.AgentIntegrationTests.setUp
    request = integration.AgentIntegrationTests.request
    version = integration.AgentIntegrationTests.version
    chat = integration.AgentIntegrationTests.chat
    material = integration.AgentIntegrationTests.material
    send = integration.AgentIntegrationTests.send
    analyze = integration.AgentIntegrationTests.analyze
    conversation = integration.AgentIntegrationTests.conversation
    count = integration.AgentIntegrationTests.count
    run_context = test_assistant_replies.AssistantReplyTests.run_context

    def ready(self, with_jd=True):
        session = self.chat(key=f"create-{self.count('sessions')}")
        self.material(session, text=RESUME, role='resume')
        if with_jd:
            self.material(session, text=JD, role='jd', request_id='jd')
        host = self.app.state.agent_host
        host.client_factory = lambda runtime, budget: TargetClient(runtime)
        host.decision_calls, host.decision_contexts = 0, []
        user = self.send(session, '请根据已上传的JD优化简历。')
        events = self.analyze(session, user)
        if with_jd:
            for attempt in range(3):
                last = self.conversation(session)['messages'][-1]
                if (last.get('assistant') or {}).get('kind') != 'question':
                    break
                text = '老师录入时间从半小时缩短到五分钟。' if '实际变化' in last['content'] else '这部分由我独立负责。'
                answer = self.send(session, text, request_id=f'segment-{attempt}', replyToMessageId=last['messageId'])
                events = self.analyze(session, answer, f'segment-{attempt}')
        return session, user, host, events

    def proposal(self):
        session, _, host, events = self.ready()
        value = self.conversation(session)
        self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user', events)
        prompt = value['messages'][-1]
        self.assertEqual(prompt['assistant']['kind'], 'change_question', value)
        trigger = next(m for m in reversed(value['messages']) if m['role'] == 'user')
        return session, trigger, host, prompt

    def test_single_jd_fills_target_and_reaches_decision_with_jd_text(self):
        session, _, host, _ = self.ready()
        value = self.conversation(session)
        self.assertEqual(value['session']['targetDirection'], '临床内科医师')
        self.assertEqual(value['session']['directionStatus'], 'provided')
        self.assertFalse(any('这次想投什么岗位' in m['content'] for m in value['messages']))
        self.assertTrue(host.decision_contexts)
        self.assertIn(JD, host.decision_contexts[0])
        self.assertIn('不能当作用户经历', host.decision_contexts[0])
        self.assertIn('目标：临床内科医师', host.decision_contexts[0])
        self.assertEqual(value['draft']['markdown'], RESUME)
        self.assertNotIn('执业医师资格证', value['draft']['markdown'])
        self.assertEqual(Repository(self.path).get_session(RequestContext(session['sessionId']))['targetDirection'], '临床内科医师')

    def test_without_jd_still_asks_missing_goal(self):
        session, _, host, events = self.ready(False)
        self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user')
        self.assertIn('这次想投什么岗位', self.conversation(session)['messages'][-1]['content'])
        self.assertEqual(host.decision_contexts, [])

    def test_goal_answer_announces_first_segment_before_its_question(self):
        session, _, host, _ = self.ready(False)
        goal = self.send(session, '临床内科医师', request_id='goal-answer')
        self.analyze(session, goal, 'goal-answer')
        messages = self.conversation(session)['messages']
        self.assertEqual(messages[-2]['role'], 'assistant')
        self.assertIn('现在进入第 1 段', messages[-2]['content'])
        self.assertEqual(messages[-1]['assistant']['kind'], 'question')

    def test_yes_accepts_unique_suggestion_without_model_parsing_and_replays_once(self):
        for word, explicit in [('是', False), ('是的！', True), ('可以。', True)]:
            with self.subTest(word=word):
                session, _, host, prompt = self.proposal()
                model_steps = len(host.decision_contexts)
                kwargs = {'replyToMessageId': prompt['messageId']} if explicit else {}
                answer = self.send(session, word, request_id='yes', **kwargs)
                events = self.analyze(session, answer, 'yes')
                self.assertEqual(events[-1]['payload']['outcome'], 'completed', events)
                self.assertEqual(self.conversation(session)['draft']['markdown'], '协助整理反馈。')
                self.assertEqual(host.decision_calls, 0)
                self.assertEqual(len(host.decision_contexts), model_steps)
                before = self.count('suggestion_decisions')
                self.analyze(session, answer, 'yes-replay')
                self.assertEqual(self.count('suggestion_decisions'), before)

    def test_legacy_pending_direction_is_synced_only_after_valid_acceptance(self):
        session, _, host, prompt = self.proposal()
        # Reproduce the old release's state: a valid pending suggestion with A's goal unset.
        with db.database(self.path) as c:
            c.execute("UPDATE sessions SET direction='新对话',direction_status='pending' WHERE session_id=?", (session['sessionId'],))
        answer = self.send(session, '是', request_id='legacy-yes', replyToMessageId=prompt['messageId'])
        events = self.analyze(session, answer, 'legacy-yes')
        self.assertEqual(events[-1]['payload']['outcome'], 'completed', events)
        value = self.conversation(session)
        self.assertEqual(value['draft']['markdown'], '协助整理反馈。')
        self.assertEqual(value['session']['targetDirection'], '临床内科医师')
        self.assertEqual(self.count('suggestion_decisions'), 1)
        self.assertEqual(host.decision_calls, 0)

    def test_multi_suggestion_yes_asks_instead_of_selecting_last(self):
        session, trigger, host, _ = self.proposal()
        repo = Repository(self.path)
        context = self.run_context(session)
        data = repo.get_session_context(context)
        first = data['suggestions'][0]; block = data['draft']['blocks'][0]
        second = repo.save_suggestion(context, {'operation': 'replace', 'target': first['target'], 'before': block['markdown'],
            'after': '参与反馈整理与测试。', 'reason': '构造另一个有效候选', 'gaps': [], 'sourceRefs': first['sourceRefs']})
        prompt = BusinessService(repo).save_assistant_message(context, {'requestId': 'multi', 'userMessageId': trigger['messageId'],
            'content': '两条候选，请选择。', 'kind': 'change_question', 'suggestionIds': [first['suggestionId'], second['suggestionId']]}, synthetic=True)['data']['message']
        answer = self.send(session, '是', request_id='multi-yes', replyToMessageId=prompt['messageId'])
        self.analyze(session, answer, 'multi-yes')
        self.assertEqual(self.count('suggestion_decisions'), 0)
        self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)
        self.assertEqual(host.decision_calls, 0)
        self.assertEqual(self.conversation(session)['messages'][-1]['assistant']['kind'], 'change_question')

    def test_yes_among_multiple_question_types_needs_an_explicit_reply_target(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                session, trigger, host, _ = self.proposal()
                repo = Repository(self.path)
                context = self.run_context(session)
                proposal = repo.get_session_context(context)['suggestions'][0]
                service = BusinessService(repo)
                change = service.save_assistant_message(context, {'requestId': 'mixed-change', 'userMessageId': trigger['messageId'],
                    'content': '是否采用这条改写？', 'kind': 'change_question', 'suggestionIds': [proposal['suggestionId']]}, synthetic=True)['data']['message']
                service.save_assistant_message(context, {'requestId': 'mixed-other', 'userMessageId': trigger['messageId'],
                    'content': '这份资料是否仅供参考？', 'kind': 'question', 'suggestionIds': []}, synthetic=True)
                kwargs = {'replyToMessageId': change['messageId']} if explicit else {}
                answer = self.send(session, '是', request_id='mixed-yes', **kwargs)
                self.analyze(session, answer, 'mixed-yes')
                if explicit:
                    self.assertEqual(self.conversation(session)['draft']['markdown'], '协助整理反馈。')
                else:
                    self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)
                    self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_conditional_or_quoted_yes_is_not_a_direct_accept(self):
        for text in ('是，但先不要修改', '他说“是”，我还没决定', '是吗？', '不是'):
            with self.subTest(text=text):
                session, _, host, prompt = self.proposal()
                answer = self.send(session, text, request_id='conditional', replyToMessageId=prompt['messageId'])
                self.analyze(session, answer, 'conditional')
                self.assertEqual(self.conversation(session)['draft']['markdown'], RESUME)
                self.assertGreater(host.decision_calls, 0)

    def test_edited_draft_cannot_be_overwritten_by_yes_to_expired_prompt(self):
        session, _, host, prompt = self.proposal()
        repo = Repository(self.path)
        repo.update_draft(RequestContext(session['sessionId'], self.version(session['sessionId'])), '用户手改：整理反馈并测试。')
        # Old prompt links remain valid historical references, never write authority.
        result = self.request('POST', f"/api/sessions/{session['sessionId']}/chat/messages", data={
            'requestId': 'stale-yes', 'expectedStateRevision': self.version(session['sessionId']), 'content': '是',
            'attachmentIds': [], 'replyToMessageId': prompt['messageId']})
        self.assertEqual(result['status'], 201)
        answer = result['json']['data']['message']
        self.analyze(session, answer, 'stale-yes')
        self.assertEqual(self.conversation(session)['draft']['markdown'], '用户手改：整理反馈并测试。')
        self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_multiple_jds_are_not_silently_chosen_as_one_target(self):
        session = self.chat()
        self.material(session, text=JD, role='jd')
        self.material(session, text='Python开发实习生JD\n岗位职责：实现接口。', role='jd', request_id='second-jd')
        self.assertIsNone(current_jd(Repository(self.path), RequestContext(session['sessionId'])))

    def test_explicit_user_direction_is_not_overwritten_by_jd(self):
        session, _, host, _ = self.ready()
        with db.database(self.path) as c:
            c.execute("UPDATE sessions SET direction='软件开发实习生',direction_status='provided' WHERE session_id=?", (session['sessionId'],))
        # A provided direction is authoritative, even when it differs from the JD.
        from app.agent_goal import sync
        runtime = SimpleNamespace(repo=Repository(self.path), sid=session['sessionId'], context=RequestContext(session['sessionId']))
        self.assertIsNone(sync(runtime))
        self.assertEqual(runtime.repo.get_session(runtime.context)['targetDirection'], '软件开发实习生')


class GoalMemoryTests(unittest.TestCase):
    def test_canonical_target_replaces_stale_agent_goal(self):
        repo, store = policy.make_repo(direction='软件开发实习生', direction_confirmed=True), InMemoryStateStore()
        save_session(store, policy.SESSION, SessionDoc(direction='旧岗位', goal='旧岗位'), None)
        llm = policy.FakeLLM(route_script=[policy.ANSWER_ROUTE], decide_script=[policy.FINISH])
        policy.run_turn(repo, store, llm, '继续分析')
        doc, _ = load_session(store, policy.SESSION)
        self.assertEqual(doc.direction, '软件开发实习生')
        self.assertEqual(doc.goal, '软件开发实习生')

    def test_previously_awaiting_goal_stops_asking_when_direction_is_available(self):
        repo, store = policy.make_repo(direction='临床内科医师', direction_confirmed=True), InMemoryStateStore()
        save_session(store, policy.SESSION, SessionDoc(goal_asked=True, awaiting_goal=True), None)
        llm = policy.FakeLLM(route_script=[policy.ANSWER_ROUTE], decide_script=[policy.FINISH])
        events = policy.run_turn(repo, store, llm, '继续分析')
        doc, _ = load_session(store, policy.SESSION)
        self.assertEqual(doc.goal, '临床内科医师')
        self.assertFalse(doc.awaiting_goal)
        self.assertFalse(any(e.type == 'question' and e.payload.get('question_id') == 'goal' for e in events))

    def test_generic_jd_title_does_not_invent_a_role(self):
        self.assertEqual(title_from_jd('招聘要求\n开发接口并维护服务'), '按已提供的岗位描述优化')
        self.assertEqual(title_from_jd('职位名称：Python开发实习生\n熟悉Flask'), 'Python开发实习生')


if __name__ == '__main__':
    unittest.main()
