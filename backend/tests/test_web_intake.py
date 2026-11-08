"""Isolated SQLite intake tests; no network or model calls."""
import unittest

from app import web_intake as intake
from app.db import database, transaction
from app.repository import RepositoryError
from app.web_target import _put
import test_web_storage as fixture


class WebIntakeTests(unittest.TestCase):
    setUp = fixture.WebStorageTests.setUp
    settings = fixture.WebStorageTests.settings

    def state(self, intent='intent-one', **kwargs):
        with self.repo._operation(self.user, write=True) as (c, s):
            return intake.ensure(c, s, intent, **kwargs)

    def claim(self, value, request_id='q1', operation='search', **kwargs):
        with self.repo._operation(self.user, write=True) as (c, s):
            return intake.claim_attempt(c, s, episode_id=value['episodeId'],
                expected_revision=value['revision'], request_id=request_id,
                operation=operation, fingerprint=request_id, **kwargs)

    def enabled(self):
        self.settings(True, 1)
        return self.state()

    def test_initialize_is_persistent_and_idempotent(self):
        first = self.state()
        self.assertEqual(first, self.state())
        with database(self.path) as c:
            self.assertEqual(intake.load(c, self.sid), first)
        self.assertEqual(first['formatVersion'], 1)

    def test_corrupt_counter_is_rejected_without_reset(self):
        value = self.state()
        value['searchCount'] = -1
        with self.repo._operation(self.user, write=True) as (c, s):
            _put(c, self.sid, intake.KEY, value)
        with self.assertRaises(RepositoryError) as error:
            self.state()
        self.assertEqual(error.exception.code, 'invalid_intake_state')

    def test_state_is_isolated_from_other_sessions_and_business_data(self):
        before = self.repo.get_draft(self.user)
        session = self.repo.get_session(self.user)
        self.claim(self.enabled())
        other = self.repo.create_session('构造另一个会话')
        with database(self.path) as c:
            self.assertIsNone(intake.load(c, other['sessionId']))
        self.assertEqual(self.repo.get_draft(self.user), before)
        after = self.repo.get_session(self.user)
        for key in ('stateRevision', 'draftRevision', 'currentResumeGeneration'):
            self.assertEqual(after[key], session[key])

    def test_permission_toggle_invalidates_results_without_replenishing_attempts(self):
        value, _ = self.claim(self.enabled())
        self.settings(False, 2)
        self.settings(True, 3)
        new = self.state()
        self.assertEqual(new['episodeId'], value['episodeId'])
        self.assertEqual(new['searchCount'], 1)
        self.assertEqual(new['candidates'], [])
        self.assertGreater(new['revision'], value['revision'])
        with self.assertRaises(RepositoryError) as error:
            self.claim(value, 'old')
        self.assertEqual(error.exception.code, 'stale_state')

    def test_search_limit_and_receipt_replay(self):
        value, created = self.claim(self.enabled())
        self.assertTrue(created)
        replay, created = self.claim(value)
        self.assertFalse(created)
        self.assertEqual(replay, value)
        value, _ = self.claim(value, 'q2')
        with self.assertRaises(RepositoryError) as error:
            self.claim(value, 'q3')
        self.assertEqual(error.exception.code, 'web_limit')
        self.assertEqual(self.state()['searchCount'], 2)

    def test_changed_request_cannot_reuse_receipt(self):
        value, _ = self.claim(self.enabled())
        with self.assertRaises(RepositoryError) as error:
            self.claim(value, operation='fetch', url_identity='https://example.com/1')
        self.assertEqual(error.exception.code, 'request_conflict')

    def test_pages_are_deduplicated_and_limited(self):
        value = self.enabled()
        for i in range(3):
            value, _ = self.claim(value, f'f{i}', 'fetch', url_identity=f'https://example.com/{i}')
        with self.assertRaises(RepositoryError) as error:
            self.claim(value, 'duplicate', 'fetch', url_identity='https://example.com/0')
        self.assertEqual(error.exception.code, 'web_duplicate_page')
        with self.assertRaises(RepositoryError) as error:
            self.claim(value, 'f4', 'fetch', url_identity='https://example.com/4')
        self.assertEqual(error.exception.code, 'web_limit')

    def test_explicit_changed_conditions_or_retry_start_new_episode(self):
        value, _ = self.claim(self.enabled())
        changed = self.state('intent-two')
        self.assertNotEqual(changed['episodeId'], value['episodeId'])
        self.assertEqual(changed['searchCount'], 0)
        retried = self.state('intent-two', explicit_retry=True)
        self.assertNotEqual(retried['episodeId'], changed['episodeId'])

    def test_disabled_and_stale_state_cannot_claim(self):
        value = self.state()
        with self.assertRaises(RepositoryError) as error:
            self.claim(value)
        self.assertEqual(error.exception.code, 'web_disabled')
        value = self.enabled()
        self.claim(value)
        with self.assertRaises(RepositoryError) as error:
            self.claim(value, 'stale')
        self.assertEqual(error.exception.code, 'stale_state')

    def test_unknown_version_preserved_instead_of_overwritten(self):
        with self.repo._operation(self.user, write=True) as (c, s):
            _put(c, self.sid, intake.KEY, {'formatVersion': 99})
        with self.assertRaises(RepositoryError) as error:
            self.state()
        self.assertEqual(error.exception.code, 'unsupported_intake_version')
        with database(self.path) as c:
            self.assertIn('99', c.execute("select doc_json from agent_state where key=?",
                                        (intake.KEY,)).fetchone()[0])

    def test_generic_phase_update_cannot_qualify_evidence(self):
        value = self.enabled()
        with self.assertRaises(RepositoryError) as error:
            with self.repo._operation(self.user, write=True) as (c, s):
                intake.set_phase(c, s, episode_id=value['episodeId'],
                                 expected_revision=value['revision'], phase='ready')
        self.assertEqual(error.exception.code, 'scope_violation')

    def test_transaction_rollback_does_not_spend_attempt(self):
        value = self.enabled()
        with self.assertRaises(RuntimeError):
            with self.repo._operation(self.user, write=True) as (c, s):
                intake.claim_attempt(c, s, episode_id=value['episodeId'],
                    expected_revision=value['revision'], request_id='rollback',
                    operation='search', fingerprint='rollback')
                raise RuntimeError('synthetic rollback')
        self.assertEqual(self.state()['searchCount'], 0)

    def test_resume_generation_change_invalidates_binding_not_allowance(self):
        value, _ = self.claim(self.enabled())
        with database(self.path) as c, transaction(c):
            c.execute('update sessions set current_resume_generation=current_resume_generation+1 where session_id=?', (self.sid,))
        new = self.state()
        self.assertEqual(new['searchCount'], 1)
        self.assertEqual(new['episodeId'], value['episodeId'])
        self.assertNotEqual(new['resumeGeneration'], value['resumeGeneration'])


class WebDeclineTests(unittest.TestCase):
    def test_short_declines_are_recognized(self):
        for text in ('不查', '不查了', '不搜', '不用搜', '不用查', '这次不查了',
                     '先按通用吧', '通用修改', '不需要联网。', '跳过联网'):
            with self.subTest(text=text):
                self.assertTrue(intake.is_web_decline(text))

    def test_content_answers_are_not_declines(self):
        for text in ('不查户口，但要联网看岗位', '我负责接口测试',
                     '先不查相关资料我再想想'):
            with self.subTest(text=text):
                self.assertFalse(intake.is_web_decline(text))
