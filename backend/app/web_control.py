"""Persisted, user-authorized web-setting events, independent of chat requests.

Uses existing agent_state and runs tables. Never creates a fake user message,
changes the normal message/request uniqueness rule, or reapplies a chat decision.
"""
import asyncio
import json
import re

from app.agent_host import Runtime
from app.db import database, transaction
from app.repository import RepositoryError, RequestContext
from app.web_target import _load, _put

PENDING = 'web_control_pending'
PREFIX = 'web_control:'


def changed(connection, session, enabled, revision):
    if enabled:
        value = {'requestId': f'web-settings-{revision}', 'webSettingsRevision': revision, 'status': 'pending'}
    else:
        value = {'webSettingsRevision': revision, 'status': 'cancelled'}
    _put(connection, session['session_id'], PENDING, value)


def consume_for_message(connection, session):
    value = _load(connection, session['session_id'], PENDING)
    if value and value['status'] == 'pending' and value['webSettingsRevision'] == session['web_settings_revision']:
        value.update(status='done', outcome='handled_by_user_message')
        _put(connection, session['session_id'], PENDING, value)


def status(host, sid):
    with host.repo._operation(RequestContext(sid)) as (c, s):
        pending = _load(c, sid, PENDING)
        if not pending or not s['web_enabled'] or pending['webSettingsRevision'] != s['web_settings_revision']:
            return {'sessionId': sid, 'status': 'idle'}
        record = _load(c, sid, PREFIX + pending.get('requestId', ''))
        if record:
            return {'sessionId': sid, 'status': record['status'], 'requestId': record['requestId'],
                    'webSettingsRevision': record['webSettingsRevision'],
                    'expectedStateRevision': record['inputStateRevision'], 'runId': record.get('currentRunId')}
        return {'sessionId': sid, **pending, 'expectedStateRevision': s['state_revision']}


def snapshot(host, sid, request_id):
    with host.repo._operation(RequestContext(sid)) as (c, _):
        value = _load(c, sid, PREFIX + request_id)
        if not value:
            raise RepositoryError('source_not_found', '联网接续记录不存在。')
        return {'requestId': request_id, 'runId': value['rootRunId'],
                'status': value['status'], 'outcome': value.get('outcome'), 'events': value['events']}


def authorizes(connection, session, context, user_id):
    if not context.run_id:
        return False
    rows = connection.execute("SELECT doc_json FROM agent_state WHERE session_id=? AND scope='session' AND key LIKE 'web_control:%'",
                              (session['session_id'],))
    for row in rows:
        value = json.loads(row[0])
        if (value['status'] == 'running' and value['currentRunId'] == context.run_id
                and value['userMessageId'] == user_id and value['stateRevision'] == session['state_revision']
                and value['resumeGeneration'] == session['current_resume_generation']
                and session['web_enabled'] and value['webSettingsRevision'] == session['web_settings_revision']):
            return True
    return False


def running(connection, sid):
    return bool(connection.execute("""SELECT 1 FROM agent_state a JOIN runs r
        ON r.session_id=a.session_id AND r.run_id=json_extract(a.doc_json,'$.currentRunId')
        WHERE a.session_id=? AND a.scope='session' AND a.key LIKE 'web_control:%'
        AND json_extract(a.doc_json,'$.status')='running' AND r.status='running' LIMIT 1""", (sid,)).fetchone())


def start(host, sid, payload):
    with host.repo._operation(RequestContext(sid), write=True) as (c, s):
        pending = _load(c, sid, PENDING)
        if (not pending or pending.get('requestId') != payload['requestId']
                or not s['web_enabled'] or s['web_settings_revision'] != payload['webSettingsRevision']):
            raise RepositoryError('stale_state', '联网接续事件已失效。')
        old = _load(c, sid, PREFIX + payload['requestId'])
        if old:
            if old['inputStateRevision'] != payload['expectedStateRevision']:
                raise RepositoryError('request_conflict', '接续事件不能更换已绑定的版本。')
            return {'request_id': old['requestId']}, False
        if pending.get('outcome') == 'needs_user_message':
            return None, False
        if pending['status'] != 'pending':
            raise RepositoryError('stale_state', '本次联网接续已由新消息处理。')
        if not host.enabled or s['mode'] != 'local':
            raise RepositoryError('agent_unavailable', '当前无法自动接续；已有资料保持可用。')
        host.repo._check_context(c, RequestContext(sid, payload['expectedStateRevision']))
        if c.execute("SELECT 1 FROM runs WHERE session_id=? AND status='running'", (sid,)).fetchone():
            raise RepositoryError('run_busy', '当前处理尚未结束，联网接续已保留。')
        if any(key[0] == sid and runtime.task and not runtime.task.done() for key, runtime in list(host.active.items())):
            raise RepositoryError('run_busy', '当前处理仍在清理，联网接续已保留。')
        if c.execute('SELECT 1 FROM agent_run_requests WHERE session_id=? AND request_id=?',
                     (sid, payload['requestId'])).fetchone():
            raise RepositoryError('request_conflict', '事件编号与消息请求冲突。')
        user = c.execute("""SELECT message_id FROM messages WHERE session_id=? AND role='user'
            AND input_channel='chat' AND use_status='active' AND resume_generation=?
            ORDER BY rowid DESC LIMIT 1""", (sid, s['current_resume_generation'])).fetchone()
        if not user:
            pending.update(status='done', outcome='needs_user_message')
            _put(c, sid, PENDING, pending)
            return None, False
        account = host.budget()._account(c)
        reserved = c.execute("SELECT coalesce(sum(reserved_cents),0) FROM model_budget_reservations WHERE status='reserved'").fetchone()[0]
        if account['spent_cents'] + reserved >= account['limit_cents']:
            raise RepositoryError('agent_unavailable', '累计预算不足，未发起联网接续。')
        run_id, generation = host._new_run(c, s)
        value = {'formatVersion': 1, 'requestId': payload['requestId'], 'status': 'running',
                 'webSettingsRevision': s['web_settings_revision'], 'stateRevision': s['state_revision'],
                 'inputStateRevision': s['state_revision'], 'draftRevision': s['draft_revision'],
                 'resumeGeneration': s['current_resume_generation'], 'userMessageId': user['message_id'],
                 'rootRunId': run_id, 'currentRunId': run_id, 'events': []}
        _put(c, sid, PREFIX + payload['requestId'], value)
        pending['status'] = 'running'
        _put(c, sid, PENDING, pending)
    row = {'session_id': sid, 'request_id': payload['requestId'], 'user_message_id': user['message_id'], 'root_run_id': run_id}
    runtime = ControlRuntime(host, row, RequestContext(sid, payload['expectedStateRevision'], run_id, generation))
    host.active[(sid, payload['requestId'])] = runtime
    return {'request_id': payload['requestId']}, True


async def cancel_if_control(host, run_id):
    with database(host.repo.path, read_only=True) as c:
        row = c.execute("""SELECT session_id,doc_json FROM agent_state WHERE scope='session'
            AND key LIKE 'web_control:%' AND (json_extract(doc_json,'$.rootRunId')=?
            OR json_extract(doc_json,'$.currentRunId')=?)""", (run_id, run_id)).fetchone()
    if not row:
        return None
    value = json.loads(row['doc_json'])
    runtime = host.active.get((row['session_id'], value['requestId']))
    if runtime:
        runtime.finish('interrupted', 'cancelled', check=False)
        if runtime.task and runtime.task is not asyncio.current_task():
            if value['status'] == 'running':
                runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
    return {'runId': run_id, 'status': 'interrupted' if value['status'] == 'running' else value['status']}


class ControlRuntime(Runtime):
    control_event = True
    def _assert_control(self):
        with self.repo._operation(self.context) as (c, s):
            if not authorizes(c, s, self.context, self.user_id):
                raise RepositoryError('stale_state', '联网控制事件已失效。')

    def _reply(self, *args, **kwargs):
        self._assert_control()
        return super()._reply(*args, **kwargs)

    async def _json(self, *args, **kwargs):
        self._assert_control()
        result = await super()._json(*args, **kwargs)
        self._assert_control()
        return result

    def _record(self, connection):
        value = _load(connection, self.sid, PREFIX + self.request_id)
        if not value or value['status'] != 'running' or value['currentRunId'] != self.context.run_id:
            raise RepositoryError('cancelled', '联网接续已经结束。')
        return value

    def _emit(self, kind, payload, connection=None):
        if connection is None:
            with self.repo._operation(self.context, write=True) as (c, _):
                event = self._emit(kind, payload, c)
            self.queue.put_nowait(event)
            return event
        value = self._record(connection)
        event = {'schemaVersion': '0.1', 'sessionId': self.sid, 'requestId': self.request_id,
                 'runId': self.context.run_id, 'generation': self.context.generation,
                 'sequence': len(value['events']) + 1, 'type': kind,
                 'mode': 'mock' if self.host.client_factory else 'live', 'payload': payload}
        value['events'].append(event)
        _put(connection, self.sid, PREFIX + self.request_id, value)
        return event


    def _continue(self):
        with self.repo._operation(RequestContext(self.sid), write=True) as (c, s):
            value = self._record(c)
            if not s['web_enabled'] or s['web_settings_revision'] != value['webSettingsRevision']:
                raise RepositoryError('web_disabled', '联网权限已变化。')
            latest = c.execute("SELECT message_id FROM messages WHERE session_id=? AND role='user' ORDER BY rowid DESC LIMIT 1", (self.sid,)).fetchone()
            if (not latest or latest['message_id'] != self.user_id
                    or value.get('nextStateRevision') != s['state_revision']
                    or value['resumeGeneration'] != s['current_resume_generation']
                    or value['draftRevision'] != s['draft_revision']):
                raise RepositoryError('stale_state', '控制事件之后已有其他输入或修改。')
            rid, generation = self.host._new_run(c, s)
            value.update(currentRunId=rid, stateRevision=s['state_revision'])
            value.pop('nextStateRevision', None)
            _put(c, self.sid, PREFIX + self.request_id, value)
            self.context = RequestContext(self.sid, s['state_revision'], rid, generation)

    async def run(self):
        try:
            if self.host.client_factory:
                self.llm = self.host.client_factory(self, self.budget)
            else:
                from app.agent.llm import DeepSeekClient, LLMSettings
                self.llm = DeepSeekClient(LLMSettings.from_env(), budget=self.budget)
            self._emit('progress', {'message': '联网设置已确认，正在接续已有目标和资料。'})
            turn = self.service.get_chat_turn_context(self.context, self.user_id)
            # This is a setting event, never replay the old message's decision,
            # attachments, target selection, or personal answer.
            previous = turn['userMessage']['content'].strip()
            link_input = previous if re.fullmatch(r'(?:JD[：:]\s*)?https?://\S+', previous, re.I) else ''
            turn = {**turn, 'candidatePrompts': [], 'decisionReceipt': None,
                    'userMessage': {**turn['userMessage'], 'content': link_input}}
            if self.repo.get_draft(self.context)['blocks']:
                from app.agent_web import AgentWebBridge
                from app.web_intake_host import handle
                self.web_bridge = AgentWebBridge(self)
                result = await handle(self, turn)
                if result == 'ready':
                    from app.agent_progress import plan
                    if plan(self)['pending']:
                        self._reply('岗位资料已更新，先前待确认建议保留；它未必依据新资料，请核对后再决定。')
            self.finish('done', 'waiting_user' if self.waiting else 'completed')
        except asyncio.CancelledError:
            self.finish('interrupted', 'cancelled', check=False)
        except RepositoryError as error:
            self.finish('interrupted', error.code, check=False)
        except Exception:
            self.finish('failed', 'control_failed', check=False)
        finally:
            try:
                await self.budget.close_unknown()
            finally:
                self.host.active.pop((self.sid, self.request_id), None)
                self.queue.put_nowait(None)

    def finish(self, status, outcome, check=True):
        with database(self.repo.path) as c, transaction(c):
            value = _load(c, self.sid, PREFIX + self.request_id)
            if not value or value['status'] != 'running' or value['currentRunId'] != self.context.run_id:
                return
            if check:
                self.repo._check_context(c, self.context)
            event = self._emit('done', {'outcome': outcome}, c)
            value = _load(c, self.sid, PREFIX + self.request_id)
            value.update(status=status, outcome=outcome)
            _put(c, self.sid, PREFIX + self.request_id, value)
            pending = _load(c, self.sid, PENDING)
            if pending and pending.get('requestId') == self.request_id:
                pending.update(status=status, outcome=outcome)
                _put(c, self.sid, PENDING, pending)
            c.execute("UPDATE sessions SET status=? WHERE session_id=? AND status='running' AND EXISTS (SELECT 1 FROM runs WHERE run_id=? AND status='running')",
                      ('idle' if status == 'done' else 'interrupted', self.sid, self.context.run_id))
            c.execute("UPDATE runs SET status=?,error_code=? WHERE run_id=? AND status='running'",
                      ('completed' if status == 'done' else 'interrupted', None if status == 'done' else outcome, self.context.run_id))
        self.queue.put_nowait(event)
        return event
