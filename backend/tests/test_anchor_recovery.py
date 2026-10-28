"""Trusted before snapshots and explicit recovery after protocol failures."""
import asyncio
import unittest
import unicodedata
from unittest.mock import patch

import httpx

from app.agent.loop import run_loop
from app.agent.memory import load_experience, save_experience
from app.agent.ports import SourceText
from app.agent.store import InMemoryStateStore
from fakes import FakeLLM
import test_agent_session_policy as policy
import test_answer_source_recovery as recovery
from app.business_service import BusinessService


class AnchorSnapshotTests(unittest.TestCase):
    def setup_loop(self):
        text = '项目经历\n我负责⽤Python开发查询模块。\n'
        repo = policy.make_repo(direction='开发实习', direction_confirmed=True, markdown=text)
        repo.sources = [SourceText(source_id='src_1', kind='resume', text=text)]
        store = InMemoryStateStore()
        policy.seed_experience(store, asked='你负责哪部分？', slot='responsibility',
                               answered=True, effect=True)
        doc, revision = load_experience(store, policy.SESSION, 'exp_01')
        doc.raw_text = text
        save_experience(store, policy.SESSION, doc, revision)
        args = {'operation': 'replace', 'target': 'exp_01',
                'after': text.replace('开发查询模块', '实现查询模块'),
                'reason': '整理明确的个人动作', 'gaps': [],
                'source_refs': [{'source_id': 'src_1', 'source_revision': 1, 'quote': text}]}
        return text, repo, store, args

    def test_omitted_before_uses_exact_snapshot_and_does_not_write_draft(self):
        text, repo, store, args = self.setup_loop()
        llm = FakeLLM(decide_script=[{'action': 'tool', 'tool': 'propose_change', 'args': args}, policy.FINISH])
        result = asyncio.run(run_loop(run_id='snapshot', session_id=policy.SESSION,
                                      repo=repo, store=store, llm=llm,
                                      focus_experience_id='exp_01'))
        self.assertEqual(result.outcome, 'waiting_user')
        self.assertEqual(repo.suggestions[0].before, text)
        self.assertEqual(result.steps[0].args['before'], text)
        self.assertEqual(repo.draft_blocks[0].markdown, text)

    def test_explicitly_wrong_before_stays_rejected_and_records_bounded_evidence(self):
        for before_kind in ('normalized', 'wrong_text'):
            with self.subTest(kind=before_kind):
                text, repo, store, args = self.setup_loop()
                before = unicodedata.normalize('NFKC', text) if before_kind == 'normalized' else '另一段原文'
                args['before'] = before
                decision = {'action': 'tool', 'tool': 'propose_change', 'args': args}
                llm = FakeLLM(decide_script=[decision, decision])
                result = asyncio.run(run_loop(run_id='mismatch', session_id=policy.SESSION,
                                              repo=repo, store=store, llm=llm,
                                              focus_experience_id='exp_01'))
                warnings = [e.payload for e in result.events
                            if e.type == 'warning' and e.payload.get('code') == 'stale_anchor']
                self.assertEqual(len(warnings), 2)
                evidence = warnings[0]['anchor_check']
                self.assertEqual(evidence['expected_characters'], len(text.strip()))
                self.assertEqual(evidence['provided_characters'], len(before.strip()))
                self.assertNotIn('expected_text', evidence)
                self.assertNotIn('provided_text', evidence)
                self.assertNotEqual(evidence['expected_codepoint'], evidence['provided_codepoint'])
                self.assertEqual(repo.suggestions, [])
                self.assertEqual(repo.draft_blocks[0].markdown, text)

    def test_omitted_before_does_not_bypass_target_or_personal_evidence_checks(self):
        for violation in ('scope_violation', 'unsupported_section_claim'):
            with self.subTest(violation=violation):
                text, repo, store, args = self.setup_loop()
                if violation == 'scope_violation':
                    args['target'] = 'foreign-block'
                else:
                    args['after'] += '\n已取得教师资格证。'
                decision = {'action': 'tool', 'tool': 'propose_change', 'args': args}
                llm = FakeLLM(decide_script=[decision, decision])
                result = asyncio.run(run_loop(run_id='protected', session_id=policy.SESSION,
                                              repo=repo, store=store, llm=llm,
                                              focus_experience_id='exp_01'))
                self.assertTrue(any(e.type == 'warning' and e.payload.get('code') == violation
                                    for e in result.events))
                self.assertEqual(repo.suggestions, [])
                self.assertEqual(repo.draft_blocks[0].markdown, text)


class AnchorRecoveryClient(recovery.RecoveryClient):
    async def decide(self, **kwargs):
        result = await super().decide(**kwargs)
        if result.data.get('tool') == 'propose_change':
            if self.runtime.host.fail_anchor:
                result.data['args']['before'] = '模型只复制了片段，而不是当前整段'
                self.proposed = False
            else:
                result.data['args'].pop('before', None)
        return result


class AnchorRecoveryTests(unittest.TestCase):
    setUp = recovery.RecoveryTests.setUp
    request = recovery.RecoveryTests.request
    version = recovery.RecoveryTests.version
    chat = recovery.RecoveryTests.chat
    material = recovery.RecoveryTests.material
    send = recovery.RecoveryTests.send
    analyze = recovery.RecoveryTests.analyze
    conversation = recovery.RecoveryTests.conversation
    count = recovery.RecoveryTests.count
    ready = recovery.RecoveryTests.ready
    turn = recovery.RecoveryTests.turn
    pending = recovery.RecoveryTests.pending
    setup_recovery = recovery.RecoveryTests.setup_recovery

    def test_continue_retries_protocol_failure_using_saved_answers_on_same_target(self):
        with patch.object(httpx.AsyncHTTPTransport, 'handle_async_request',
                          side_effect=AssertionError('Offline: network forbidden')):
            session, host, ids = self.setup_recovery()
            host.fail_anchor = True
            host.client_factory = lambda runtime, budget: AnchorRecoveryClient(runtime)
            self.turn(session, recovery.EVIDENCE, 'evidence')
            value = self.conversation(session)
            original = value['draft']
            self.assertIn('回复“继续”', value['messages'][-1]['content'])
            self.assertNotIn('before', value['messages'][-1]['content'])
            self.assertEqual(self.pending(session), [])
            calls = host.calls
            host.fail_anchor = False
            user, events = self.turn(session, '继续', 'retry-current')
            self.assertGreater(host.calls, calls)
            self.assertEqual(events[-1]['payload']['outcome'], 'waiting_user')
            suggestion = self.pending(session)[0]
            self.assertEqual(suggestion['target']['blockId'], ids[0])
            self.assertEqual(suggestion['before'], original['blocks'][0]['markdown'])
            self.assertIn('sqlite3', suggestion['after'])
            self.assertTrue(any(recovery.ANSWER in r['quote'] for r in suggestion['sourceRefs']))
            self.assertEqual(self.conversation(session)['draft'], original)
            self.assertEqual(self.count('suggestion_decisions'), 0)
            questions = [m['content'] for m in self.conversation(session)['messages']
                         if m['role'] == 'assistant' and (m.get('assistant') or {}).get('kind') == 'question']
            self.assertEqual(sum(q.startswith(recovery.QUESTION) for q in questions), 1)
            calls = host.calls
            self.analyze(session, user, 'retry-same-message')
            self.assertEqual(host.calls, calls)
            self.turn(session, '继续', 'still-pending')
            self.assertEqual(host.calls, calls)
            self.assertEqual(self.count('suggestion_decisions'), 0)
            self.assertEqual(self.pending(session)[0]['suggestionId'], suggestion['suggestionId'])

    def test_hand_edit_during_generation_cannot_be_overwritten_by_snapshot_binding(self):
        class EditingClient(AnchorRecoveryClient):
            async def decide(self, **kwargs):
                result = await super().decide(**kwargs)
                if result.data.get('tool') == 'propose_change':
                    runtime = self.runtime
                    current = runtime.repo.get_session_context(runtime.context)
                    markdown = current['draft']['markdown'] + '\n用户手动修改标记。'
                    runtime.host.manual_markdown = markdown
                    BusinessService(runtime.repo).update_draft(runtime.sid, {
                        'requestId': 'manual-edit-during-generation',
                        'expectedStateRevision': current['session']['stateRevision'],
                        'markdown': markdown,
                    })
                return result

        with patch.object(httpx.AsyncHTTPTransport, 'handle_async_request',
                          side_effect=AssertionError('Offline: network forbidden')):
            session, host, _ = self.setup_recovery()
            host.fail_anchor = False
            host.client_factory = lambda runtime, budget: EditingClient(runtime)
            self.turn(session, recovery.EVIDENCE, 'evidence-with-concurrent-edit')
            self.assertEqual(self.conversation(session)['draft']['markdown'], host.manual_markdown)
            self.assertEqual(self.pending(session), [])
            self.assertEqual(self.count('suggestion_decisions'), 0)


if __name__ == '__main__':
    unittest.main()
