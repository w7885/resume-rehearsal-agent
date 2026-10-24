"""Resolve an available target without another model call or invented user facts."""
import re

from app.repository import Repository, RequestContext, _ACTIVE_SOURCE


def current_jd(repo, context):
    with repo._operation(context) as (connection, _):
        rows = connection.execute(f"SELECT b.* FROM source_blocks b JOIN sessions s ON s.session_id=b.session_id WHERE b.session_id=? AND b.role='jd' AND {_ACTIVE_SOURCE} ORDER BY coalesce(b.page_number,1), coalesce(json_extract(b.origin_json,'$.paragraph'),b.line_start,1), b.rowid", (context.session_id,)).fetchall()
        documents = {(row['attachment_id'], row['message_id']) for row in rows}
        if len(documents) != 1:
            return None
        return '\n\n'.join(row['text'] for row in rows)


def title_from_jd(text):
    lines = [re.sub(r'^\s*#{1,6}\s*', '', line).strip() for line in text.splitlines() if line.strip()]
    for line in lines[:6]:
        match = re.fullmatch(r'(?:岗位名称|职位名称|招聘岗位|招聘职位)\s*[:：]\s*(.{2,80})', line)
        if match:
            return match[1]
    if lines:
        match = re.fullmatch(r'(.{2,80}?)(?:\s*[-—:：]?\s*(?:JD|岗位描述|职位描述))', lines[0], re.I)
        if match and match[1].strip() not in {'招聘', '工作', '岗位', '职位'}:
            return match[1].strip()
    return '按已提供的岗位描述优化'


def refers_to_jd(goal):
    return bool(re.search(r'(?:按|依|参考|以).{0,16}(?:jd|岗位描述|职位描述)', goal, re.I))


def sync(runtime):
    """Only fill a pending target, outside the acceptance preflight window."""
    session = runtime.repo.get_session(runtime.context)
    if session['directionStatus'] == 'provided':
        return None
    from app.agent.memory import load_session
    from app.agent_state import GuardedStateStore
    existing = load_session(GuardedStateStore(runtime), runtime.sid)
    old_goal = existing[0].goal if existing else ''
    text = current_jd(runtime.repo, runtime.context)
    if old_goal and not refers_to_jd(old_goal):
        goal = old_goal[:120]
    elif text:
        goal = title_from_jd(text)
    else:
        return None
    with runtime.repo._operation(runtime.context, write=True) as (connection, _):
        bound = Repository(runtime.repo.path, _connection=connection)
        connection.execute("UPDATE sessions SET direction=?,direction_status='provided' WHERE session_id=?", (goal, runtime.sid))
        bound._touch(connection, runtime.sid)
        current = bound.get_session(RequestContext(runtime.sid))
        connection.execute('INSERT OR REPLACE INTO agent_turn_effects VALUES (?,?,?,?,?)', (runtime.sid, runtime.user_id, current['stateRevision'], current['draftRevision'], current['currentResumeGeneration']))
    runtime._continue()
    return goal
