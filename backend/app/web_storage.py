"""Session web permissions and immutable external references, never personal facts.

Only trusted server code calls the write methods. This module performs no HTTP
requests and never changes the resume, personal sources, or global cost ledger.
"""
import hashlib
import json
import time
from datetime import datetime

from app.repository import RepositoryError, RequestContext, _choice, _id, _integer, _keys, _now, _text


def _settings(session):
    return {'enabled': bool(session['web_enabled']),
            'webSettingsRevision': session['web_settings_revision']}


def get_settings(repo, context):
    with repo._operation(context) as (_, session):
        return _settings(session)


def update_settings(repo, context, *, enabled, expected_revision):
    repo._user_path(context)
    if type(enabled) is not bool:
        raise RepositoryError('invalid_input', '联网开关必须是布尔值。')
    _integer(expected_revision, '联网设置版本')
    with repo._operation(context, write=True) as (connection, session):
        if session['web_settings_revision'] != expected_revision:
            raise RepositoryError('stale_state', '联网设置已更新，请读取最新状态。')
        if enabled and session['mode'] != 'local':
            raise RepositoryError('scope_violation', '模拟会话不能开启真实联网。', False)
        if bool(session['web_enabled']) != enabled:
            if expected_revision == 2**63 - 1:
                raise RepositoryError('invalid_input', '联网设置版本已达到上限。')
            connection.execute('UPDATE sessions SET web_enabled=?,web_settings_revision=web_settings_revision+1 WHERE session_id=?',
                               (int(enabled), context.session_id))
            # Includes pending results of any previous permission epoch. This
            # does not cancel model work or mutate the original proposal context.
            connection.execute("UPDATE web_requests SET status='cancelled',error_code='web_settings_changed',completed_at=? WHERE session_id=? AND status='pending'",
                               (_now(), context.session_id))
        session = connection.execute('SELECT * FROM sessions WHERE session_id=?', (context.session_id,)).fetchone()
        return _settings(session)


def _require_run(context):
    if not isinstance(context, RequestContext) or context.run_id is None:
        raise RepositoryError('scope_violation', '外部搜索必须绑定有效运行。', False)


def _require_permission(session, revision):
    _integer(revision, '联网设置版本')
    if not session['web_enabled'] or session['web_settings_revision'] != revision:
        raise RepositoryError('web_disabled', '联网已关闭或设置已变化，本次搜索结果不可使用。')


def begin_request(repo, context, *, request_id, web_settings_revision, target_key, operation, payload,
                  budget_reservation_id=None):
    _require_run(context)
    _text(request_id, '搜索请求编号')
    _text(target_key, '搜索目标标识')
    _choice(operation, {'search', 'fetch'}, '搜索操作')
    if not isinstance(payload, dict):
        raise RepositoryError('invalid_input', '搜索输入必须为对象。')
    try:
        input_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise RepositoryError('invalid_input', '搜索输入不是有效JSON。') from error
    if len(input_json) > 4000 or len(target_key) > 256 or len(request_id) > 128:
        raise RepositoryError('invalid_input', '搜索请求超过长度限制。')
    with repo._operation(context, write=True) as (connection, session):
        _require_permission(session, web_settings_revision)
        if session['mode'] != 'local':
            raise RepositoryError('scope_violation', '模拟会话不能发起真实搜索。', False)
        root = connection.execute('SELECT root_run_id FROM agent_run_requests WHERE session_id=? AND current_run_id=?',
                                  (context.session_id, context.run_id)).fetchone()
        scope_run_id = root[0] if root else context.run_id
        fingerprint = hashlib.sha256(json.dumps([context.run_id, session['current_resume_generation'],
            web_settings_revision, target_key, operation, input_json], ensure_ascii=False).encode('utf-8')).hexdigest()
        old = connection.execute('SELECT * FROM web_requests WHERE session_id=? AND request_id=?',
                                 (context.session_id, request_id)).fetchone()
        if old:
            if old['request_hash'] != fingerprint:
                raise RepositoryError('request_conflict', '搜索请求编号已用于其他输入。')
            return {'requestId': request_id, 'status': old['status'], 'created': False,
                    'scopeRunId': old['scope_run_id'] or old['run_id'], 'deadlineAt': old['deadline_at']}
        count = connection.execute('SELECT count(*) FROM web_requests WHERE session_id=? AND coalesce(scope_run_id,run_id)=? AND operation=?',
                                   (context.session_id, scope_run_id, operation)).fetchone()[0]
        if count >= (2 if operation == 'search' else 3):
            raise RepositoryError('web_limit', '本轮搜索或读取次数已达上限。')
        first = connection.execute('SELECT * FROM web_requests WHERE session_id=? AND coalesce(scope_run_id,run_id)=? ORDER BY created_at,rowid LIMIT 1',
                                   (context.session_id, scope_run_id)).fetchone()
        deadline = time.time() + 45 if first is None else first['deadline_at']
        if deadline is None:
            deadline = datetime.fromisoformat(first['created_at'].replace('Z', '+00:00')).timestamp() + 45
        if time.time() >= deadline:
            raise RepositoryError('web_timeout', '本轮搜索已超过总时限。')
        if first and (first['target_key'] != target_key or first['web_settings_revision'] != web_settings_revision):
            raise RepositoryError('stale_state', '本轮搜索目标或联网权限已变化。')
        connection.execute('''INSERT INTO web_requests
            (session_id,request_id,run_id,resume_generation,web_settings_revision,target_key,operation,provider,request_hash,input_json,created_at,scope_run_id,budget_reservation_id,deadline_at)
            VALUES (?,?,?,?,?,?,?,'volcengine',?,?,?,?,?,?)''',
            (context.session_id, request_id, context.run_id, session['current_resume_generation'],
             web_settings_revision, target_key, operation, fingerprint, input_json, _now(), scope_run_id, budget_reservation_id, deadline))
        return {'requestId': request_id, 'status': 'pending', 'created': True,
                'scopeRunId': scope_run_id, 'deadlineAt': deadline}


def _reference(row):
    return {'referenceId': row['reference_id'], 'title': row['title'], 'url': row['url'],
            'content': row['content'], 'retrievedAt': row['retrieved_at'], 'publishedAt': row['published_at'],
            'matchStatus': row['match_status'], 'hiringStatus': row['hiring_status'],
            'contentStatus': row['content_status']}


def _references(connection, session_id, request_id):
    return [_reference(row) for row in connection.execute(
        'SELECT * FROM external_references WHERE session_id=? AND request_id=? ORDER BY rowid',
        (session_id, request_id))]


def finish_request(repo, context, *, request_id, web_settings_revision, target_key,
                   references, status='succeeded', error_code=None, usage=None):
    _require_run(context)
    _choice(status, {'succeeded', 'failed', 'unknown'}, '搜索完成状态')
    if not isinstance(references, list) or len(references) > 5 or (status != 'succeeded' and references):
        raise RepositoryError('invalid_input', '搜索结果列表无效。')
    with repo._operation(context, write=True) as (connection, session):
        _require_permission(session, web_settings_revision)
        row = connection.execute('SELECT * FROM web_requests WHERE session_id=? AND request_id=?',
                                 (context.session_id, request_id)).fetchone()
        if row is None or row['run_id'] != context.run_id or row['target_key'] != target_key:
            raise RepositoryError('scope_violation', '搜索结果不属于当前运行和目标。', False)
        if row['resume_generation'] != session['current_resume_generation'] or row['web_settings_revision'] != web_settings_revision:
            raise RepositoryError('stale_state', '搜索依据已经变化。')
        if row['status'] == 'cancelled':
            raise RepositoryError('cancelled', '搜索请求已失效。')
        if row['status'] != 'pending':
            return {'requestId': request_id, 'status': row['status'],
                    'references': _references(connection, context.session_id, request_id), 'replayed': True}
        if row['deadline_at'] is not None and time.time() >= row['deadline_at']:
            raise RepositoryError('web_timeout', '本轮搜索结果已超过总时限。')
        for item in references:
            _keys(item, {'title', 'url', 'content', 'matchStatus', 'hiringStatus', 'contentStatus'}, {'publishedAt'})
            for name in ('title', 'url'):
                _text(item[name], name)
            _text(item['content'], '外部内容', empty=True)
            if len(item['content']) > 50000 or len(item['title']) > 500 or len(item['url']) > 4096:
                raise RepositoryError('invalid_input', '外部参考超过长度限制。')
            if not item['url'].startswith(('https://', 'http://')):
                raise RepositoryError('invalid_input', '外部参考必须有网页链接。')
            _choice(item['matchStatus'], {'matched', 'unconfirmed', 'mismatch'}, '目标匹配状态')
            _choice(item['hiringStatus'], {'unconfirmed', 'conflict', 'closed', 'open'}, '招聘状态')
            _choice(item['contentStatus'], {'sufficient', 'partial', 'unavailable'}, '正文完整度')
            if item.get('publishedAt') is not None:
                _text(item['publishedAt'], '发布时间')
            connection.execute('''INSERT INTO external_references
                (reference_id,session_id,request_id,title,url,content,retrieved_at,published_at,match_status,hiring_status,content_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (_id('external'), context.session_id, request_id, item['title'], item['url'], item['content'],
                 _now(), item.get('publishedAt'), item['matchStatus'], item['hiringStatus'], item['contentStatus']))
        connection.execute('UPDATE web_requests SET status=?,error_code=?,usage_json=?,completed_at=? WHERE session_id=? AND request_id=?',
                           (status, error_code, json.dumps(usage, allow_nan=False) if usage is not None else None,
                            _now(), context.session_id, request_id))
        return {'requestId': request_id, 'status': status,
                'references': _references(connection, context.session_id, request_id), 'replayed': False}


def list_references(repo, context):
    """User history only. A future Agent read must additionally bind its target/run."""
    repo._user_path(context)
    with repo._operation(context) as (connection, _):
        return [_reference(row) for row in connection.execute(
            'SELECT * FROM external_references WHERE session_id=? ORDER BY retrieved_at,rowid', (context.session_id,))]


def request_result(repo, context, request_id, revision, target_key):
    _require_run(context)
    with repo._operation(context) as (connection, session):
        _require_permission(session, revision)
        row = connection.execute('SELECT * FROM web_requests WHERE session_id=? AND request_id=?', (context.session_id, request_id)).fetchone()
        if row is None or row['run_id'] != context.run_id or row['target_key'] != target_key or row['web_settings_revision'] != revision:
            raise RepositoryError('scope_violation', '搜索请求不属于当前运行和目标。', False)
        return {'requestId': request_id, 'status': row['status'], 'references': _references(connection, context.session_id, request_id)}


def close_request(repo, context, request_id, *, status, error_code, usage=None):
    """Cleanup can record cost/failure after cancellation, but never write content."""
    _require_run(context)
    _choice(status, {'failed', 'unknown', 'cancelled'}, '搜索结束状态')
    with repo._connection_scope(write=True) as connection:
        row = connection.execute('SELECT * FROM web_requests WHERE session_id=? AND request_id=? AND run_id=?',
                                 (context.session_id, request_id, context.run_id)).fetchone()
        if row is None:
            return  # A deleted session must not be resurrected by cleanup.
        if row['status'] == 'pending':
            connection.execute('UPDATE web_requests SET status=?,error_code=?,completed_at=? WHERE session_id=? AND request_id=?',
                               (status, error_code, _now(), context.session_id, request_id))
        if usage is not None:
            connection.execute('UPDATE web_requests SET usage_json=? WHERE session_id=? AND request_id=?',
                               (json.dumps(usage, allow_nan=False), context.session_id, request_id))
