"""User-owned session display metadata. Never invokes Agent or a provider."""
import re

from app.repository import RepositoryError, RequestContext, _text


def normalize_title(value):
    _text(value, '会话名称')
    title = value.strip()
    if not 1 <= len(title) <= 50 or re.search(r'[\x00-\x1f\x7f-\x9f]', value):
        raise RepositoryError('invalid_input', '会话名称须为1～50字，且不能包含换行或控制字符。')
    return title


def rename_session(service, session_id, payload):
    title = normalize_title(payload['title'])
    normalized = {**payload, 'title': title}

    def apply(repo):
        context = RequestContext(session_id, payload['expectedStateRevision'])
        repo._user_path(context)
        with repo._operation(context, write=True) as (connection, session):
            active = connection.execute(
                "SELECT 1 FROM agent_run_requests WHERE session_id=? AND status='running' LIMIT 1",
                (session_id,)).fetchone()
            active_run = connection.execute("SELECT 1 FROM runs WHERE session_id=? AND status='running' LIMIT 1", (session_id,)).fetchone()
            if session['status'] == 'running' or active or active_run:
                raise RepositoryError('run_busy', '会话正在处理中，请先完成或停止任务再重命名。')
            if session['chat_title'] != title:
                connection.execute('UPDATE sessions SET chat_title=? WHERE session_id=?', (title, session_id))
                repo._touch(connection, session_id)
            return repo.get_session(RequestContext(session_id))

    return service._mutate(f'session:{session_id}', 'rename_session', normalized,
                           payload['requestId'], apply, session_id)
