"""Permission/turn/budget controlled web operations for a trusted Agent Host.

No public HTTP proxy, free-form query tool, model calls or automatic retries.
Target semantic assessment and Agent/UI integration belong to the next stage.
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from app import web_intake
from app.agent_budget import ProjectBudget
from app.repository import RepositoryError, RequestContext
from app.web_provider import (PublicTarget, PublicSearchIntent, VolcWebClient, WebProviderError, WebSettings,
                              public_url_shape, reference_identity, same_posting_url)
from app.web_storage import _references, close_request, request_result

REQUEST_TIMEOUT_SECONDS = 15


class WebSearchService:
    def __init__(self, repo, settings=None, *, client=None, limit_cents=3000):
        self.repo = repo
        self.settings = settings if settings is not None else WebSettings.from_env()
        self.client = client if client is not None else VolcWebClient(self.settings)
        self.limit_cents = limit_cents

    def bind_user_jd_links(self, context, *, kind, source_id, source_revision=None):
        from app.web_inputs import bind_links
        with self.repo._operation(context, write=True) as (connection, session):
            return bind_links(connection, session, kind=kind, source_id=source_id,
                              source_revision=source_revision)

    def _episode(self, context, target_key):
        from app import web_intake
        from app.web_citations import goal_state
        from app.web_target import _put
        with self.repo._operation(context, write=True) as (connection, session):
            state = web_intake.ensure(connection, session, goal_state(connection, session)['baseHash'])
            imported = state.get('legacyImportedTargets', [])
            if target_key not in imported and not state.get('skipLegacyImport'):
                rows = connection.execute('''SELECT operation,input_json FROM web_requests
                    WHERE session_id=? AND target_key=? AND resume_generation=?''',
                    (context.session_id, target_key, session['current_resume_generation'])).fetchall()
                if not rows:
                    return state
                state['searchCount'] = min(2, state['searchCount'] + sum(row['operation'] == 'search' for row in rows))
                pages = list(state['fetchIdentities'])
                for row in rows:
                    if row['operation'] == 'fetch':
                        url = json.loads(row['input_json']).get('url')
                        if url:
                            identity = reference_identity(url)
                            if identity not in pages:
                                pages.append(identity)
                state['fetchIdentities'] = pages[:3]
                state['legacyImportedTargets'] = [*imported, target_key]
                state['revision'] += 1
                _put(connection, session['session_id'], web_intake.KEY, state)
            return state

    async def discover(self, context, intent, *, request_id, web_settings_revision):
        """Explore explicit public intent; results remain unconfirmed candidates."""
        from app.web_citations import goal_state
        if not isinstance(intent, PublicSearchIntent):
            raise WebProviderError('invalid_public_target')
        self._guard(context, web_settings_revision)
        def validate():
            with self.repo._operation(context) as (connection, session):
                goal = goal_state(connection, session)
                proof = goal['direction'] + '\n' + goal['jd']
                if any(value and value not in proof for value in intent.__dict__.values()):
                    raise WebProviderError('unanchored_public_intent')
                return goal['baseHash']
        bound_hash = validate()
        def guard():
            if validate() != bound_hash:
                raise RepositoryError('stale_state', '求职意向已变化。')
        episode = self._episode(context, intent.key)
        target = SimpleNamespace(key=hashlib.sha256(episode['episodeId'].encode()).hexdigest())
        return await self._perform(context, target, request_id, web_settings_revision, 'search',
            {'query': intent.query(), 'purpose': 'job_discovery'},
            lambda dispatch: self.client.search(intent.query(), dispatch),
            intake_binding={'episodeId': episode['episodeId'], 'revision': episode['revision']},
            input_guard=guard)

    async def fetch_user_jd(self, context, binding_id, *, request_id, web_settings_revision):
        from app.web_inputs import resolve_link
        from app.web_citations import goal_state
        self._guard(context, web_settings_revision)
        with self.repo._operation(context) as (connection, session):
            url = resolve_link(connection, session, binding_id)
            goal_hash = goal_state(connection, session)['baseHash']
        def guard():
            with self.repo._operation(context) as (connection, session):
                if (resolve_link(connection, session, binding_id) != url
                        or goal_state(connection, session)['baseHash'] != goal_hash):
                    raise RepositoryError('stale_state', 'JD链接或求职意向已变化。')
        target = SimpleNamespace(key=hashlib.sha256(('user-jd\0' + goal_hash).encode()).hexdigest())
        episode = self._episode(context, target.key)
        target = SimpleNamespace(key=hashlib.sha256(episode['episodeId'].encode()).hexdigest())
        return await self._perform(context, target, request_id, web_settings_revision, 'fetch',
            {'url': url, 'sourceKind': 'user_jd', 'bindingId': binding_id},
            lambda dispatch: self.client.fetch(url, dispatch),
            intake_binding={'episodeId': episode['episodeId'], 'revision': episode['revision']},
            input_guard=guard, result_guard=lambda refs: all(same_posting_url(url, ref['url']) for ref in refs))

    def previous_user_jd(self, context, binding_id, *, request_id, web_settings_revision):
        from app.web_inputs import resolve_link
        self._guard(context, web_settings_revision)
        with self.repo._operation(context) as (connection, session):
            url = resolve_link(connection, session, binding_id)
            state = web_intake.load(connection, context.session_id)
            key = hashlib.sha256(state['episodeId'].encode()).hexdigest()
            row = connection.execute('''SELECT status FROM web_requests WHERE session_id=?
                AND request_id=? AND target_key=? AND resume_generation=? AND web_settings_revision=?
                AND operation='fetch' AND json_extract(input_json,'$.bindingId')=?
                AND json_extract(input_json,'$.url')=?''', (context.session_id, request_id, key,
                session['current_resume_generation'], web_settings_revision, binding_id, url)).fetchone()
            if row is None:
                return None
            return {'status': row['status'], 'references': _references(connection, context.session_id, request_id)}

    async def fetch_candidate(self, context, reference_id, *, request_id, web_settings_revision, recover=False, reuse=False):
        """Read a persisted discovery candidate, including after a user selection turn."""
        from app import web_intake
        from app.web_citations import goal_state
        self._guard(context, web_settings_revision)
        def binding():
            with self.repo._operation(context) as (connection, session):
                state = web_intake.load(connection, session['session_id'])
                if (not state or state['intentHash'] != goal_state(connection, session)['baseHash']
                        or state['permissionRevision'] != web_settings_revision
                        or state['resumeGeneration'] != session['current_resume_generation']):
                    raise RepositoryError('stale_state', '探索目标或来源已变化。')
                key = hashlib.sha256(state['episodeId'].encode()).hexdigest()
                row = connection.execute('''SELECT e.url FROM external_references e
                    JOIN web_requests w ON w.session_id=e.session_id AND w.request_id=e.request_id
                    WHERE e.session_id=? AND e.reference_id=? AND w.target_key=?
                    AND w.web_settings_revision=? AND w.resume_generation=?
                    AND w.status='succeeded' AND w.operation='search' ''',
                    (context.session_id, reference_id, key, web_settings_revision,
                     session['current_resume_generation'])).fetchone()
                if not row:
                    raise WebProviderError('undiscovered_url')
                return state, key, public_url_shape(row['url'])
        state, key, url = binding()
        if reuse:
            with self.repo._operation(context) as (connection, session):
                row = connection.execute('''SELECT status FROM web_requests WHERE session_id=? AND request_id=?
                    AND target_key=? AND resume_generation=? AND web_settings_revision=? AND operation='fetch'
                    AND json_extract(input_json,'$.referenceId')=?''', (context.session_id, request_id, key,
                    session['current_resume_generation'], web_settings_revision, reference_id)).fetchone()
                return None if row is None else {'status': row['status'],
                    'references': _references(connection, context.session_id, request_id)}
        def guard():
            _, current_key, current_url = binding()
            if current_key != key or current_url != url:
                raise RepositoryError('stale_state', '候选来源已变化。')
        if recover:
            parsed = urlsplit(url)
            match = re.search(r'(?<!\w)(?:\d{6,}|inn_[A-Za-z0-9]+)(?!\w)', parsed.path)
            if not match:
                return {'references': []}
            query = f'site:{parsed.hostname} {match.group()}'
            if len(query) > 100:
                return {'references': []}
            return await self._perform(context, SimpleNamespace(key=key), request_id, web_settings_revision,
                'search', {'query': query, 'purpose': 'job_discovery', 'referenceId': reference_id},
                lambda dispatch: self.client.search(query, dispatch),
                intake_binding={'episodeId': state['episodeId'], 'revision': state['revision']}, input_guard=guard)
        return await self._perform(context, SimpleNamespace(key=key), request_id,
            web_settings_revision, 'fetch', {'url': url, 'sourceKind': 'search_result',
                                            'referenceId': reference_id},
            lambda dispatch: self.client.fetch(url, dispatch),
            intake_binding={'episodeId': state['episodeId'], 'revision': state['revision']},
            input_guard=guard)

    async def search_user_jd(self, context, binding_id, *, request_id, web_settings_revision):
        """One deterministic same-posting recovery search, not a similar-job query."""
        from app.web_inputs import resolve_link
        from app.web_citations import goal_state
        self._guard(context, web_settings_revision)
        with self.repo._operation(context) as (connection, session):
            url = resolve_link(connection, session, binding_id)
            goal_hash = goal_state(connection, session)['baseHash']
        parsed = urlsplit(url)
        match = re.search(r'(?<!\w)(?:\d{6,}|inn_[A-Za-z0-9]+)(?!\w)', parsed.path)
        if not match:
            raise WebProviderError('missing_posting_identity')
        query = f'site:{parsed.hostname} {match.group()}'
        if len(query) > 100:
            raise WebProviderError('query_too_long')
        target = SimpleNamespace(key=hashlib.sha256(('user-jd\0' + goal_hash).encode()).hexdigest())
        episode = self._episode(context, target.key)
        target = SimpleNamespace(key=hashlib.sha256(episode['episodeId'].encode()).hexdigest())
        def guard():
            with self.repo._operation(context) as (connection, session):
                if (resolve_link(connection, session, binding_id) != url
                        or goal_state(connection, session)['baseHash'] != goal_hash):
                    raise RepositoryError('stale_state', 'JD链接已变化。')
        return await self._perform(context, target, request_id, web_settings_revision, 'search',
            {'query': query, 'purpose': 'job_discovery', 'bindingId': binding_id},
            lambda dispatch: self.client.search(query, dispatch),
            intake_binding={'episodeId': episode['episodeId'], 'revision': episode['revision']}, input_guard=guard)

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

    async def _perform(self, context, target, request_id, revision, operation, payload, call,
                       *, intake_binding=None, input_guard=None, result_guard=None):
        self._guard(context, revision)
        if input_guard:
            input_guard()
        if not self.settings.available:
            return {'status': 'unavailable', 'code': 'web_unavailable', 'references': [], 'needsJD': True}
        reservation_id = 'web-' + hashlib.sha256((context.session_id + '\0' + request_id).encode()).hexdigest()
        request = self.repo.begin_web_request(context, request_id=request_id,
            web_settings_revision=revision, target_key=target.key, operation=operation, payload=payload,
            budget_reservation_id=reservation_id, intake_binding=intake_binding)
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
            if input_guard:
                input_guard()
            if time.time() >= request['deadlineAt']:
                raise WebProviderError('web_timeout')
            dispatched = True

        try:
            self._guard(context, revision)
            refs, warnings = await self._guarded_io(context, revision, request['deadlineAt'], lambda: call(on_dispatch))
            if input_guard:
                input_guard()
            if result_guard and not result_guard(refs):
                raise WebProviderError('posting_redirect_mismatch')
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
            # Log only stable categories; exception text can contain URLs or credentials.
            logging.getLogger(__name__).warning(
                'web_provider_failure operation=%s code=%s exception_type=%s',
                operation, error_code, type(error).__name__)
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
