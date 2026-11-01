"""Offline checks for web permissions, reference isolation and lossless upgrades."""
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import patch

from app import db
from app.business_service import BusinessService
from app.repository import Repository, RepositoryError, RequestContext
import test_api
from test_db import seed_fixture, temporary_directory


REFERENCE = {'title': '构造公司工程实习', 'url': 'https://example.com/jobs/1',
             'content': '岗位要求SQL；这不能证明候选人会SQL。',
             'matchStatus': 'matched', 'hiringStatus': 'unconfirmed', 'contentStatus': 'sufficient'}


class WebStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.path = self.root / 'web.sqlite'
        with db.database(self.path) as c:
            db.initialize_database(c)
            self.sid = seed_fixture(c)
        self.repo = Repository(self.path)
        self.service = BusinessService(self.repo)
        self.user = RequestContext(self.sid)

    def settings(self, enabled, revision, key=None):
        return self.service.update_web_settings(self.sid, {'requestId': key or f'web-{revision}',
            'enabled': enabled, 'expectedWebSettingsRevision': revision})['data']

    def run_context(self):
        session = self.repo.get_session(self.user)
        with db.database(self.path) as c, db.transaction(c):
            c.execute("UPDATE runs SET status='running',input_state_revision=?,resume_generation=? WHERE session_id=? AND run_id='run-test-001'",
                      (session['stateRevision'], session['currentResumeGeneration'], self.sid))
        return RequestContext(self.sid, session['stateRevision'], 'run-test-001', 1)

    def begin(self, context, key='search-1', **overrides):
        args = {'request_id': key, 'web_settings_revision': 2, 'target_key': 'target-1',
                'operation': 'search', 'payload': {'Query': '构造公司 工程实习'}}
        args.update(overrides)
        return self.repo.begin_web_request(context, **args)

    def finish(self, context, key='search-1', **overrides):
        args = {'request_id': key, 'web_settings_revision': 2, 'target_key': 'target-1',
                'references': [deepcopy(REFERENCE)]}
        args.update(overrides)
        return self.repo.finish_web_request(context, **args)

    def assert_error(self, code, callback):
        with self.assertRaises(RepositoryError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_new_sessions_default_off_and_settings_survive_new_repository(self):
        session = self.repo.create_session('构造岗位')
        self.assertFalse(session['webEnabled'])
        self.assertEqual(session['webSettingsRevision'], 1)
        self.settings(True, 1)
        self.assertEqual(Repository(self.path).get_web_settings(self.user),
                         {'enabled': True, 'webSettingsRevision': 2})
        self.assertFalse(self.repo.get_web_settings(RequestContext(session['sessionId']))['enabled'])

    def test_permission_changes_do_not_mutate_draft_or_business_context(self):
        before = self.repo.get_session(self.user)
        draft = self.repo.get_draft(self.user)
        context = self.run_context()
        self.settings(True, 1)
        self.settings(False, 2)
        after = self.repo.get_session(context)  # live run remains valid
        for name in ('stateRevision', 'draftRevision', 'currentResumeGeneration', 'updatedAt'):
            self.assertEqual(after[name], before[name])
        self.assertEqual(self.repo.get_draft(context), draft)

    def test_stale_window_and_exact_request_replay(self):
        first = self.settings(True, 1, 'enable')
        self.assert_error('stale_state', lambda: self.settings(False, 1, 'stale-window'))
        self.assertEqual(self.settings(True, 1, 'enable'), first)
        self.settings(False, 2, 'disable')
        self.assertEqual(self.settings(True, 1, 'enable'), first)
        self.assertFalse(self.repo.get_web_settings(self.user)['enabled'])
        self.assert_error('request_conflict', lambda: self.settings(False, 3, 'enable'))

    def test_agent_cannot_grant_itself_permission(self):
        context = self.run_context()
        self.assert_error('scope_violation', lambda: self.repo.update_web_settings(context, enabled=True, expected_revision=1))
        self.assert_error('web_disabled', lambda: self.begin(context, web_settings_revision=1))
        self.assert_error('scope_violation', lambda: self.begin(self.user))

    def test_close_reopen_cancels_pending_and_rejects_late_results(self):
        self.settings(True, 1)
        context = self.run_context()
        self.begin(context)
        self.settings(False, 2)
        self.settings(True, 3)
        self.assert_error('web_disabled', lambda: self.finish(context))
        with db.database(self.path, read_only=True) as c:
            row = c.execute('SELECT status,error_code FROM web_requests').fetchone()
            self.assertEqual(tuple(row), ('cancelled', 'web_settings_changed'))
            self.assertEqual(c.execute('SELECT count(*) FROM external_references').fetchone()[0], 0)

    def test_request_replay_does_not_create_another_network_dispatch_slot(self):
        self.settings(True, 1)
        context = self.run_context()
        self.assertTrue(self.begin(context)['created'])
        self.assertFalse(self.begin(context)['created'])
        self.assert_error('request_conflict', lambda: self.begin(context, target_key='different-target'))
        first = self.finish(context)
        replay = self.finish(context)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['references'], first['references'])
        self.assertEqual(len(self.repo.list_external_references(self.user)), 1)

    def test_wrong_target_and_wrong_session_cannot_finish_request(self):
        self.settings(True, 1)
        context = self.run_context()
        self.begin(context)
        self.assert_error('scope_violation', lambda: self.finish(context, target_key='wrong'))
        other = self.repo.create_session('另一个构造岗位')
        foreign = RequestContext(other['sessionId'], 1, 'run-test-001', 1)
        self.assert_error('cancelled', lambda: self.finish(foreign))
        self.assertEqual(self.repo.list_external_references(self.user), [])

    def test_cancelled_run_and_changed_resume_generation_reject_results(self):
        self.settings(True, 1)
        context = self.run_context()
        self.begin(context)
        with db.database(self.path) as c, db.transaction(c):
            c.execute("UPDATE runs SET status='interrupted' WHERE run_id='run-test-001'")
        self.assert_error('cancelled', lambda: self.finish(context))
        context = self.run_context()
        with db.database(self.path) as c, db.transaction(c):
            c.execute('UPDATE sessions SET current_resume_generation=current_resume_generation+1 WHERE session_id=?', (self.sid,))
        self.assert_error('stale_state', lambda: self.finish(context))
        self.assertEqual(self.repo.list_external_references(self.user), [])

    def test_invalid_reference_rolls_back_entire_batch(self):
        self.settings(True, 1)
        context = self.run_context()
        self.begin(context)
        invalid = {**REFERENCE, 'hiringStatus': 'assumed-open'}
        self.assert_error('invalid_input', lambda: self.finish(context, references=[REFERENCE, invalid]))
        self.assertEqual(self.repo.list_external_references(self.user), [])
        with db.database(self.path, read_only=True) as c:
            self.assertEqual(c.execute('SELECT status FROM web_requests').fetchone()[0], 'pending')

    def test_references_are_immutable_separate_from_personal_sources_and_remain_in_history(self):
        self.settings(True, 1)
        context = self.run_context()
        before = self.repo.get_session_context(context)
        self.begin(context)
        result = self.finish(context)
        reference = result['references'][0]
        other = self.repo.create_session('其他会话')['sessionId']
        with db.database(self.path) as c:
            with self.assertRaises(sqlite3.IntegrityError), db.transaction(c):
                c.execute("UPDATE external_references SET content='changed'")
            with self.assertRaises(sqlite3.IntegrityError), db.transaction(c):
                c.execute('''INSERT INTO external_references(reference_id,session_id,request_id,title,url,content,retrieved_at,match_status,hiring_status,content_status)
                             VALUES ('cross',?,'search-1','x','https://example.com','x','now','matched','open','sufficient')''', (other,))
        self.assertEqual(self.repo.get_session_context(context), before)
        self.settings(False, 2)
        self.assertEqual(self.repo.list_external_references(self.user), [reference])

    def test_delete_cascades_web_data_but_keeps_cost_records(self):
        self.settings(True, 1)
        context = self.run_context()
        self.begin(context)
        self.finish(context)
        with db.database(self.path) as c, db.transaction(c):
            c.execute("UPDATE runs SET status='completed' WHERE run_id='run-test-001'")
            c.execute("INSERT INTO model_budget_accounts(budget_id,limit_cents,spent_cents) VALUES ('project',3000,123)")
            c.execute("INSERT INTO model_budget_reservations(reservation_id,run_id,reserved_cents,actual_cents,status) VALUES ('cost','run-test-001',2,2,'settled')")
        self.repo.delete_session(self.user)
        with db.database(self.path, read_only=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM web_requests').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM external_references').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT spent_cents FROM model_budget_accounts').fetchone()[0], 123)
            self.assertEqual(c.execute('SELECT count(*) FROM model_budget_reservations').fetchone()[0], 1)

    def test_v12_upgrade_defaults_old_sessions_off_and_preserves_all_business_rows(self):
        with db.database(':memory:') as c:
            with patch.object(db, 'MIGRATIONS', db.MIGRATIONS[:12]), patch.object(db, 'SCHEMA_VERSION', 12):
                db.initialize_database(c)
                sid = seed_fixture(c)
            tables = [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('sessions','schema_version')")]
            saved = {name: [tuple(row) for row in c.execute('SELECT * FROM '+name)] for name in tables}
            old_session = dict(c.execute('SELECT * FROM sessions WHERE session_id=?', (sid,)).fetchone())
            db.initialize_database(c)
            current = dict(c.execute('SELECT * FROM sessions WHERE session_id=?', (sid,)).fetchone())
            self.assertEqual({k:current[k] for k in old_session}, old_session)
            self.assertEqual(current['web_enabled'], 0)
            self.assertEqual(current['web_settings_revision'], 1)
            for name in tables:
                self.assertEqual([tuple(row) for row in c.execute('SELECT * FROM '+name)], saved[name], name)
            db.initialize_database(c)
            self.assertEqual(db.check_database(c)['schemaVersion'], db.SCHEMA_VERSION)


class WebSettingsAPITests(unittest.TestCase):
    setUp = test_api.ApiTests.setUp
    request = test_api.ApiTests.request
    new_session = test_api.ApiTests.new_session

    def test_user_settings_endpoint_strict_validation_and_refresh_restore(self):
        session = self.new_session()
        path = f"/api/sessions/{session['sessionId']}/web-settings"
        self.assertEqual(self.request('GET', path)['json']['data'], {'enabled':False,'webSettingsRevision':1})
        payload = {'requestId':'web-1','enabled':True,'expectedWebSettingsRevision':1}
        updated = self.request('PATCH', path, data=payload)
        self.assertEqual(updated['status'], 200)
        self.assertEqual(self.request('GET', path)['json']['data'], {'enabled':True,'webSettingsRevision':2})
        self.assertEqual(self.request('PATCH', path, data=payload)['json'], updated['json'])
        for changed in ({'enabled':'false'}, {'expectedWebSettingsRevision':True}, {'extra':'no'}):
            response = self.request('PATCH', path, data={**payload, **changed})
            self.assertEqual(response['status'], 422, response)
        response = self.request('PATCH', path, data={**payload,'requestId':'stale','enabled':False})
        self.assertEqual(response['status'], 409)

    def test_no_cross_site_mutations_no_missing_session_and_no_public_fetch_proxy(self):
        session = self.new_session()
        path = f"/api/sessions/{session['sessionId']}/web-settings"
        payload = {'requestId':'web','enabled':True,'expectedWebSettingsRevision':1}
        self.assertEqual(self.request('PATCH', path, data=payload, headers={'origin':'https://example.com'})['status'],403)
        self.assertEqual(self.request('PATCH','/api/sessions/missing/web-settings',data=payload)['status'],404)
        self.assertEqual(self.request('GET',f"/api/sessions/{session['sessionId']}/external-references")['json']['data'],[])
        self.assertEqual(self.request('POST',f"/api/sessions/{session['sessionId']}/fetch",data={'url':'http://localhost'})['status'],404)
