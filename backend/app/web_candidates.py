"""Host-controlled candidate display/selection, separate from accepted evidence."""
import hashlib
import re

from app import web_intake
from app.repository import RepositoryError
from app.web_citations import goal_state, has_status_conflict
from app.web_provider import PublicSearchIntent, reference_identity
from app.web_target import _put


def _state(connection, session, episode_id, revision):
    value = web_intake._bound(connection, session, episode_id=episode_id,
                              expected_revision=revision)
    if value['intentHash'] != goal_state(connection, session)['baseHash']:
        raise RepositoryError('stale_state', '求职意向已经变化。')
    return value


def _reference(connection, session, state, reference_id):
    key = hashlib.sha256(state['episodeId'].encode()).hexdigest()
    row = connection.execute('''SELECT e.*,w.operation FROM external_references e
        JOIN web_requests w ON w.session_id=e.session_id AND w.request_id=e.request_id
        WHERE e.session_id=? AND e.reference_id=? AND w.target_key=?
        AND w.status='succeeded' AND w.web_settings_revision=? AND w.resume_generation=?''',
        (session['session_id'], reference_id, key, session['web_settings_revision'],
         session['current_resume_generation'])).fetchone()
    if not row or has_status_conflict(connection, session['session_id'], key, row):
        raise RepositoryError('scope_violation', '候选来源失效或不属于当前探索。', False)
    return row


def offer(connection, session, *, episode_id, expected_revision, candidates):
    """Validate candidate labels against stored public sources, never personal data.

    Caller orders candidates by explicit intent fit. No candidate is accepted here.
    A source's host alone never proves official ownership.
    """
    state = _state(connection, session, episode_id, expected_revision)
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 3:
        raise RepositoryError('invalid_input', '候选应为1至3项。')
    result, identities = [], set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {'referenceId', 'company', 'role', 'location', 'employmentType'}:
            raise RepositoryError('invalid_input', '候选字段无效。')
        row = _reference(connection, session, state, candidate['referenceId'])
        text = row['title'] + '\n' + row['content']
        fields = [candidate[name] for name in ('company', 'role', 'location', 'employmentType')]
        if any(not isinstance(value, str) or (value and value not in text) for value in fields):
            raise RepositoryError('invalid_input', '候选标签须来自同一来源原文。')
        if len(candidate['company'].strip()) < 2 or len(candidate['role'].strip()) < 2:
            raise RepositoryError('invalid_input', '候选公司和岗位尚未明确。')
        PublicSearchIntent(company=candidate['company'], role=candidate['role'],
                           location=candidate['location'], employment_type=candidate['employmentType'])
        identity = reference_identity(row['url'])
        if identity in identities:
            raise RepositoryError('invalid_input', '同一来源不可重复列为多个候选。')
        identities.add(identity)
        candidate_id = 'candidate-' + hashlib.sha256(
            (state['episodeId'] + '\0' + identity).encode()).hexdigest()[:24]
        hints = state.get('explicitFields', {})
        matches = [label for key, field, label in (
            ('company', 'company', '公司'), ('role', 'role', '岗位'), ('location', 'location', '地点'))
            if hints.get(key) and hints[key] in candidate[field]]
        result.append({**candidate, 'candidateId': candidate_id, 'url': row['url'],
                       'matchReason': '、'.join(matches) + '符合已明确条件' if matches else '需要你核对是否符合投递方向',
                       'sourceNotice': '公开来源，官网身份和当前招聘状态未独立核实；仅作为有限岗位参考。'})
    state.update(candidates=result, selectedCandidateId=None, confirmedThirdPartyReferenceIds=[],
                 phase='awaiting_selection', revision=state['revision'] + 1)
    state.pop('candidatePrompt', None)
    _put(connection, session['session_id'], web_intake.KEY, state)
    return state


def question(state):
    lines = ['找到以下候选，请回复候选编号，或补充条件继续找：']
    for index, candidate in enumerate(state['candidates'], 1):
        lines.append(f"{index}. {candidate['company']} · {candidate['role']} · "
                     f"{candidate['location'] or '地点未知'} · {candidate['employmentType'] or '招聘类型未知'}\n"
                     f"选择依据：{candidate['matchReason']}\n来源：{candidate['url']}\n{candidate['sourceNotice']}")
    lines.append('选择某项表示同意使用其公开来源作为有限参考；仍需核对正文，选择本身不会修改简历。')
    return '\n\n'.join(lines)


def record_prompt(connection, session, *, episode_id, expected_revision, message_id):
    state = _state(connection, session, episode_id, expected_revision)
    row = connection.execute('''SELECT m.rowid,m.content FROM messages m JOIN assistant_messages a
        ON a.session_id=m.session_id AND a.message_id=m.message_id
        WHERE m.session_id=? AND m.message_id=? AND a.kind='question'
        AND a.bound_state_revision=? AND a.resume_generation=?''',
        (session['session_id'], message_id, session['state_revision'],
         session['current_resume_generation'])).fetchone()
    if state['phase'] != 'awaiting_selection' or not row or row['content'] != question(state):
        raise RepositoryError('stale_state', '候选询问未按当前版本展示。')
    state.update(candidatePrompt={'messageId': message_id, 'rowid': row['rowid']},
                 revision=state['revision'] + 1)
    _put(connection, session['session_id'], web_intake.KEY, state)
    return state


def select(connection, session, *, episode_id, expected_revision, user_message_id):
    state = _state(connection, session, episode_id, expected_revision)
    prompt = state.get('candidatePrompt')
    row = connection.execute('''SELECT rowid,content,reply_to_message_id FROM messages WHERE session_id=?
        AND message_id=? AND role='user' AND input_channel='chat' AND use_status='active'
        AND resume_generation=?''', (session['session_id'], user_message_id,
                                     session['current_resume_generation'])).fetchone()
    if (state['phase'] != 'awaiting_selection' or not prompt or not row
            or row['rowid'] <= prompt['rowid']
            or row['reply_to_message_id'] not in (None, prompt['messageId'])):
        raise RepositoryError('stale_state', '选择必须回复当前候选询问。')
    text = row['content'].strip().rstrip('。！!')
    match = re.fullmatch(r'(?:我)?(?:就)?(?:选择|选)?(?:第)?\s*([123一二三])\s*(?:个|项|号)?(?:吧)?', text)
    chosen = None
    index = 0
    if match:
        index = int(match[1]) if match[1].isdigit() else {'一': 1, '二': 2, '三': 3}[match[1]]
    if index and index <= len(state['candidates']):
        chosen = state['candidates'][index - 1]
    elif len(state['candidates']) == 1 and text in {'是', '是的', '就这个', '确认这个', '确认'}:
        chosen = state['candidates'][0]
    else:
        exact = [c for c in state['candidates'] if text in {
            c['candidateId'], c['company'] + ' ' + c['role'], c['company'] + c['role']}]
        chosen = exact[0] if len(exact) == 1 else None
    if chosen is None:
        raise RepositoryError('ambiguous_selection', '请明确选择候选编号，或补充搜索条件。')
    _reference(connection, session, state, chosen['referenceId'])
    state.update(selectedCandidateId=chosen['candidateId'],
                 confirmedThirdPartyReferenceIds=[chosen['referenceId']],
                 phase='needs_material', revision=state['revision'] + 1,
                 selectionMessageId=user_message_id)
    _put(connection, session['session_id'], web_intake.KEY, state)
    return state
