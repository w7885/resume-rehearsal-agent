"""Real PostgreSQL regression, opt in with an isolated local test database.

Run: python -m unittest discover -s tests -p test_postgres.py -v
RESUME_TEST_POSTGRES_URL must point at a disposable resume_test_* database.
The runner truncates ONLY that database's resume_app schema between cases.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import unittest
from urllib.parse import urlsplit

from app import db
from app.agent_budget import ProjectBudget
from app.repository import Repository, RequestContext
from app.material_tools import MaterialTools
from test_db import seed_fixture, temporary_directory


URL = os.environ.get('RESUME_TEST_POSTGRES_URL', '')


def isolated_database():
    parsed = urlsplit(URL)
    if parsed.scheme not in {'postgres', 'postgresql'} or parsed.hostname not in {'127.0.0.1', 'localhost'} or not parsed.path.startswith('/resume_test_') or parsed.query:
        raise RuntimeError('PostgreSQL tests require an explicitly isolated local resume_test_* database.')
    with db.database(URL) as c:
        db.initialize_database(c)
        with db.transaction(c):
            names = db.check_database(c)['tables']
            names = [name for name in names if name != 'schema_version']
            c.execute('TRUNCATE ' + ','.join('"'+name+'"' for name in names) + ' RESTART IDENTITY CASCADE')
    return URL


def setup_repository(self):
    self.directory = temporary_directory()
    self.root = self.directory.__enter__()
    self.addCleanup(self.directory.__exit__, None, None, None)
    self.path = isolated_database()
    with db.database(self.path) as c:
        self.session_id = seed_fixture(c)
    self.repo = Repository(self.path)
    self.context = RequestContext(self.session_id)
    self.tools = MaterialTools(self.repo, self.context)


def setup_api(self):
    from app.main import create_app
    self.temp = temporary_directory()
    self.root = self.temp.__enter__()
    self.addCleanup(self.temp.__exit__, None, None, None)
    self.path = isolated_database()
    self.runner = asyncio.Runner()
    self.addCleanup(self.runner.close)
    self.app = create_app(self.path)
    self.lifecycle = self.app.router.lifespan_context(self.app)
    self.runner.run(self.lifecycle.__aenter__())
    self.addCleanup(lambda: self.runner.run(self.lifecycle.__aexit__(None, None, None)))
    self.sequence = 0


def setup_web(self):
    from app.business_service import BusinessService
    self.path = isolated_database()
    with db.database(self.path) as c:
        self.sid = seed_fixture(c)
    self.repo = Repository(self.path)
    self.service = BusinessService(self.repo)
    self.user = RequestContext(self.sid)


def install_failure_trigger(test, table):
    """PostgreSQL equivalent of SQLite RAISE(ABORT), only in the test DB."""
    if test.path != URL or table not in {'suggestion_sources', 'request_log'}:
        raise AssertionError('Failure injection must stay inside the isolated test database.')
    def cleanup():
        with db.database(test.path) as connection, db.transaction(connection):
            connection.execute('DROP TRIGGER app_test_failure ON ' + table)
            connection.execute('DROP FUNCTION app_test_failure()')
    with db.database(test.path) as connection, db.transaction(connection):
        connection.execute("""CREATE FUNCTION app_test_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'do-not-expose-private-sql' USING ERRCODE='23514'; END;
            $$""")
        connection.execute('CREATE TRIGGER app_test_failure BEFORE INSERT ON ' + table +
                           ' FOR EACH ROW EXECUTE FUNCTION app_test_failure()')
    test.addCleanup(cleanup)


def repository_sql(self, statement, parameters=()):
    if statement.startswith('CREATE TRIGGER fail_reference '):
        install_failure_trigger(self, 'suggestion_sources')
        return []
    import test_repository
    return test_repository.RepositoryTests.sql(self, statement, parameters)


def request_log_failure(self):
    session = self.new_session()
    install_failure_trigger(self, 'request_log')
    result = self.message(session, '不应部分保存')
    self.assert_error(result, 503, 'storage_failed')
    self.assertNotIn(b'do-not-expose', result['body'])
    self.assertEqual(self.count('messages'), 0)
    self.assertEqual(self.count('source_blocks'), 0)
    self.assertEqual(self.version(session['sessionId']), 1)


@unittest.skipUnless(URL, 'Real PostgreSQL requires RESUME_TEST_POSTGRES_URL; SQLite is not a substitute.')
class PostgresProtectionTests(unittest.TestCase):
    def setUp(self):
        self.path = isolated_database()

    def test_budget_concurrency_and_idempotent_unknown_settlement(self):
        def reserve(index):
            return ProjectBudget(self.path, 'run', 100).reserve_request('cost-'+str(index), 60)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, range(6)))
        self.assertEqual(sum(results), 1)
        budget = ProjectBudget(self.path, 'run', 100)
        rid = 'cost-'+str(results.index(True))
        self.assertEqual(budget.settle_request(rid)['chargedCents'], 60)
        self.assertEqual(budget.settle_request(rid, 0)['chargedCents'], 60)
        self.assertEqual(budget.status()['spentCents'], 60)
        self.assertEqual(budget.status()['reservedCents'], 0)

    def test_state_store_uses_same_transaction_and_revision_contract(self):
        from app.agent.state_sqlite import SqliteStateStore
        from app.agent.store import StaleState
        sid = Repository(self.path).create_session('构造岗位')['sessionId']
        store = SqliteStateStore(self.path)
        self.assertEqual(store.put(sid, 'session', 'key', {'value': 1}, None), 1)
        with self.assertRaises(StaleState):
            store.put(sid, 'session', 'key', {'value': 2}, None)
        self.assertEqual(store.put(sid, 'session', 'key', {'value': 2}, 1), 2)
        self.assertEqual(store.get(sid, 'session', 'key').doc, {'value': 2})

    def test_read_only_connection_rejects_write(self):
        with db.database(self.path, read_only=True) as c:
            with self.assertRaises(sqlite3.Error):
                c.execute("INSERT INTO sessions(session_id,direction) VALUES ('forbidden','test')")

    def test_constraint_drift_refused_and_transaction_rolls_back(self):
        with db.database(self.path) as c:
            with self.assertRaises(db.SchemaError), db.transaction(c):
                c.execute('ALTER TABLE source_blocks DISABLE TRIGGER sources_immutable')
                db.check_database(c)
            self.assertEqual(db.check_database(c)['schemaVersion'], db.SCHEMA_VERSION)

    def test_deferred_fk_failure_rolls_back_all_writes(self):
        with db.database(self.path) as c:
            with self.assertRaises(sqlite3.IntegrityError), db.transaction(c):
                c.execute("INSERT INTO sessions(session_id,direction) VALUES ('rollback','test')")
                c.execute("""INSERT INTO source_blocks(source_id,source_revision,session_id,message_id,role,text,origin_json)
                    VALUES ('missing-ref',1,'rollback','missing','supplement','test','{}')""")
            self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)


def load_tests(loader, tests, pattern):
    if not URL:
        return tests
    import test_repository, test_api, test_proposal_snapshot, test_web_storage, test_agent_integration, test_auth
    # Preserve existing business assertions. SQLite-only trigger syntax gets
    # native PostgreSQL failure injection; local-file/SQLite-upgrade cases stay
    # in the SQLite suite, rather than being counted as PostgreSQL coverage.
    cases = [
        (test_repository.RepositoryTests, setup_repository, {'test_storage_failure_is_not_a_successful_empty_result'}),
        (test_api.ApiTests, setup_api, set()),
        (test_proposal_snapshot.SnapshotPersistenceTests, setup_api, set()),
        (test_web_storage.WebStorageTests, setup_web, {'test_v12_upgrade_defaults_old_sessions_off_and_preserves_all_business_rows'}),
        (test_agent_integration.BudgetIntegrationTests, setup_api, set()),
    ]
    for base, setup, exclude in cases:
        overrides = {'setUp': setup}
        if base is test_repository.RepositoryTests:
            overrides['sql'] = repository_sql
        elif base is test_api.ApiTests:
            overrides['test_request_log_failure_rolls_back_message_source_and_revision'] = request_log_failure
        cls = type('Postgres'+base.__name__, (base,), overrides)
        for name in loader.getTestCaseNames(cls):
            if name not in exclude:
                tests.addTest(cls(name))
    auth_cls = type('PostgresAuthenticationTests', (test_auth.AuthenticationTests,),
                    {'database_path': lambda self, root: isolated_database()})
    tests.addTests(loader.loadTestsFromTestCase(auth_cls))
    return tests
