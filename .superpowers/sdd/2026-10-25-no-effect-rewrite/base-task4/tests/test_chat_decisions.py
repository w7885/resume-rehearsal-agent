"""A-side structured Host checks; no model semantics or real provider calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch

from app import db
from app.business_service import BusinessService
from app.material_tools import MaterialTools
from app.repository import Repository, RepositoryError, RequestContext
import test_assistant_replies


class ChatDecisionTests(unittest.TestCase):
    setUp = test_assistant_replies.AssistantReplyTests.setUp
    request = test_assistant_replies.AssistantReplyTests.request
    version = test_assistant_replies.AssistantReplyTests.version
    material = test_assistant_replies.AssistantReplyTests.material
    count = test_assistant_replies.AssistantReplyTests.count
    run_context = test_assistant_replies.AssistantReplyTests.run_context
    chat = test_assistant_replies.AssistantReplyTests.chat
    send = test_assistant_replies.AssistantReplyTests.send
    save = test_assistant_replies.AssistantReplyTests.save
    conversation = test_assistant_replies.AssistantReplyTests.conversation

    def case(self, *, two=False):
        session = self.chat(key=f"create-{self.count('sessions')}")
        self.material(session, text='整理反馈；协助测试', role='supplement')
        self.service = BusinessService(Repository(self.path))
        repo = self.service.repository
        source = repo.get_session_context(RequestContext(session['sessionId']))['sources'][0]
        ref = {'sourceId': source['sourceId'], 'sourceRevision': 1, 'quote': '整理反馈'}
        repo.create_initial_draft(RequestContext(session['sessionId']), [
            {'markdown': text, 'sourceRefs': [ref]} for text in (['第一段', '第二段'] if two else ['第一段'])])
        trigger = self.send(session, '请核对表达。')
        context = self.run_context(session)
        current = repo.get_session_context(context)
        proposals = [repo.save_suggestion(context, {
            'operation': 'replace', 'target': {'blockId': block['blockId'], 'expectedBlockRevision': block['blockRevision']},
            'before': block['markdown'], 'after': f'构造改写 {index + 1}', 'reason': '构造验证',
            'gaps': [], 'sourceRefs': [ref]}) for index, block in enumerate(current['draft']['blocks'])]
        prompt = self.save(context, trigger, 'change_question', [p['suggestionId'] for p in proposals])
        return session, trigger, context, current, proposals, prompt

    def answer(self, session, prompt, text='采用第一条。'):
        user = self.send(session, text, request_id='answer', replyToMessageId=prompt['messageId'])
        return user, self.run_context(session)

    def payload(self, user, prompt, proposals, actions=None):
        actions = actions or ['accept'] * len(proposals)
        return {'requestId': 'decision', 'userMessageId': user['messageId'], 'userContent': user['content'],
                'status': 'resolved', 'decisions': [
                    {'promptMessageId': prompt['messageId'], 'suggestionId': p['suggestionId'],
                     'decision': action, 'feedback': user['content'] if action == 'revise' else ''}
                    for p, action in zip(proposals, actions)]}

    def apply(self, context, payload):
        return self.service.apply_resolved_user_decision(context, payload, synthetic=True)

    def error(self, context, payload, code=None):
        with self.assertRaises(RepositoryError) as caught:
            self.apply(context, payload)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_accept_bridges_reply_and_reuses_draft_transaction_with_receipt(self):
        session, _, _, before, proposals, prompt = self.case()
        user, context = self.answer(session, prompt)
        result = self.apply(context, self.payload(user, prompt, proposals))['data']
        after = self.conversation(session)
        self.assertEqual(after['draft']['markdown'], '构造改写 1')
        self.assertEqual(result['stateRevision'], context.expected_state_revision + 1)
        self.assertEqual(result['draftRevision'], before['draft']['draftRevision'] + 1)
        self.assertEqual(self.count('suggestion_decisions'), 1)
        self.assertEqual(self.count('chat_decision_resolutions'), 1)
        self.assertEqual(self.count('source_blocks'), 1)
        self.assertEqual(after['messages'][-1]['content'], user['content'])
        self.assertEqual(after['draft']['blocks'][0]['sourceRefs'], proposals[0]['sourceRefs'])
        with db.database(self.path, read_only=True) as connection:
            self.assertEqual(connection.execute('SELECT status FROM runs WHERE run_id = ?', (context.run_id,)).fetchone()[0], 'interrupted')
        self.assertFalse(after['capabilities']['decisionExecutionAvailable'])

    def test_none_and_clarify_never_apply_even_if_raw_text_contains_accept_word(self):
        for status in ('none', 'clarify'):
            with self.subTest(status=status):
                session, _, _, before, proposals, prompt = self.case()
                user, context = self.answer(session, prompt, '他说“采用”，但我还没决定。')
                payload = {**self.payload(user, prompt, proposals), 'status': status, 'decisions': []}
                receipt = self.apply(context, payload)['data']
                self.assertEqual(receipt['stateRevision'], context.expected_state_revision)
                self.assertEqual(self.conversation(session)['draft'], before['draft'])
                self.assertEqual(self.count('suggestion_decisions'), 0)
                self.error(context, {**self.payload(user, prompt, proposals), 'requestId': 'changed'}, 'request_conflict')

    def test_reject_and_revise_leave_text_unchanged_and_keep_exact_feedback(self):
        session, _, _, before, proposals, prompt = self.case(two=True)
        user, context = self.answer(session, prompt, '第一条不采用，第二条请保留原来的职责。')
        self.apply(context, self.payload(user, prompt, proposals, ['reject', 'revise']))
        self.assertEqual(self.conversation(session)['draft'], before['draft'])
        with db.database(self.path, read_only=True) as connection:
            rows = connection.execute('SELECT decision,feedback FROM suggestion_decisions ORDER BY rowid').fetchall()
        self.assertEqual([r[0] for r in rows], ['reject', 'revise'])
        self.assertEqual(rows[1][1], user['content'])

    def test_multiple_selected_blocks_one_state_change_and_unselected_stay_pending(self):
        session, _, _, before, proposals, prompt = self.case(two=True)
        user, context = self.answer(session, prompt, '两条都采用。')
        result = self.apply(context, self.payload(user, prompt, proposals))['data']
        self.assertEqual(result['stateRevision'], context.expected_state_revision + 1)
        self.assertEqual(self.count('suggestion_decisions'), 2)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '构造改写 1\n\n构造改写 2')

    def test_partial_accept_only_changes_selected_suggestion(self):
        session, _, _, before, proposals, prompt = self.case(two=True)
        user, context = self.answer(session, prompt)
        self.apply(context, self.payload(user, prompt, proposals[:1]))
        self.assertEqual(self.conversation(session)['draft']['markdown'], '构造改写 1\n\n第二段')
        self.assertEqual(self.service.repository.get_suggestion(RequestContext(session['sessionId']), proposals[1]['suggestionId'])['status'], 'pending')

    def test_two_alternatives_cannot_both_be_accepted_but_reject_then_accept_is_atomic(self):
        session, _, context, before, proposals, prompt = self.case()
        original = proposals[0]
        alternate = self.service.repository.save_suggestion(context, {key: original[key] for key in ('operation', 'target', 'before', 'after', 'reason', 'sourceRefs', 'gaps')})
        prompt = self.save(context, self.conversation(session)['messages'][0], 'change_question', [original['suggestionId'], alternate['suggestionId']], request_id='both')
        user, context = self.answer(session, prompt)
        payload = self.payload(user, prompt, [original, alternate])
        self.error(context, payload, 'invalid_input')
        self.assertEqual(self.count('chat_decision_resolutions'), 0)
        payload['decisions'][1]['decision'] = 'reject'
        self.apply(context, payload)
        self.assertEqual(self.count('suggestion_decisions'), 2)

    def test_old_manual_edit_before_or_after_reply_blocks_execution(self):
        session, _, _, _, proposals, prompt = self.case()
        self.service.update_draft(session['sessionId'], {'requestId': 'manual', 'expectedStateRevision': self.version(session['sessionId']), 'markdown': '用户手改正文'})
        user, context = self.answer(session, prompt)
        self.error(context, self.payload(user, prompt, proposals))
        self.assertEqual(self.conversation(session)['draft']['markdown'], '用户手改正文')
        self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_extra_input_upload_source_change_and_new_question_invalidate_bridge(self):
        for event in ('input', 'upload', 'source', 'question', 'edit', 'replacement'):
            with self.subTest(event=event):
                session, _, _, before, proposals, prompt = self.case()
                user, context = self.answer(session, prompt)
                if event == 'input':
                    self.send(session, '再等等', request_id='later')
                elif event == 'upload':
                    self.material(session, text='补充材料', request_id='later', role='unknown')
                elif event == 'source':
                    source = self.service.repository.get_session_context(context)['sources'][0]
                    self.service.repository.save_source(RequestContext(session['sessionId']), text='修正原文', role=source['role'], origin=source['origin'], use_status='active', source_id=source['sourceId'], expected_source_revision=1)
                elif event == 'question':
                    self.save(context, user, 'question', request_id='later')
                elif event == 'edit':
                    self.service.update_draft(session['sessionId'], {'requestId': 'edit', 'expectedStateRevision': self.version(session['sessionId']), 'markdown': '手改'})
                else:
                    material = self.material(session, text='新版', request_id='new-resume', role='resume')['json']['data']['material']
                    self.service.replace_draft(session['sessionId'], {'requestId': 'replace', 'expectedStateRevision': self.version(session['sessionId']), 'expectedDraftRevision': before['draft']['draftRevision'], 'materialId': material['materialId'], 'authorizationMessageId': user['messageId'], 'confirmed': True, 'samePersonConfirmed': True, 'completeConfirmed': True})
                self.error(self.run_context(session), self.payload(user, prompt, proposals))
                self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_no_default_last_prompt_explicit_scope_and_exact_raw_content_checked(self):
        session, trigger, context, _, proposals, prompt = self.case(two=True)
        other = self.save(context, trigger, 'change_question', [proposals[1]['suggestionId']], request_id='other')
        user, context = self.answer(session, prompt)
        payload = self.payload(user, other, proposals[1:])
        self.error(context, payload, 'scope_violation')
        payload = self.payload(user, prompt, proposals[:1])
        self.error(context, {**payload, 'userContent': '助手捏造的同意'}, 'scope_violation')
        payload['decisions'][0]['feedback'] = '助手新增条件'
        self.error(context, payload, 'invalid_input')
        self.assertEqual(self.count('chat_decision_resolutions'), 0)

    def test_unselected_prompt_requires_b_to_supply_real_scope_not_last_guess(self):
        session, trigger, context, _, proposals, prompt = self.case(two=True)
        other = self.save(context, trigger, 'change_question', [proposals[1]['suggestionId']], request_id='other')
        user = self.send(session, '采用第一条。', request_id='answer')
        context = self.run_context(session)
        payload = self.payload(user, prompt, proposals[:1])
        self.apply(context, payload)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '构造改写 1\n\n第二段')

    def test_lost_response_concurrent_retry_and_durable_consumption_after_request_expiry(self):
        session, _, _, _, proposals, prompt = self.case()
        user, context = self.answer(session, prompt)
        payload = self.payload(user, prompt, proposals)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.apply(context, payload), range(2)))
        self.assertEqual(results[0], results[1])
        with db.database(self.path) as connection:
            connection.execute('DELETE FROM request_log')
        self.assertEqual(self.apply(context, {**payload, 'requestId': 'after-expiry'}), results[0])
        changed = json.loads(json.dumps(payload)); changed['requestId'] = 'changed'; changed['decisions'][0]['decision'] = 'reject'
        self.error(context, changed, 'request_conflict')
        self.error(self.run_context(session), {**payload, 'requestId': 'other-run'}, 'request_conflict')
        self.assertEqual(self.count('suggestion_decisions'), 1)

    def test_failure_after_draft_write_rolls_back_all_business_and_consumption(self):
        session, _, _, before, proposals, prompt = self.case()
        user, context = self.answer(session, prompt)
        payload = self.payload(user, prompt, proposals)
        real = json.dumps
        def fail(value, *args, **kwargs):
            if isinstance(value, dict) and value.get('ok') and value.get('data', {}).get('status') == 'resolved':
                raise OSError('construct response cache failure')
            return real(value, *args, **kwargs)
        with patch('app.business_service.json.dumps', side_effect=fail), self.assertRaises(OSError):
            self.apply(context, payload)
        self.assertEqual(self.conversation(session)['draft'], before['draft'])
        self.assertEqual(self.count('suggestion_decisions'), 0)
        self.assertEqual(self.count('chat_decision_resolutions'), 0)
        self.assertEqual(self.version(session['sessionId']), context.expected_state_revision)
        self.apply(context, payload)

    def test_later_target_failure_rolls_back_prior_selected_write(self):
        session, _, _, before, proposals, prompt = self.case(two=True)
        user, context = self.answer(session, prompt)
        real = Repository.apply_suggestion_decision
        calls = []
        def fail(repo, *args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('construct second write failure')
            return real(repo, *args)
        with patch.object(Repository, 'apply_suggestion_decision', fail), self.assertRaises(OSError):
            self.apply(context, self.payload(user, prompt, proposals))
        self.assertEqual(self.conversation(session)['draft'], before['draft'])
        self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_fresh_run_can_report_own_saved_outcome_but_not_after_unrelated_edit(self):
        session, _, _, _, proposals, prompt = self.case()
        user, context = self.answer(session, prompt)
        self.apply(context, self.payload(user, prompt, proposals))
        new_context = self.run_context(session)
        turn = self.service.get_chat_turn_context(new_context, user['messageId'])
        self.assertEqual(turn['decisionReceipt']['status'], 'resolved')
        self.save(new_context, user, request_id='outcome')
        self.service.update_draft(session['sessionId'], {'requestId': 'edit', 'expectedStateRevision': self.version(session['sessionId']), 'markdown': '另一次手改'})
        with self.assertRaises(RepositoryError):
            self.save(self.run_context(session), user, request_id='too-late')

    def test_cancelled_foreign_or_unbound_host_and_fabricated_fields_cannot_execute(self):
        session, _, _, _, proposals, prompt = self.case()
        user, context = self.answer(session, prompt)
        payload = self.payload(user, prompt, proposals)
        self.error(RequestContext(session['sessionId']), payload, 'scope_violation')
        self.error(context, {**payload, 'userMessageId': prompt['messageId']})
        self.error(context, {**payload, 'sql': 'not allowed'}, 'invalid_input')
        self.error(context, {**payload, 'status': 'clarify'}, 'invalid_input')
        with self.assertRaises(RepositoryError):
            self.service.apply_resolved_user_decision(context, payload)
        with self.assertRaises(RepositoryError):
            self.service.repository.apply_suggestion_decision(context, proposals[0]['suggestionId'], {'decision': 'accept'})
        self.run_context(session)
        self.error(context, payload, 'cancelled')
        self.assertEqual(self.count('suggestion_decisions'), 0)
        self.assertFalse(MaterialTools(self.service.repository, context).dispatch('apply_resolved_user_decision', payload)['ok'])

    def test_delete_cascades_consumption_and_old_payload_cannot_resurrect(self):
        session, _, _, _, proposals, prompt = self.case()
        user, context = self.answer(session, prompt)
        payload = self.payload(user, prompt, proposals)
        self.apply(context, payload)
        self.service.delete_session(session['sessionId'], {'requestId': 'delete', 'expectedStateRevision': self.version(session['sessionId']), 'confirmed': True})
        self.assertEqual(self.count('chat_decision_resolutions'), 0)
        self.error(context, payload, 'session_not_found')

    def test_foreign_turn_and_unasked_suggestion_cannot_become_authorization(self):
        session, _, _, _, proposals, prompt = self.case()
        foreign, _, _, _, foreign_proposals, foreign_prompt = self.case()
        foreign_user, foreign_context = self.answer(foreign, foreign_prompt)
        user, context = self.answer(session, prompt)
        payload = self.payload(user, prompt, proposals)
        self.error(foreign_context, payload)
        wrong = self.payload(user, foreign_prompt, foreign_proposals)
        self.error(context, wrong, 'scope_violation')
        wrong = self.payload(user, prompt, foreign_proposals)
        self.error(context, wrong, 'scope_violation')
        self.assertEqual(self.count('suggestion_decisions'), 0)

    def test_malformed_empty_duplicate_decisions_and_fabricated_feedback_rejected(self):
        session, _, _, _, proposals, prompt = self.case()
        user, context = self.answer(session, prompt)
        payload = self.payload(user, prompt, proposals)
        item = payload['decisions'][0]
        for decisions in ([], [item, item], [{**item, 'decision': 'maybe'}],
                          [{**item, 'decision': 'revise', 'feedback': ''}],
                          [{**item, 'expectedBlockRevision': 999}], 'not a list'):
            with self.subTest(decisions=decisions):
                self.error(context, {**payload, 'decisions': decisions}, 'invalid_input')
        self.assertEqual(self.count('chat_decision_resolutions'), 0)

    def test_schema_nine_upgrade_preserves_messages_without_inventing_consumption(self):
        with db.database(':memory:') as connection:
            with patch.object(db, 'MIGRATIONS', db.MIGRATIONS[:9]), patch.object(db, 'SCHEMA_VERSION', 9):
                db.initialize_database(connection)
                connection.execute("INSERT INTO sessions(session_id,direction) VALUES ('s','构造')")
                connection.execute("INSERT INTO messages(message_id,session_id,role,content) VALUES ('m','s','user','原话')")
            before = dict(connection.execute('SELECT * FROM messages').fetchone())
            db.initialize_database(connection)
            self.assertEqual(dict(connection.execute('SELECT * FROM messages').fetchone()), before)
            self.assertEqual(connection.execute('SELECT count(*) FROM chat_decision_resolutions').fetchone()[0], 0)

    def test_clarification_creates_fresh_prompt_then_new_user_turn_can_choose(self):
        session, _, _, before, proposals, prompt = self.case(two=True)
        user, context = self.answer(session, prompt, '可以。')
        unresolved = {**self.payload(user, prompt, proposals), 'status': 'clarify', 'decisions': []}
        self.apply(context, unresolved)
        clarified = self.save(context, user, 'change_question', [p['suggestionId'] for p in proposals], request_id='clarification')
        next_user = self.send(session, '仅采用第一条。', request_id='clarified-answer', replyToMessageId=clarified['messageId'])
        next_payload = {**self.payload(next_user, clarified, proposals[:1]), 'requestId': 'next-decision'}
        self.apply(self.run_context(session), next_payload)
        self.assertEqual(self.conversation(session)['draft']['markdown'], '构造改写 1\n\n第二段')
        self.assertEqual(self.count('chat_decision_resolutions'), 2)
        self.assertEqual(self.count('suggestion_decisions'), 1)


if __name__ == '__main__':
    unittest.main()
