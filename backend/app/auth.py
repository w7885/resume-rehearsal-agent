"""Verified Supabase identities with explicit invited/open access policy."""
from contextvars import ContextVar
from dataclasses import dataclass
import os
import logging
from time import perf_counter
from urllib.parse import urlsplit, parse_qs
from uuid import UUID

import httpx


current_user = ContextVar('current_user', default=None)
current_identity = ContextVar('current_identity', default=None)
LOGGER = logging.getLogger('uvicorn.error')


@dataclass(frozen=True, repr=False)
class VerifiedIdentity:
    id: str
    email: str
    email_verified: bool


@dataclass(frozen=True, repr=False)
class RemoteSettings:
    supabase_url: str
    publishable_key: str
    public_host: str
    origins: tuple[str, ...]
    emails: frozenset[str]
    access_mode: str = 'invited'

    @classmethod
    def from_environment(cls):
        mode = os.getenv('APP_MODE', 'local')
        if mode == 'local':
            if os.getenv('RENDER') == 'true':
                raise RuntimeError('Render requires explicitly configured remote authentication.')
            return None
        development = mode == 'development'
        if mode not in {'remote', 'development'}:
            raise RuntimeError('Unsupported APP_MODE.')
        if development and (os.getenv('RENDER') == 'true' or os.getenv('VERCEL') == '1'):
            raise RuntimeError('Development authentication is restricted to local execution.')
        url = os.getenv('SUPABASE_URL', '').rstrip('/')
        public = urlsplit(os.getenv('PUBLIC_API_URL', ''))
        origins = tuple(value.strip().rstrip('/') for value in os.getenv('ALLOWED_ORIGINS', '').split(',') if value.strip())
        emails = frozenset(value.strip().lower() for value in os.getenv('ALLOWED_USER_EMAILS', '').split(',') if value.strip())
        access_mode = os.getenv('AUTH_ACCESS_MODE', 'invited')
        if access_mode not in {'invited', 'open'}:
            raise RuntimeError('Unsupported AUTH_ACCESS_MODE.')
        key = os.getenv('SUPABASE_PUBLISHABLE_KEY', '')
        database = urlsplit(os.getenv('DATABASE_URL', ''))
        secure = parse_qs(database.query).get('sslmode', []) in [['require'], ['verify-full']]
        def https_origin(value):
            parsed = urlsplit(value)
            return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment
        def local_origin(value):
            parsed = urlsplit(value)
            return (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
                    and not parsed.username and not parsed.password and parsed.path in {'', '/'}
                    and not parsed.query and not parsed.fragment and parsed.port is not None)
        app_origin = local_origin if development else https_origin
        if not (https_origin(url) and app_origin(public.geturl()) and origins and all(app_origin(value) for value in origins)
                and key and (emails or access_mode == 'open') and database.scheme in {'postgres', 'postgresql'} and secure):
            raise RuntimeError('Remote configuration incomplete; use private platform environment settings.')
        return cls(url, key, public.netloc, origins, emails, access_mode)


def auth_client():
    return httpx.AsyncClient(timeout=8, trust_env=os.getenv('APP_MODE') == 'development',
                             follow_redirects=False)


async def verify_user(settings, token, *, client=None):
    # Ask the configured Supabase Auth server to validate the bearer token. Do
    # not trust a decoded JWT payload, client-supplied user ID, or service key.
    # Local developers may need their proxy/CA environment to reach Supabase.
    # Deployed remote mode keeps its explicit, environment-independent transport.
    if client is None:
        async with auth_client() as owned:
            return await verify_user(settings, token, client=owned)
    # Reuse only HTTP transports; verify every token against Auth on every call.
    # Per-request headers prevent identities leaking across pooled connections.
    response = await client.get(settings.supabase_url + '/auth/v1/user',
                                headers={'apikey': settings.publishable_key, 'Authorization': 'Bearer ' + token})
    if response.status_code in {400, 401, 403}:
        return None
    if response.status_code != 200:
        raise ValueError('Unexpected authentication service response.')
    try:
        user = response.json()
        identity = str(UUID(user['id']))
        email = user.get('email')
        if (not isinstance(email, str) or not email.strip() or not user.get('email_confirmed_at')
                or user.get('is_anonymous') is True
                or (settings.access_mode != 'open' and email.lower() not in settings.emails)):
            return None
        return VerifiedIdentity(identity, email, True)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


class RemoteAccessGuard:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.client = None

    async def __call__(self, scope, receive, send):
        from app.api import error_response
        if scope['type'] == 'lifespan':
            async with auth_client() as client:
                self.client = client
                try:
                    return await self.app(scope, receive, send)
                finally:
                    self.client = None
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.lower(): v.decode('latin-1') for k, v in scope['headers']}
        origin = headers.get(b'origin')
        if headers.get(b'host') != self.settings.public_host or (origin and origin not in self.settings.origins):
            return await error_response('scope_violation', '请求来源未获授权。', 403)(scope, receive, send)
        if scope['path'] == '/api/health' and scope['method'] == 'GET':
            return await self.app(scope, receive, send)
        authorization = headers.get(b'authorization', '')
        if not authorization.startswith('Bearer ') or len(authorization) > 16384:
            return await error_response('unauthorized', '请先登录。', 401)(scope, receive, send)
        started = perf_counter()
        try:
            user = await verify_user(self.settings, authorization[7:], client=self.client) if self.client is not None else await verify_user(self.settings, authorization[7:])
        except Exception as error:
            LOGGER.info('remote_auth result=unavailable elapsed_ms=%d error_type=%s',
                        (perf_counter() - started) * 1000, type(error).__name__)
            return await error_response('auth_unavailable', '暂时无法核对登录，请稍后重试。', 503)(scope, receive, send)
        if not user:
            return await error_response('unauthorized', '登录已失效或账号尚未获准使用。', 401)(scope, receive, send)
        marker = current_user.set(user.id)
        identity_marker = current_identity.set(user)
        auth_ms = round((perf_counter() - started) * 1000)
        status = None
        async def timed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)
        try:
            return await self.app(scope, receive, timed_send)
        finally:
            current_identity.reset(identity_marker)
            current_user.reset(marker)
            LOGGER.info('remote_request auth_ms=%d total_ms=%d status=%s',
                        auth_ms, (perf_counter() - started) * 1000, status)
