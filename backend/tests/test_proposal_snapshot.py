"""Immutable generation inputs and atomic persistence; no provider requests."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from app.agent.adapters.a_repo import ARepoAdapter
from app.agent.loop import run_loop
from app.agent.ports import (RepoError, SourceRef, SourceText, capture_proposal_snapshot,
                             verify_proposal_snapshot)
from app.agent.schemas import ToolStep
from app.agent.tools import ToolContext, execute_tool
from app.material_tools import MaterialTools
from app.repository import RepositoryError
from fakes import FakeLLM
import app.agent.loop as loop_module
import test_anchor_recovery as anchors
import test_agent_session_policy as policy
import test_draft_actions as actions


class SnapshotValueTests(unittest.TestCase):
    def test_snapshot_is_deeply_immutable_and_detached_from_context(self):
        _, repo, _, _ = anchors.AnchorSnapshotTests().setup_loop()
        ctx = repo.get_session_context(policy.SESSION)
        snapshot = capture_proposal_snapshot(ctx, 'exp_01', run_id='input')
        before, versions, fingerprint = snapshot.before, snapshot.source_versions, snapshot.fingerprint
        with self.assertRaises(FrozenInstanceError):
            snapshot.before = 'new text'
        with self.assertRaises(TypeError):
            snapshot.source_versions[0][1] = 99
        ctx.draft_blocks[0].markdown = 'changed context'
        ctx.sources[0].source_revision += 1
        self.assertEqual(snapshot.before, before)
        self.assertEqual(snapshot.source_versions, versions)
        self.assertEqual(snapshot.fingerprint, fingerprint)
        with self.assertRaises(RepoError):
            verify_proposal_snapshot(snapshot, ctx, policy.SESSION, 'exp_01', run_id='input')

    def test_model_cannot_supply_its_own_snapshot_and_missing_snapshot_is_rejected(self):
        _, repo, store, args = anchors.AnchorSnapshotTests().setup_loop()
        args['snapshot'] = {'session_id': policy.SESSION, 'block_id': 'exp_01'}
        step = ToolStep(action='tool', tool='propose_change', args=args)
        with self.assertRaises(RepoError) as caught:
            asyncio.run(execute_tool(step, ToolContext(policy.SESSION, repo, store, run_id='input')))
        self.assertEqual(caught.exception.code, 'missing_snapshot')
        self.assertEqual(repo.suggestions, [])

    def test_foreign_target_session_and_run_are_rejected(self):
        _, repo, _, _ = anchors.AnchorSnapshotTests().setup_loop()
        ctx = repo.get_session_context(policy.SESSION)
        snapshot = capture_proposal_snapshot(ctx, 'exp_01', run_id='input')
        for session, target, run_id in ((policy.SESSION, 'other', 'input'),
                                       ('foreign', 'exp_01', 'input'),
                                       (policy.SESSION, 'exp_01', 'new-run')):
            with self.subTest(session=session, target=target, run_id=run_id), self.assertRaises(RepoError):
                verify_proposal_snapshot(snapshot, ctx, session, target, run_id=run_id)


class SnapshotLoopTests(unittest.TestCase):
    def test_input_changes_during_generation_never_create_a_rebound_proposal(self):
        def change_source_revision(repo):
            repo.sources[0].source_revision += 1  # Same quote, different immutable version.

        changes = {
            'text_without_revision': lambda r: setattr(r.draft_blocks[0], 'markdown', '手动改稿'),
            'block_revision_same_text': lambda r: setattr(r.draft_blocks[0], 'block_revision', 2),
            'session_revision': lambda r: setattr(r.session, 'state_revision', 2),
            'draft_revision': lambda r: setattr(r.session, 'draft_revision', 2),
            'resume_generation': lambda r: setattr(r.session, 'current_resume_generation', 2),
            'source_revision_same_text': change_source_revision,
            'source_role': lambda r: setattr(r.sources[0], 'kind', 'jd'),
            'source_removed': lambda r: r.sources.clear(),
            'source_added': lambda r: r.sources.append(SourceText(source_id='new', text='新补充')),
        }
        for name, mutate in changes.items():
            with self.subTest(change=name):
                _, repo, store, args = anchors.AnchorSnapshotTests().setup_loop()

                class ChangingModel(FakeLLM):
                    async def decide(self, **kwargs):
                        completion = await super().decide(**kwargs)
                        mutate(repo)
                        return completion

                llm = ChangingModel(decide_script=[{'action': 'tool', 'tool': 'propose_change', 'args': args}])
                result = asyncio.run(run_loop(run_id='input', session_id=policy.SESSION,
                                              repo=repo, store=store, llm=llm,
                                              focus_experience_id='exp_01'))
                self.assertEqual(result.outcome, 'limited')
                self.assertEqual(repo.suggestions, [])
                self.assertTrue(any(e.type == 'warning' and e.payload.get('code') == 'stale_state'
                                    for e in result.events))

    def test_exact_object_captured_before_model_reaches_persistence(self):
        text, repo, store, args = anchors.AnchorSnapshotTests().setup_loop()
        captured, saved = [], []
        capture = loop_module.capture_proposal_snapshot
        save = repo.save_suggestion

        def capture_spy(*pos, **kwargs):
            value = capture(*pos, **kwargs)
            captured.append(value)
            return value

        def save_spy(*pos, **kwargs):
            saved.append(kwargs.get('snapshot'))
            return save(*pos, **kwargs)

        llm = FakeLLM(decide_script=[{'action': 'tool', 'tool': 'propose_change', 'args': args}, policy.FINISH])
        with patch.object(loop_module, 'capture_proposal_snapshot', capture_spy), \
             patch.object(repo, 'save_suggestion', save_spy):
            result = asyncio.run(run_loop(run_id='input', session_id=policy.SESSION,
                                          repo=repo, store=store, llm=llm,
                                          focus_experience_id='exp_01'))
        self.assertEqual(len(saved), 1)
        self.assertIs(saved[0], captured[0])
        self.assertEqual(result.steps[0].observation['input_snapshot_id'], captured[0].fingerprint)
        self.assertEqual(repo.suggestions[0].before, text)
        self.assertEqual(repo.draft_blocks[0].markdown, text)

    def test_missing_target_snapshot_does_not_fall_back_to_memory_text(self):
        _, repo, store, args = anchors.AnchorSnapshotTests().setup_loop()
        repo.draft_blocks.clear()
        llm = FakeLLM(decide_script=[{'action': 'tool', 'tool': 'propose_change', 'args': args}])
        result = asyncio.run(run_loop(run_id='input', session_id=policy.SESSION,
                                      repo=repo, store=store, llm=llm,
                                      focus_experience_id='exp_01'))
        self.assertEqual(repo.suggestions, [])
        self.assertTrue(any(e.type == 'warning' and e.payload.get('code') == 'missing_snapshot'
                            for e in result.events))


class SnapshotPersistenceTests(unittest.TestCase):
    setUp = actions.DraftActionsTests.setUp
    request = actions.DraftActionsTests.request
    new_session = actions.DraftActionsTests.new_session
    version = actions.DraftActionsTests.version
    material = actions.DraftActionsTests.material
    message = actions.DraftActionsTests.message
    count = actions.DraftActionsTests.count
    assert_error = actions.DraftActionsTests.assert_error
    setup_case = actions.DraftActionsTests.setup_case
    draft = actions.DraftActionsTests.draft
    decision_payload = actions.DraftActionsTests.decision_payload
    decide = actions.DraftActionsTests.decide
    edit = actions.DraftActionsTests.edit

    def prepared(self, *, extra=False):
        session = self.setup_case()
        extra_source = None
        if extra:
            data = self.material(session, text='额外个人补充。', role='supplement', request_id='extra-source')['json']['data']
            extra_source = data['sources'][0]
        tools = MaterialTools(self.repo, self.ctx)
        adapter = ARepoAdapter(tools=tools, context=self.ctx, session_id=self.sid)
        block = self.draft()['blocks'][1]
        snapshot = capture_proposal_snapshot(adapter.get_session_context(self.sid), block['blockId'])
        return session, tools, adapter, snapshot, extra_source

    def save(self, adapter, snapshot):
        result = adapter.save_suggestion(
            self.sid, snapshot.target_id, 'replace', snapshot.before, '整理并归类8条反馈',
            '根据个人材料整理', [SourceRef(source_id=self.ref['sourceId'],
                                          source_revision=self.ref['sourceRevision'], quote=self.ref['quote'])],
            [], snapshot=snapshot)
        return self.repo.get_suggestion(self.ctx, result.suggestion_id)

    def test_atomic_boundary_rejects_source_change_after_adapter_validation(self):
        _, tools, adapter, snapshot, extra = self.prepared(extra=True)
        original = tools.propose_with_snapshot

        def race(proposal, frozen):
            self.repo.save_source(self.ctx, text=extra['text'], role=extra['role'], origin=extra['origin'],
                                  use_status='active', source_id=extra['sourceId'], expected_source_revision=1)
            return original(proposal, frozen)

        with patch.object(tools, 'propose_with_snapshot', side_effect=race), self.assertRaises(RepoError):
            self.save(adapter, snapshot)
        self.assertEqual(self.count('suggestions'), 0)
        frozen = {sid: revision for sid, revision, _ in snapshot.source_versions}
        self.assertEqual(frozen[extra['sourceId']], 1)
        self.assertEqual(self.repo.read_material(self.ctx, [extra['sourceId']])[0]['sourceRevision'], 2)

    def test_trusted_snapshot_entry_rejects_missing_object(self):
        _, tools, _, snapshot, _ = self.prepared()
        proposal = {'operation': 'replace', 'target': {'blockId': snapshot.block_id,
                    'expectedBlockRevision': snapshot.block_revision}, 'before': snapshot.before,
                    'after': '整理并归类8条反馈', 'reason': '测试', 'sourceRefs': [self.ref], 'gaps': []}
        result = tools.propose_with_snapshot(proposal, None)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'missing_snapshot')
        self.assertEqual(self.count('suggestions'), 0)

    def test_repository_rejects_foreign_snapshot_even_with_valid_proposal_text(self):
        _, _, adapter, snapshot, _ = self.prepared()
        proposal = {'operation': 'replace', 'target': {'blockId': snapshot.block_id,
                    'expectedBlockRevision': snapshot.block_revision}, 'before': snapshot.before,
                    'after': '整理并归类8条反馈', 'reason': '测试', 'sourceRefs': [self.ref], 'gaps': []}
        with self.assertRaises(RepositoryError) as error:
            self.repo.save_suggestion(self.ctx, proposal, snapshot=replace(snapshot, session_id='foreign'))
        self.assertEqual(error.exception.code, 'scope_violation')
        self.assertEqual(self.count('suggestions'), 0)

    def test_same_quote_new_source_version_cannot_be_silently_rebound(self):
        _, _, adapter, snapshot, _ = self.prepared()
        source = self.repo.read_material(self.ctx, [self.ref['sourceId']])[0]
        self.repo.save_source(self.ctx, text=source['text'], role=source['role'], origin=source['origin'],
                              use_status='active', source_id=source['sourceId'], expected_source_revision=1)
        with self.assertRaises(RepoError):
            self.save(adapter, snapshot)
        self.assertEqual(self.count('suggestions'), 0)

    def test_accept_rechecks_target_after_manual_edit(self):
        _, _, adapter, snapshot, _ = self.prepared()
        suggestion = self.save(adapter, snapshot)
        changed = self.draft()['markdown'].replace('负责需求整理', '用户手动修改的需求说明')
        self.assertEqual(self.edit(changed)['status'], 200)
        self.assert_error(self.decide(suggestion, self.decision_payload(suggestion)), 409, 'stale_state')
        self.assertEqual(self.draft()['markdown'], changed)
        self.assertEqual(self.repo.get_suggestion(self.ctx, suggestion['suggestionId'])['before'], snapshot.before)

    def test_accept_rechecks_cited_source_even_when_quote_is_unchanged(self):
        _, _, adapter, snapshot, _ = self.prepared()
        suggestion = self.save(adapter, snapshot)
        source = self.repo.read_material(self.ctx, [self.ref['sourceId']])[0]
        before = self.draft()
        self.repo.save_source(self.ctx, text=source['text'], role=source['role'], origin=source['origin'],
                              use_status='active', source_id=source['sourceId'], expected_source_revision=1)
        self.assert_error(self.decide(suggestion, self.decision_payload(suggestion)), 409, 'stale_state')
        self.assertEqual(self.draft(), before)

    def test_confirmation_message_may_change_session_revision_without_invalidating_same_target(self):
        session, _, adapter, snapshot, _ = self.prepared()
        suggestion = self.save(adapter, snapshot)
        self.assertEqual(self.message(session, content='我先查看这条建议。', request_id='new-message')['status'], 201)
        self.assertGreater(self.version(self.sid), snapshot.state_revision)
        response = self.decide(suggestion, self.decision_payload(suggestion))
        self.assertEqual(response['status'], 200, response)
        self.assertEqual(response['json']['data']['suggestion']['status'], 'accepted')


if __name__ == '__main__':
    unittest.main()
