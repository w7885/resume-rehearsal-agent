"""External assessment/citation scope checks; no model or HTTP calls."""
import hashlib
import json
import re
import unicodedata

from app.repository import RepositoryError, _ACTIVE_SOURCE, _now
from app.web_provider import reference_identity
from app.web_storage import _reference


def goal_state(connection, session):
    rows = connection.execute(f'''SELECT b.* FROM source_blocks b
        JOIN sessions s ON s.session_id=b.session_id WHERE b.session_id=? AND b.role='jd' AND {_ACTIVE_SOURCE}
        ORDER BY b.source_id''', (session['session_id'],)).fetchall()
    direction = session['direction'] if session['direction_status'] == 'provided' else ''
    versions = [(r['source_id'], r['source_revision'], hashlib.sha256(r['text'].encode()).hexdigest()) for r in rows]
    # User intent outlives resume replacements; reference eligibility does not.
    values = [session['session_id'], direction, versions]
    base_hash = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
    from app.web_target import saved_target, is_deferred
    clarified = saved_target(connection,session['session_id'],base_hash)
    deferred = is_deferred(connection,session['session_id'],base_hash)
    digest = hashlib.sha256(json.dumps([base_hash,session['current_resume_generation'],clarified,deferred],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'hash': digest, 'direction': direction, 'jd': '\n\n'.join(r['text'] for r in rows),
            'baseHash':base_hash, 'clarifiedTarget':clarified,
            'deferred':deferred,
            'jdDocuments': len({(r['attachment_id'],r['message_id']) for r in rows}),
            'targetId': 'target-' + digest}


def has_status_conflict(connection, session_id, target_key, row):
    if row['hiring_status'] in ('closed', 'conflict'):
        return True
    previous = connection.execute('''SELECT e.url,e.hiring_status FROM external_references e
        JOIN web_requests w ON w.session_id=e.session_id AND w.request_id=e.request_id
        LEFT JOIN external_assessments a ON a.session_id=e.session_id AND a.reference_id=e.reference_id
        WHERE e.session_id=? AND (e.hiring_status IN ('closed','conflict') OR a.verdict IN ('closed','conflict'))''',
        (session_id,)).fetchall()
    identity = reference_identity(row['url'])
    return any(reference_identity(r['url']) == identity for r in previous)


def assess(repo, context, reference_id, *, goal_hash, target_key, purpose, verdict, evidence):
    with repo._operation(context, write=True) as (connection, session):
        if not session['web_enabled'] or goal_state(connection, session)['hash'] != goal_hash:
            raise RepositoryError('stale_state', '联网目标或权限已经变化。')
        row = connection.execute('''SELECT e.*,w.target_key,w.web_settings_revision,w.resume_generation FROM external_references e
            JOIN web_requests w ON w.session_id=e.session_id AND w.request_id=e.request_id
            WHERE e.session_id=? AND e.reference_id=?''', (context.session_id, reference_id)).fetchone()
        if row is None or row['target_key'] != target_key or row['web_settings_revision'] != session['web_settings_revision'] or row['resume_generation'] != session['current_resume_generation']:
            raise RepositoryError('scope_violation', '外部参考不属于当前目标或权限版本。', False)
        if verdict == 'accepted' and has_status_conflict(connection, context.session_id, target_key, row):
            verdict = 'conflict'
        old = connection.execute('SELECT * FROM external_assessments WHERE reference_id=?', (reference_id,)).fetchone()
        if old:
            if old['goal_hash'] != goal_hash or old['target_key'] != target_key or old['purpose'] != purpose:
                raise RepositoryError('scope_violation', '外部核对记录不属于当前目标。', False)
            return old['verdict']
        connection.execute('''INSERT INTO external_assessments
            (reference_id,session_id,goal_hash,target_key,web_settings_revision,resume_generation,purpose,verdict,evidence_json,assessed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (reference_id, context.session_id, goal_hash, target_key, session['web_settings_revision'],
             session['current_resume_generation'], purpose, verdict, json.dumps(evidence, ensure_ascii=False), _now()))
        return verdict


def _current_reference(connection, session, reference_id, *, require_permission=True):
    row = connection.execute('''SELECT e.*,a.goal_hash,a.target_key,a.web_settings_revision,a.resume_generation,a.verdict,a.purpose
        FROM external_references e JOIN external_assessments a ON a.reference_id=e.reference_id AND a.session_id=e.session_id
        WHERE e.session_id=? AND e.reference_id=?''', (session['session_id'],reference_id)).fetchone()
    if row is None or row['verdict'] != 'accepted':
        raise RepositoryError('scope_violation', '引用的外部参考不存在或尚未通过核对。', False)
    if row['goal_hash'] != goal_state(connection, session)['hash'] or row['resume_generation'] != session['current_resume_generation']:
        raise RepositoryError('stale_state', '外部参考对应的目标或材料已变化。')
    if require_permission and (not session['web_enabled'] or row['web_settings_revision'] != session['web_settings_revision']):
        raise RepositoryError('web_disabled', '联网已关闭或权限版本已变化。')
    if has_status_conflict(connection, session['session_id'], row['target_key'], row):
        raise RepositoryError('stale_state', '外部参考存在下线或状态冲突，请补充JD。')
    return row


def validate_ids(connection, session, ids, *, require_permission=True):
    if not isinstance(ids, list) or len(ids) > 5 or any(not isinstance(x,str) or not x or len(x)>128 for x in ids) or len(set(ids)) != len(ids):
        raise RepositoryError('invalid_input', '外部引用须为最多5个互不重复的参考编号。')
    return [_current_reference(connection,session,ref,require_permission=require_permission) for ref in ids]


def cached_references(repo, context, purpose):
    with repo._operation(context) as (connection, session):
        if not session['web_enabled']:
            return []
        goal = goal_state(connection,session)
        ids = [r[0] for r in connection.execute('''SELECT reference_id FROM external_assessments
            WHERE session_id=? AND goal_hash=? AND web_settings_revision=? AND resume_generation=? AND purpose=? AND verdict='accepted'
            ORDER BY assessed_at DESC,rowid DESC LIMIT 5''',
            (context.session_id, goal['hash'], session['web_settings_revision'], session['current_resume_generation'],purpose))]
        refs = []
        for reference_id in ids:
            try:
                row = _current_reference(connection,session,reference_id)
            except RepositoryError:
                continue
            refs.append({**_reference(row),'matchStatus':'matched',
                         'hiringNotice':'招聘状态未核实，不代表当前在招。','purpose':row['purpose']})
        return refs


def _requirement_sentence(text):
    for sentence in re.split(r'[。；;\n]', text or ''):
        value = sentence.strip()
        if len(value) >= 6:
            return value[:60]
    return ''


def requirement_quotes(repo, context):
    with repo._operation(context) as (connection, session):
        if not session['web_enabled']:
            return []
        goal = goal_state(connection, session)
        # 只取 accepted：未通过核对的网页内容不能当作岗位要求。
        rows = connection.execute('''SELECT reference_id,evidence_json FROM external_assessments
            WHERE session_id=? AND goal_hash=? AND web_settings_revision=? AND resume_generation=?
            AND purpose='job_requirements' AND verdict='accepted'
            ORDER BY assessed_at DESC,rowid DESC''',
            (context.session_id, goal['hash'], session['web_settings_revision'],
             session['current_resume_generation'])).fetchall()
        result, seen = [], set()
        for row in rows:
            try:
                _current_reference(connection, session, row['reference_id'])
            except RepositoryError:
                continue
            try:
                evidence = json.loads(row['evidence_json'] or '{}')
            except (TypeError, ValueError):
                continue
            # 损坏或旧版行可能存成 list/str/int，不能当对象取字段。
            if not isinstance(evidence, dict):
                continue
            for field in ('requirements_quote', 'duties_quote'):
                # 逐字截取证据原文，不改写：下游只能原样引用，改写会变成伪造岗位要求。
                quote = _requirement_sentence(evidence.get(field) or '')
                if not quote or quote in seen:
                    continue
                seen.add(quote)
                result.append({'requirementId': f'req-{len(result) + 1}',
                               'referenceId': row['reference_id'], 'quote': quote})
                if len(result) >= 3:
                    return result
        return result


def link_suggestion(connection, session, suggestion_id, ids):
    validate_ids(connection,session,ids)
    for reference_id in ids:
        connection.execute('INSERT INTO suggestion_external_references VALUES (?,?,?)',
                           (session['session_id'],suggestion_id,reference_id))


def link_assistant(connection, session, message_id, ids):
    validate_ids(connection,session,ids)
    for reference_id in ids:
        connection.execute('INSERT INTO assistant_external_references VALUES (?,?,?)',
                           (session['session_id'],message_id,reference_id))


def citation_view(connection, session_id, *, suggestion_id=None, message_id=None):
    if suggestion_id is not None:
        rows = connection.execute('''SELECT e.* FROM external_references e JOIN suggestion_external_references l
            ON l.session_id=e.session_id AND l.reference_id=e.reference_id
            WHERE l.session_id=? AND l.suggestion_id=? ORDER BY e.rowid''', (session_id,suggestion_id))
    else:
        rows = connection.execute('''SELECT e.* FROM external_references e JOIN assistant_external_references l
            ON l.session_id=e.session_id AND l.reference_id=e.reference_id
            WHERE l.session_id=? AND l.message_id=? ORDER BY e.rowid''', (session_id,message_id))
    return [{**_reference(row), 'matchStatus':'matched', 'hiringNotice':'招聘状态未核实，不代表当前在招。'} for row in rows]


def validate_adoption(connection, session, suggestion_id):
    ids = [r[0] for r in connection.execute('SELECT reference_id FROM suggestion_external_references WHERE session_id=? AND suggestion_id=?',
                                           (session['session_id'],suggestion_id))]
    # Published suggestions survive turning web off, but not target/JD changes or
    # newly discovered contradictory sources. Personal before/refs still recheck.
    validate_ids(connection,session,ids,require_permission=False)


def validate_company_claim(connection, session, after, before, personal_quotes):
    """Known search targets cannot silently become novel personal employers/clients.

    This is a lexical provenance guard, not general company/entity truth checking.
    User hand edits remain user actions; only generated suggestions pass here.
    """
    normalize=lambda s:re.sub(r'\s+','',unicodedata.normalize('NFKC',s)).casefold()
    evidence=normalize(before+'\n'+'\n'.join(personal_quotes))
    proposed=normalize(after)
    rows=connection.execute('SELECT evidence_json FROM external_assessments WHERE session_id=? AND resume_generation=?',
                            (session['session_id'],session['current_resume_generation']))
    for row in rows:
        company=(json.loads(row[0]).get('public_target') or {}).get('company','')
        if company and normalize(company) in proposed and normalize(company) not in evidence:
            raise RepositoryError('unsupported_external_company','目标公司的名称不能凭岗位资料新增为个人任职或客户经历，须有个人原句依据。')
