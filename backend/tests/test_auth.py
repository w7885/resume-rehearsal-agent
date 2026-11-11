"""Authentication/ownership integration with synthetic identities and no network."""
import asyncio
from dataclasses import replace
import os
from unittest.mock import AsyncMock, patch
import unittest

from app.auth import RemoteSettings, current_user
from app.business_service import BusinessService
from app.db import database, initialize_database, transaction
from app.main import create_app
from app.repository import Repository, RequestContext
from test_api import request_asgi
from test_db import temporary_directory


USER_A = '11111111-1111-4111-8111-111111111111'
USER_B = '22222222-2222-4222-8222-222222222222'
SETTINGS = RemoteSettings('https://synthetic.supabase.co', 'synthetic-public-key', 'api.example.com',
                          ('https://app.example.com',), frozenset({'a@example.com', 'b@example.com'}))


class AuthenticationTests(unittest.TestCase):
    def test_read_cleanup_failure_returns_safe_503_with_exact_cors_origin(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        from app.postgres import Connection
        import psycopg
        sid = self.create()
        repo = self.app.state.business_service.repository
        scope = repo._connection_scope

        @contextmanager
        def failed_cleanup(*, write=False):
            with scope(write=write) as connection:
                yield connection
                if not write:
                    raw = Mock()
                    raw.rollback.side_effect = psycopg.OperationalError('private-cleanup-details')
                    Connection(raw).rollback()

        with patch.object(repo, '_connection_scope', failed_cleanup):
            result = self.request('GET', f'/api/sessions/{sid}/conversation')
        self.assertEqual(result['status'], 503)
        self.assertEqual(result['json']['error']['code'], 'storage_failed')
        self.assertEqual(result['headers'][b'access-control-allow-origin'], SETTINGS.origins[0].encode())
        self.assertNotIn(b'private-cleanup', result['body'])
        self.assertEqual(self.request('GET', f'/api/sessions/{sid}/conversation')['status'], 200)

    def database_path(self, root):
        return root / 'auth.sqlite'

    def setUp(self):
        self.temp = temporary_directory()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.path = self.database_path(self.root)
        self.runner = asyncio.Runner()
        self.addCleanup(self.runner.close)
        with patch('app.auth.RemoteSettings.from_environment', return_value=SETTINGS):
            self.app = create_app(self.path)
        self.lifecycle = self.app.router.lifespan_context(self.app)
        self.runner.run(self.lifecycle.__aenter__())
        self.addCleanup(lambda: self.runner.run(self.lifecycle.__aexit__(None, None, None)))
        async def verify(settings, token):
            return {'token-a': USER_A, 'token-b': USER_B}.get(token)
        mocked = patch('app.auth.verify_user', side_effect=verify)
        self.verify = mocked.start()
        self.addCleanup(mocked.stop)

    def request(self, method, path, *, token='token-a', **kwargs):
        headers = {'host': 'api.example.com', 'origin': 'https://app.example.com'}
        if token:
            headers['authorization'] = 'Bearer ' + token
        headers.update(kwargs.pop('headers', {}))
        return self.runner.run(request_asgi(self.app, method, path, headers=headers, **kwargs))

    def create(self, token='token-a', request='same-request'):
        response = self.request('POST', '/api/chat/sessions', token=token, data={'requestId': request})
        self.assertEqual(response['status'], 201, response)
        return response['json']['data']['sessionId']

    def test_anonymous_invalid_and_unavailable_auth_fail_closed(self):
        self.assertEqual(self.request('GET', '/api/sessions', token=None)['status'], 401)
        self.assertEqual(self.request('GET', '/api/sessions', token='invalid')['status'], 401)
        with patch('app.auth.verify_user', side_effect=RuntimeError('private diagnostic')):
            response = self.request('GET', '/api/sessions')
        self.assertEqual(response['status'], 503)
        self.assertNotIn(b'private diagnostic', response['body'])
        self.assertEqual(self.request('GET', '/api/health', token=None)['status'], 200)

    def test_user_lists_and_create_replays_are_isolated(self):
        a, b = self.create(), self.create('token-b')
        self.assertNotEqual(a, b)
        self.assertEqual(self.create(), a)
        self.assertEqual(self.create('token-b'), b)
        for token, expected in [('token-a', a), ('token-b', b)]:
            listed = self.request('GET', '/api/sessions', token=token)['json']['data']
            self.assertEqual([row['sessionId'] for row in listed], [expected])

    def test_open_registration_retains_account_isolation_and_one_shared_budget(self):
        with patch('app.auth.RemoteSettings.from_environment', return_value=replace(SETTINGS, emails=frozenset(), access_mode='open')):
            opened = create_app(self.path)
        old_app = self.app
        lifecycle = opened.router.lifespan_context(opened)
        self.runner.run(lifecycle.__aenter__())
        self.app = opened
        try:
            a, b = self.create(), self.create('token-b')
            self.assertEqual(self.request('GET', '/api/sessions/'+a, token='token-b')['status'], 404)
            self.assertEqual(self.request('GET', '/api/sessions/'+b)['status'], 404)
            from app.agent_budget import ProjectBudget
            budget = ProjectBudget(self.path, 'synthetic-shared', 1000)
            self.assertTrue(budget.reserve_request('synthetic-open-cost', 5))
            budget.settle_request('synthetic-open-cost', 3)
            for token, sid in [('token-a', a), ('token-b', b)]:
                value = self.request('GET', '/api/sessions/'+sid+'/budget', token=token)['json']['data']
                self.assertEqual(value['spentCents'], 3)
                self.assertEqual(value['limitCents'], 1000)
        finally:
            self.app = old_app
            self.runner.run(lifecycle.__aexit__(None, None, None))

    def test_cross_account_paths_materials_runs_and_mutations_are_rejected(self):
        sid = self.create()
        base = '/api/sessions/' + sid
        material = self.request('POST', base+'/materials', data={'requestId': 'upload', 'expectedStateRevision': 1,
            'name': 'synthetic.md', 'text': '构造简历，负责整理反馈。', 'role': 'resume'})
        self.assertEqual(material['status'], 201, material)
        mid = material['json']['data']['material']['materialId']
        for path in [base, base+'/conversation', base+'/draft', base+'/messages', base+'/materials',
                     base+'/materials/'+mid+'/file', base+'/agent-requests/fake', base+'/budget']:
            self.assertEqual(self.request('GET', path, token='token-b')['status'], 404, path)
        response = self.request('POST', base+'/messages', token='token-b', data={
            'requestId': 'foreign', 'expectedStateRevision': 2, 'content': 'foreign'})
        self.assertEqual(response['status'], 404)
        with database(self.path) as c, transaction(c):
            c.execute("INSERT INTO runs(run_id,session_id,generation,input_state_revision,resume_generation,status) VALUES ('private-run',?,1,2,1,'running')", (sid,))
        self.assertEqual(self.request('POST', '/api/runs/private-run/cancel', token='token-b')['status'], 404)
        with database(self.path) as c:
            self.assertEqual(c.execute("SELECT status FROM runs WHERE run_id='private-run'").fetchone()[0], 'running')

    def test_local_legacy_session_is_not_claimed_by_first_login(self):
        legacy = Repository(self.path).create_session('旧本地构造数据')['sessionId']
        self.assertEqual(self.request('GET', '/api/sessions')['json']['data'], [])
        self.assertEqual(self.request('GET', '/api/sessions/'+legacy)['status'], 404)
        with database(self.path) as c:
            self.assertIsNone(c.execute('SELECT owner_user_id FROM sessions WHERE session_id=?', (legacy,)).fetchone()[0])

    def test_host_origin_cors_and_body_limits_remain_enforced(self):
        self.assertEqual(self.request('GET','/api/sessions',headers={'host':'foreign.example'})['status'],403)
        self.assertEqual(self.request('GET','/api/sessions',headers={'origin':'https://foreign.example'})['status'],403)
        response = self.request('OPTIONS','/api/sessions',token=None,headers={
            'access-control-request-method':'POST','access-control-request-headers':'authorization,content-type'})
        self.assertEqual(response['status'],200)
        self.assertEqual(response['headers'][b'access-control-allow-origin'], SETTINGS.origins[0].encode())
        response = self.request('POST','/api/chat/sessions',body=b'x',headers={'content-length':str(2**30)})
        self.assertEqual(response['status'],413)


class DevelopmentAuthenticationTests(AuthenticationTests):
    """Run the existing ownership and failure suite through loopback Auth/CORS."""
    def setUp(self):
        local = replace(SETTINGS, public_host='127.0.0.1:3001',
                        origins=('http://127.0.0.1:5173',))
        settings = patch(__name__ + '.SETTINGS', local)
        settings.start()
        self.addCleanup(settings.stop)
        super().setUp()

    def request(self, method, path, **kwargs):
        kwargs['headers'] = {'host': '127.0.0.1:3001', 'origin': 'http://127.0.0.1:5173',
                             **kwargs.get('headers', {})}
        return super().request(method, path, **kwargs)


class RemoteConfigurationTests(unittest.TestCase):
    def test_shared_transport_revalidates_tokens_and_is_owned_by_lifespan(self):
        from app.auth import RemoteAccessGuard, verify_user
        import httpx
        from unittest.mock import Mock
        async def exercise():
            client = Mock()
            client.get = AsyncMock(side_effect=[
                httpx.Response(200, json={'id': USER_A, 'email': 'a@example.com', 'email_confirmed_at': 'confirmed'}),
                httpx.Response(401),
            ])
            with patch('app.auth.auth_client') as factory:
                self.assertEqual(await verify_user(SETTINGS, 'first', client=client), USER_A)
                self.assertIsNone(await verify_user(SETTINGS, 'revoked', client=client))
                factory.assert_not_called()
            self.assertEqual(client.get.await_count, 2)
            self.assertEqual(client.get.await_args_list[1].kwargs['headers']['Authorization'], 'Bearer revoked')
            async def app(scope, receive, send):
                self.assertIs(guard.client, client)
                if scope['type'] == 'lifespan':
                    with patch('app.auth.verify_user', new=AsyncMock(return_value=USER_A)) as verify:
                        async def send_message(message):
                            pass
                        request = {'type': 'http', 'path': '/api/sessions', 'method': 'GET',
                                   'headers': [(b'host', b'api.example.com'), (b'authorization', b'Bearer token')]}
                        await guard(request, None, send_message)
                        await guard(request, None, send_message)
                        self.assertEqual(verify.await_count, 2)
                        verify.assert_awaited_with(SETTINGS, 'token', client=client)
                        self.assertIsNone(current_user.get())
                else:
                    self.assertEqual(current_user.get(), USER_A)
                    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            guard = RemoteAccessGuard(app, SETTINGS)
            with patch('app.auth.auth_client') as factory:
                factory.return_value.__aenter__.return_value = client
                await guard({'type': 'lifespan'}, None, None)
                factory.return_value.__aexit__.assert_awaited_once()
            self.assertIsNone(guard.client)
        asyncio.run(exercise())

    def test_auth_uses_proxy_environment_only_in_explicit_development(self):
        from app.auth import verify_user
        import httpx
        identity = {'id': USER_A, 'email': 'a@example.com',
                    'email_confirmed_at': '2026-11-10T00:00:00Z'}
        with asyncio.Runner() as runner:
            for mode in ('development', 'remote', 'local', None):
                env = {'APP_MODE': mode} if mode else {}
                with self.subTest(mode=mode), patch.dict(os.environ, env, clear=True), \
                        patch('app.auth.httpx.AsyncClient') as client:
                    get = client.return_value.__aenter__.return_value.get
                    get.return_value = httpx.Response(200, json=identity)
                    self.assertEqual(runner.run(verify_user(SETTINGS, 'synthetic-token')), USER_A)
                    client.assert_called_once_with(timeout=8, trust_env=mode == 'development',
                                                   follow_redirects=False)
                    get.assert_awaited_once_with('https://synthetic.supabase.co/auth/v1/user',
                        headers={'apikey': SETTINGS.publishable_key,
                                 'Authorization': 'Bearer synthetic-token'})

    def test_development_requires_local_addresses_and_production_keeps_https(self):
        env = {'APP_MODE': 'development', 'SUPABASE_URL': 'https://synthetic.supabase.co',
               'SUPABASE_PUBLISHABLE_KEY': 'synthetic-public', 'AUTH_ACCESS_MODE': 'open',
               'PUBLIC_API_URL': 'http://127.0.0.1:3001',
               'ALLOWED_ORIGINS': 'http://127.0.0.1:5173',
               'DATABASE_URL': 'postgresql://synthetic@localhost/dev?sslmode=require'}
        with patch.dict(os.environ, env, clear=True):
            settings = RemoteSettings.from_environment()
            self.assertEqual(settings.public_host, '127.0.0.1:3001')
            self.assertEqual(settings.origins, ('http://127.0.0.1:5173',))
        for change in [
            {'APP_MODE': 'remote'}, {'RENDER': 'true'}, {'VERCEL': '1'},
            {'PUBLIC_API_URL': 'http://0.0.0.0:3001'},
            {'PUBLIC_API_URL': 'http://127.0.0.1.evil.test:3001'},
            {'ALLOWED_ORIGINS': 'http://example.com:5173'},
            {'ALLOWED_ORIGINS': '*'}, {'DATABASE_URL': ''},
            {'DATABASE_URL': 'postgresql://synthetic@localhost/dev'},
            {'SUPABASE_URL': 'http://synthetic.supabase.co'},
            {'SUPABASE_PUBLISHABLE_KEY': ''},
        ]:
            with self.subTest(change=change), patch.dict(os.environ, {**env, **change}, clear=True):
                with self.assertRaises(RuntimeError):
                    RemoteSettings.from_environment()

    def test_open_mode_is_explicit_and_keeps_required_security_configuration(self):
        env = {'APP_MODE':'remote', 'SUPABASE_URL':'https://synthetic.supabase.co',
               'PUBLIC_API_URL':'https://api.example.com', 'ALLOWED_ORIGINS':'https://app.example.com',
               'SUPABASE_PUBLISHABLE_KEY':'synthetic-public',
               'DATABASE_URL':'postgresql://synthetic@localhost/synthetic?sslmode=require'}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError): RemoteSettings.from_environment()
            os.environ['AUTH_ACCESS_MODE'] = 'open'
            self.assertEqual(RemoteSettings.from_environment().access_mode, 'open')
            os.environ['ALLOWED_ORIGINS'] = ''
            with self.assertRaises(RuntimeError): RemoteSettings.from_environment()
        with patch.dict(os.environ, {**env, 'AUTH_ACCESS_MODE':'typo'}, clear=True):
            with self.assertRaises(RuntimeError): RemoteSettings.from_environment()

    def test_open_accepts_new_confirmed_email_but_not_anonymous_or_unverified_identities(self):
        from app.auth import verify_user
        import httpx
        user = {'id':USER_A, 'email':'new@example.com', 'email_confirmed_at':'2026-11-07T00:00:00Z'}
        with asyncio.Runner() as runner:
            for changes, mode, expected in [({}, 'open', USER_A), ({}, 'invited', None),
                    ({'email_confirmed_at':None}, 'open', None), ({'email':''}, 'open', None),
                    ({'is_anonymous':True}, 'open', None), ({'id':'forged'}, 'open', None)]:
                response = httpx.Response(200, json={**user, **changes})
                with patch('app.auth.httpx.AsyncClient') as client:
                    client.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
                    self.assertEqual(runner.run(verify_user(replace(SETTINGS, access_mode=mode), 'synthetic-token')), expected)

    def test_remote_requires_all_settings_and_encrypted_database(self):
        with patch.dict(os.environ, {'APP_MODE':'remote'}, clear=True):
            with self.assertRaises(RuntimeError):
                RemoteSettings.from_environment()
        with patch.dict(os.environ, {'APP_MODE':'local','RENDER':'true'}, clear=True):
            with self.assertRaises(RuntimeError):
                RemoteSettings.from_environment()

    def test_local_v15_upgrade_preserves_rows_and_budget_without_claiming(self):
        from app import db
        with temporary_directory() as root, db.database(root/'upgrade.sqlite') as c:
            with patch.object(db,'MIGRATIONS',db.MIGRATIONS[:15]), patch.object(db,'SCHEMA_VERSION',15):
                db.initialize_database(c)
            c.execute("INSERT INTO sessions(session_id,direction) VALUES ('old','构造旧会话')")
            c.execute("INSERT INTO model_budget_accounts(budget_id,limit_cents,spent_cents) VALUES ('project',1000,545)")
            c.execute("INSERT INTO model_budget_reservations(reservation_id,run_id,reserved_cents,status) VALUES ('pending','old-run',20,'reserved')")
            db.initialize_database(c)
            self.assertIsNone(c.execute('SELECT owner_user_id FROM sessions').fetchone()[0])
            self.assertEqual(c.execute('SELECT spent_cents FROM model_budget_accounts').fetchone()[0],545)
            self.assertEqual(c.execute('SELECT reserved_cents FROM model_budget_reservations').fetchone()[0],20)
