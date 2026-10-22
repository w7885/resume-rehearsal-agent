"""Trusted Host adapter from B's parsed choices to existing A-T07 transactions.

This module checks provenance and versions, never interprets natural language.
It is intentionally absent from HTTP routes and the model tool whitelist.
"""
import hashlib
import json

from app.assistant_service import host_only, prompt_snapshot, user_turn, valid_suggestion
from app.repository import RepositoryError, RequestContext, _choice, _keys, _text


def validate_payload(payload):
    _keys(payload, {'requestId', 'userMessageId', 'userContent', 'status', 'decisions'})
    for field in ('requestId', 'userMessageId'):
        if len(_text(payload[field], field)) > 128:
            raise RepositoryError('invalid_input', '消息标识过长。')
    content = _text(payload['userContent'], '用户原话', empty=True)
    if len(content) > 50000:
        raise RepositoryError('invalid_input', '用户原话超过长度限制。')
    status = _choice(payload['status'], {'none', 'clarify', 'resolved'}, '意图状态')
    decisions = payload['decisions']
    if not isinstance(decisions, list) or len(decisions) > 10 or (status == 'resolved') != bool(decisions):
        raise RepositoryError('invalid_input', '仅明确决定可包含 1～10 项选择。')
    seen = set()
    for item in decisions:
        _keys(item, {'promptMessageId', 'suggestionId', 'decision', 'feedback'})
        for field in ('promptMessageId', 'suggestionId'):
            if len(_text(item[field], field)) > 128:
                raise RepositoryError('invalid_input', '目标标识过长。')
        if item['suggestionId'] in seen:
            raise RepositoryError('invalid_input', '同一次答复不能对同一建议重复决定。')
        seen.add(item['suggestionId'])
        action = _choice(item['decision'], {'accept', 'reject', 'revise'}, '建议决定')
        feedback = _text(item['feedback'], '原话反馈', empty=action != 'revise')
        if feedback and feedback not in content:
            raise RepositoryError('invalid_input', '反馈必须直接取自这条用户原话，不能由助手补写。')


def apply_resolution(service, context, payload, *, synthetic=False):
    host_only(context)
    validate_payload(payload)
    if type(synthetic) is not bool:
        raise RepositoryError('invalid_input', '构造模式必须由 Host 明确指定。')
    mode = 'synthetic' if synthetic else 'host'
    identity = {key: value for key, value in payload.items() if key != 'requestId'}
    identity.update(runId=context.run_id, generation=context.generation,
                    stateRevision=context.expected_state_revision, mode=mode)
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def apply(repo):
        # Durable message consumption is checked before optimistic versions:
        # an acknowledged decision advanced them and interrupted its own run.
        # An exact old receipt may be returned, but it never authorizes a write.
        with repo._operation(RequestContext(context.session_id), write=True) as (connection, session):
            old = connection.execute('SELECT * FROM chat_decision_resolutions WHERE session_id = ? AND user_message_id = ?',
                                     (context.session_id, payload['userMessageId'])).fetchone()
            if old is not None:
                if old['request_hash'] != digest:
                    raise RepositoryError('request_conflict', '这条用户答复已经处理，不能更换目标或决定再次消费。')
                return json.loads(old['receipt_json'])
            session = repo._check_context(connection, context)
            if session['mode'] != 'local':
                raise RepositoryError('scope_violation', '模拟历史不能作为个人修改授权。', False)
            user = user_turn(connection, context, session, payload['userMessageId'])
            if user['content'] != payload['userContent']:
                raise RepositoryError('scope_violation', '决定依据必须是数据库中这条用户消息的完整原话。', False)
            candidates = json.loads(user['reply_candidates_json'] or '[]')
            selected = []
            accepted_blocks = set()
            for item in payload['decisions']:
                prompt_id = item['promptMessageId']
                if user['reply_to_message_id'] and user['reply_to_message_id'] != prompt_id:
                    raise RepositoryError('scope_violation', '目标询问不在这次答复的关联范围内。', False)
                prompt = prompt_snapshot(repo, connection, session, prompt_id)
                if prompt_id not in candidates:
                    if prompt['state'] in {'outdated', 'history'}:
                        raise RepositoryError('stale_state', '这条询问已过期，请以当前稿重新核对。')
                    raise RepositoryError('scope_violation', '目标询问不在这次答复的关联范围内。', False)
                if prompt['mode'] != mode:
                    raise RepositoryError('scope_violation', '构造询问不能冒充真实用户授权。', False)
                if (prompt['state'] != 'reply_saved'
                        or user['saved_before_revision'] != prompt['boundStateRevision']
                        or user['saved_after_revision'] != prompt['boundStateRevision'] + 1
                        or session['draft_revision'] != prompt['boundDraftRevision']):
                    raise RepositoryError('stale_state', '询问后还有其他输入或修改，不能直接执行旧决定。')
                if item['suggestionId'] not in {s['suggestionId'] for s in prompt['suggestions']}:
                    raise RepositoryError('scope_violation', '建议不属于用户所回复的询问。', False)
                proposal = valid_suggestion(repo, connection, session, item['suggestionId'])
                block_id = proposal['target']['blockId']
                if item['decision'] == 'accept':
                    if block_id in accepted_blocks:
                        raise RepositoryError('invalid_input', '同一段落的多个改写不能同时采用，请先澄清选择。')
                    accepted_blocks.add(block_id)
                selected.append((item, proposal))

            # All targets were preflighted before writing. Record declined or
            # revised alternatives before accepting a competing block proposal.
            for item, proposal in sorted(selected, key=lambda pair: pair[0]['decision'] == 'accept'):
                current = repo.get_session(RequestContext(context.session_id))
                # Only this validated, atomic Host path may call the user-only
                # transaction. Never hand this context to a model tool.
                repo.apply_suggestion_decision(
                    RequestContext(context.session_id, current['stateRevision']), item['suggestionId'],
                    {'decision': item['decision'], 'feedback': item['feedback'],
                     'expectedBlockRevision': proposal['target']['expectedBlockRevision']})
            current = repo.get_session(RequestContext(context.session_id))
            receipt = {'userMessageId': payload['userMessageId'], 'status': payload['status'],
                       'mode': mode, 'decisions': payload['decisions'],
                       'stateRevision': current['stateRevision'], 'draftRevision': current['draftRevision']}
            connection.execute('INSERT INTO chat_decision_resolutions VALUES (?,?,?,?,?,?,?,?,?)',
                               (context.session_id, user['message_id'], context.run_id,
                                context.expected_state_revision, session['current_resume_generation'], mode,
                                payload['status'], digest, json.dumps(receipt, ensure_ascii=False)))
            return receipt

    return service._mutate(f'session:{context.session_id}', 'chat_decision_resolution',
                           {**identity, 'requestId': payload['requestId']}, payload['requestId'], apply, context.session_id)
