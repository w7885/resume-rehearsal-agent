"""Bounded Volcengine Global/Fetch adapter. Web text remains untrusted data."""
import asyncio
from dataclasses import dataclass, field
import ipaddress
import os
import re
import socket
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class WebProviderError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WebSettings:
    api_key: str = field(default='', repr=False)
    enabled: bool = False
    budget_confirmed: bool = False

    @classmethod
    def from_env(cls):
        return cls(os.getenv('VOLC_SEARCH_API_KEY', '').strip(),
                   os.getenv('WEB_SEARCH_ENABLED') == '1', os.getenv('AGENT_BUDGET_CONFIRMED') == '1')

    @property
    def available(self):
        return self.enabled and self.budget_confirmed and bool(self.api_key)


@dataclass(frozen=True)
class PublicTarget:
    """Constructed from trusted, clarified public target fields, never resume text."""
    company: str
    role: str
    location: str = ''
    employment_type: str = ''
    posting_id: str = ''

    def __post_init__(self):
        limits = {'company': 60, 'role': 80, 'location': 30, 'employment_type': 20, 'posting_id': 64}
        for name, maximum in limits.items():
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > maximum or (name in ('company', 'role') and not value.strip()):
                raise WebProviderError('invalid_public_target')
            if re.search(r'[\r\n\x00-\x1f@{}<>]|https?://|(?<!\d)1[3-9]\d{9}(?!\d)', value, re.I):
                raise WebProviderError('private_query_rejected')

    def query(self, *, short=False, purpose='job_requirements'):
        if purpose == 'company_background':
            parts = [self.company, '产品 业务介绍']
        elif purpose == 'terminology':
            parts = [self.company, self.role, '岗位术语']
        elif purpose == 'job_requirements':
            parts = [self.company, self.role, self.posting_id] if short else [self.company, self.role, self.location, self.employment_type, self.posting_id]
        else:
            raise WebProviderError('invalid_purpose')
        query = ' '.join(x.strip() for x in parts if x.strip())
        if len(query) > 100:
            raise WebProviderError('query_too_long')
        return query

    @property
    def key(self):
        import hashlib
        import json
        values = [self.company, self.role, self.location, self.employment_type, self.posting_id]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def public_url_shape(url):
    """Reject private literals, credentials and non-web protocols before any DNS."""
    if not isinstance(url, str) or len(url) > 4096 or re.search(r'[\x00-\x20\\]', url):
        raise WebProviderError('unsafe_url')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password:
            raise ValueError()
        if parsed.port not in (None, 80 if parsed.scheme == 'http' else 443):
            raise ValueError()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        for name, _ in parse_qsl(parsed.query, keep_blank_values=True):
            if name.lower() in {'api_key', 'apikey', 'access_token', 'token', 'auth', 'password', 'authorization',
                                'secret', 'secret_key', 'access_key', 'signature', 'x-amz-signature', 'x-amz-security-token'}:
                raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host):
                raise ValueError()
        else:
            mapped = getattr(ip, 'ipv4_mapped', None)
            if not ip.is_global or ip.is_multicast or (mapped and not mapped.is_global):
                raise ValueError()
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', parsed.query, ''))
    except (ValueError, UnicodeError, AttributeError):
        raise WebProviderError('unsafe_url') from None


async def resolve_host(host):
    values = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [row[4][0] for row in values]


async def public_url(url, resolver):
    url = public_url_shape(url)
    host = urlsplit(url).hostname
    try:
        addresses = await resolver(host)
        if not addresses or any(not ipaddress.ip_address(x).is_global or ipaddress.ip_address(x).is_multicast for x in addresses):
            raise WebProviderError('unsafe_url')
    except WebProviderError:
        raise
    except Exception:
        raise WebProviderError('url_unverifiable') from None
    return url


def reference_identity(url):
    parsed = urlsplit(url)
    # Desktop/mobile pages of the same posting remain one identity for conflict detection.
    if parsed.hostname in ('www.shixiseng.com', 'wap.shixiseng.com', 'm.shixiseng.com'):
        match = re.fullmatch(r'/intern/(inn_[A-Za-z0-9]+)/*', parsed.path)
        if match:
            return 'shixiseng:' + match[1]
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip('/'), parsed.query, ''))


def normalized_reference(title, url, content, *, full_body=False, published_at=None):
    closed = bool(re.search(r'当前职位已下线|该职位已下线|职位已关闭|no longer available|position (?:is )?closed', content, re.I))
    duties = re.search(r'岗位职责|工作内容|职责|responsibilit|what you.ll build', content, re.I)
    requirements = re.search(r'任职资格|工作要求|岗位要求|职位要求|qualifications|requirements|what you.ll bring', content, re.I)
    sufficient = full_body and len(content) >= 120 and duties and requirements
    return {'title': title[:500], 'url': url, 'content': content[:50000],
            'publishedAt': published_at if isinstance(published_at, str) and published_at.strip() else None,
            'matchStatus': 'unconfirmed', 'hiringStatus': 'closed' if closed else 'unconfirmed',
            'contentStatus': 'unavailable' if not content.strip() or closed else 'sufficient' if sufficient and len(content) <= 50000 else 'partial'}


class VolcWebClient:
    def __init__(self, settings, *, transport=None, resolver=resolve_host):
        self.settings, self.transport, self.resolver = settings, transport, resolver

    async def _request(self, operation, payload, on_dispatch):
        endpoint = 'global_search' if operation == 'search' else 'fetch'
        async with httpx.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False, trust_env=False) as client:
            on_dispatch()
            response = await client.post('https://open.feedcoopapi.com/search_api/' + endpoint,
                headers={'Authorization': 'Bearer ' + self.settings.api_key}, json=payload)
        if response.status_code != 200:
            raise WebProviderError('provider_rate_limited' if response.status_code == 429 else 'provider_http_error')
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise WebProviderError('provider_response_too_large')
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError()
            metadata = body.get('ResponseMetadata')
            result = body.get('Result')
            if not isinstance(metadata, dict):
                raise ValueError()
            error = metadata.get('Error')
            if error:
                code = str(error.get('Code') or error.get('CodeN') or '') if isinstance(error, dict) else ''
                mapping = {'10406': 'provider_quota_exhausted', '10412': 'provider_quota_exhausted',
                           '10403': 'provider_permission_denied', '700901': 'provider_permission_denied',
                           '700429': 'provider_rate_limited', '10413': 'source_access_denied'}
                raise WebProviderError(mapping.get(code, 'provider_api_error'))
            if not isinstance(result, dict) or result.get('ErrorCode', 0) != 0:
                raise WebProviderError('provider_api_error')
            return result
        except WebProviderError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise WebProviderError('provider_invalid_response') from None

    def _clean(self, text):
        return text.replace(self.settings.api_key, '[REDACTED]') if self.settings.api_key else text

    async def search(self, query, on_dispatch):
        result = await self._request('search', {'Query': query, 'SearchType': 'web', 'DocCount': 5,
            'MaxSnippetLength': 1000, 'EnableWaiting': False, 'Filter': {'IcpHostOnly': False}}, on_dispatch)
        documents = result.get('Documents')
        if documents is None and result.get('TotalDocCount') == 0:
            documents = []
        if not isinstance(documents, list):
            raise WebProviderError('provider_invalid_response')
        references, warnings = [], []
        for item in documents[:5]:
            if not isinstance(item, dict) or not isinstance(item.get('Title'), str) or not item['Title'].strip():
                warnings.append('invalid_candidate')
                continue
            try:
                if not isinstance(item.get('Url'), str):
                    raise WebProviderError('unsafe_url')
                if self.settings.api_key and self.settings.api_key in item['Url']:
                    raise WebProviderError('unsafe_url')
                url = await public_url(item.get('Url'), self.resolver)
            except WebProviderError:
                warnings.append('unsafe_candidate_dropped')
                continue
            snippets = item.get('Snippet')
            if not isinstance(snippets, list) or any(not isinstance(x, dict) for x in snippets):
                raise WebProviderError('provider_invalid_response')
            texts = [x.get('Text') for x in snippets if x.get('Type') == 'text']
            if any(not isinstance(x, str) for x in texts):
                raise WebProviderError('provider_invalid_response')
            info = item.get('DocumentInfo') or {}
            if not isinstance(info, dict):
                raise WebProviderError('provider_invalid_response')
            references.append(normalized_reference(self._clean(item['Title']), url,
                self._clean('\n'.join(texts)), published_at=info.get('PublishTime')))
        return references, warnings

    async def fetch(self, url, on_dispatch):
        url = await public_url(url, self.resolver)
        result = await self._request('fetch', {'Url': url}, on_dispatch)
        data = result.get('Data')
        if not isinstance(data, dict) or type(data.get('StatusCode')) is not int:
            raise WebProviderError('provider_invalid_response')
        final_url = data.get('RedirectedUrl') or data.get('Url') or url
        if not isinstance(final_url, str):
            raise WebProviderError('provider_invalid_response')
        if self.settings.api_key and self.settings.api_key in final_url:
            raise WebProviderError('unsafe_url')
        final_url = await public_url(final_url, self.resolver)
        text = data.get('ContentText')
        title = data.get('Title')
        if text is not None and not isinstance(text, str):
            raise WebProviderError('provider_invalid_response')
        if title is not None and not isinstance(title, str):
            raise WebProviderError('provider_invalid_response')
        warnings = [] if data['StatusCode'] == 200 else ['source_unavailable']
        content = self._clean(text or '') if data['StatusCode'] == 200 else ''
        ref = normalized_reference(self._clean(title or '未取得网页标题'), final_url, content, full_body=True)
        return [ref], warnings
