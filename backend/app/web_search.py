"""Permission/turn/budget controlled web operations for a trusted Agent Host.

No public HTTP proxy, free-form query tool, model calls or automatic retries.
Target semantic assessment and Agent/UI integration belong to the next stage.
"""
import asyncio
import hashlib
import time

import httpx

from app.agent_budget import ProjectBudget
from app.repository import RepositoryError, RequestContext
from app.web_provider import (PublicTarget, VolcWebClient, WebProviderError, WebSettings,
                              public_url_shape, reference_identity)
from app.web_storage import close_request, request_result

REQUEST_TIMEOUT_SECONDS = 15


class WebSearchService:
    def __init__(self, repo, settings=None, *, client=None, limit_cents=3000):
        self.repo = repo
        self.settings = settings if settings is not None else WebSettings.from_env()
        self.client = client if client is not None else VolcWebClient(self.settings)
        self.limit_cents = limit_cents

    def _guard(self, context, revision):
        if not isinstance(context, RequestContext) or context.run_id is None:
            raise RepositoryError('scope_violation', '联网必须绑定当前运行。', False)
        current = self.repo.get_web_settings(context)
        if not current['enabled'] or current['webSettingsRevision'] != revision:
            raise RepositoryError('web_disabled', '联网已关闭或设置变化，本次结果不可使用。')

    async def _guarded_io(self, context, revision, deadline, operation):
        """Best-effort abort in-flight I/O; the post-response guard is mandatory."""
        async def watch_permission():
            while True:
                await asyncio.sleep(0.05)
                self._guard(context, revision)

        io = asyncio.create_task(operation())
        watcher = asyncio.create_task(watch_permission())
        try:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise WebProviderError('web_timeout')
            done, _ = await asyncio.wait({io, watcher}, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise WebProviderError('web_timeout' if remaining <= REQUEST_TIMEOUT_SECONDS else 'provider_timeout')
            if watcher in done:
                await watcher  # propagates the permission/context error
            value = await io
            self._guard(context, revision)
            return value
        finally:
            for task in (io, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(io, watcher, return_exceptions=True)

    def _preserve_status_conflicts(self, context, target_key, references):
        with self.repo._operation(context) as (connection, _):
            old = connection.execute('''SELECT e.url,e.hiring_status FROM external_references e
                JOIN web_requests w ON w.session_id=e.session_id AND w.request_id=e.request_id
                WHERE e.session_id=? AND w.target_key=?''', (context.session_id, target_key)).fetchall()
        for item in references:
            identity = reference_identity(item['url'])
            states = [{'url': row['url'], 'status': row['hiring_status']} for row in old]
            states.extend({'url': ref['url'], 'status': ref['hiringStatus']} for ref in references)
            for row in states:
                if reference_identity(row['url']) != identity:
                    continue
                previous = row['status']
                if previous == 'conflict' or (previous == 'closed' and item['hiringStatus'] != 'closed'):
                    item['hiringStatus'] = 'conflict'
        return references

    async def search(self, context, target, *, request_id, web_settings_revision, short_query=False, purpose='job_requirements'):
        if type(short_query) is not bool:
            raise WebProviderError('invalid_public_target')
        if not isinstance(target, PublicTarget):
            raise WebProviderError('invalid_public_target')
        query = target.query(short=short_query, purpose=purpose)
        return await self._perform(context, target, request_id, web_settings_revision, 'search',
                                   {'query': query, 'purpose': purpose}, lambda dispatch: self.client.search(query, dispatch))

    async def fetch(self, context, target, url, *, request_id, web_settings_revision):
        if not isinstance(target, PublicTarget):
            raise WebProviderError('invalid_public_target')
        url = public_url_shape(url)
        # A fetch is only allowed for a URL actually discovered in this permission
        # epoch, target, resume generation and user turn; never arbitrary model URLs.
        self._guard(context, web_settings_revision)
        with self.repo._operation(context) as (connection, session):
            root = connection.execute('SELECT root_run_id FROM agent_run_requests WHERE session_id=? AND current_run_id=?',
                                      (context.session_id, context.run_id)).fetchone()
            root_id = root[0] if root else context.run_id
            found = connection.execute('''SELECT e.url FROM external_references e
                JOIN web_requests w ON w.session_id=e.session_id AND w.request_id=e.request_id
                WHERE w.session_id=? AND coalesce(w.scope_run_id,w.run_id)=? AND w.target_key=?
                AND w.web_settings_revision=? AND w.resume_generation=? AND w.status='succeeded' AND w.operation='search' ''',
                (context.session_id, root_id, target.key, web_settings_revision, session['current_resume_generation'])).fetchall()
        if url not in {public_url_shape(row['url']) for row in found}:
            raise WebProviderError('undiscovered_url')
        return await self._perform(context, target, request_id, web_settings_revision, 'fetch',
                                   {'url': url}, lambda dispatch: self.client.fetch(url, dispatch))

    async def _perform(self, context, target, request_id, revision, operation, payload, call):
        self._guard(context, revision)
        if not self.settings.available:
            return {'status': 'unavailable', 'code': 'web_unavailable', 'references': [], 'needsJD': True}
        reservation_id = 'web-' + hashlib.sha256((context.session_id + '\0' + request_id).encode()).hexdigest()
        request = self.repo.begin_web_request(context, request_id=request_id,
            web_settings_revision=revision, target_key=target.key, operation=operation, payload=payload,
            budget_reservation_id=reservation_id)
        if not request['created']:
            result = request_result(self.repo, context, request_id, revision, target.key)
            return {**result, 'replayed': True, 'needsJD': True}
        budget = ProjectBudget(self.repo.path, request['scopeRunId'], self.limit_cents)
        # No verifiable live free-balance/billing receipt is provided by these
        # endpoints. Reserve a paid upper bound; never infer zero from missing usage.
        upper_cents = 2 if operation == 'search' else 1
        if not budget.reserve_request(reservation_id, upper_cents):
            close_request(self.repo, context, request_id, status='failed', error_code='budget_exhausted')
            return {'status': 'limited', 'code': 'budget_exhausted', 'references': [], 'needsJD': True}
        dispatched = False
        settlement = None
        result = None
        error_code = None

        def on_dispatch():
            nonlocal dispatched
            self._guard(context, revision)
            if time.time() >= request['deadlineAt']:
                raise WebProviderError('web_timeout')
            dispatched = True

        try:
            self._guard(context, revision)
            refs, warnings = await self._guarded_io(context, revision, request['deadlineAt'], lambda: call(on_dispatch))
            refs = self._preserve_status_conflicts(context, target.key, refs)
            settlement = budget.settle_request(reservation_id, None if dispatched else 0)
            usage = {**settlement, 'providerUsage': 'unverified', 'estimatedUpperCents': upper_cents}
            result = self.repo.finish_web_request(context, request_id=request_id,
                web_settings_revision=revision, target_key=target.key, references=refs, usage=usage)
            self._guard(context, revision)
            result.update(needsJD=True, warnings=warnings,
                          status='partial' if refs else 'empty', assessment='target_match_and_freshness_unconfirmed')
            return result
        except asyncio.CancelledError:
            error_code = 'cancelled'
            raise
        except RepositoryError as error:
            error_code = error.code
            raise
        except (WebProviderError, httpx.HTTPError, TimeoutError) as error:
            error_code = error.code if isinstance(error, WebProviderError) else 'provider_timeout' if isinstance(error, (httpx.TimeoutException, TimeoutError)) else 'provider_network_error'
            return {'status': 'failed', 'code': error_code, 'references': [], 'needsJD': True}
        finally:
            # Named settlement is idempotent, works after permission revocation,
            # and cannot consume a simultaneous model call's FIFO reservation.
            if settlement is None:
                settlement = budget.settle_request(reservation_id, None if dispatched else 0)
            if result is None:
                close_request(self.repo, context, request_id,
                    status='cancelled' if error_code in {'cancelled', 'web_disabled', 'stale_state'} else 'unknown' if dispatched else 'failed',
                    error_code=error_code or 'web_failed',
                    usage={**settlement, 'providerUsage': 'unverified', 'estimatedUpperCents': upper_cents})
