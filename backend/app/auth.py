"""Verified Supabase identities with explicit invited/open access policy."""
from contextvars import ContextVar
from dataclasses import dataclass
import os
from urllib.parse import urlsplit, parse_qs
from uuid import UUID

import httpx


current_user = ContextVar('current_user', default=None)


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
        if mode != 'remote':
            raise RuntimeError('Unsupported APP_MODE.')
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
        if not (https_origin(url) and https_origin(public.geturl()) and origins and all(https_origin(value) for value in origins)
                and key and (emails or access_mode == 'open') and database.scheme in {'postgres', 'postgresql'} and secure):
            raise RuntimeError('Remote configuration incomplete; use private platform environment settings.')
        return cls(url, key, public.netloc, origins, emails, access_mode)


async def verify_user(settings, token):
    # Ask the configured Supabase Auth server to validate the bearer token. Do
    # not trust a decoded JWT payload, client-supplied user ID, or service key.
    async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
        response = await client.get(settings.supabase_url + '/auth/v1/user',
                                    headers={'apikey': settings.publishable_key, 'Authorization': 'Bearer ' + token})
    if response.status_code != 200:
        return None
    try:
        user = response.json()
        identity = str(UUID(user['id']))
        email = user.get('email')
        if (not isinstance(email, str) or not email.strip() or not user.get('email_confirmed_at')
                or user.get('is_anonymous') is True
                or (settings.access_mode != 'open' and email.lower() not in settings.emails)):
            return None
        return identity
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


class RemoteAccessGuard:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        from app.api import error_response
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
        try:
            user = await verify_user(self.settings, authorization[7:])
        except Exception:
            return await error_response('auth_unavailable', '暂时无法核对登录，请稍后重试。', 503)(scope, receive, send)
        if not user:
            return await error_response('unauthorized', '登录已失效或账号尚未获准使用。', 401)(scope, receive, send)
        marker = current_user.set(user)
        try:
            return await self.app(scope, receive, send)
        finally:
            current_user.reset(marker)
