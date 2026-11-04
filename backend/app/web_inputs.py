"""Bind JD URLs to immutable user inputs, never to model-authored URLs."""
import hashlib
import re

from app.repository import RepositoryError, _ACTIVE_SOURCE
from app.web_provider import public_url_shape
from app.web_target import _load, _put


def _input(connection, session, kind, source_id, revision):
    if kind == 'message':
        row = connection.execute("""SELECT content FROM messages WHERE session_id=?
            AND message_id=? AND role='user' AND use_status='active'
            AND resume_generation=? AND input_channel='chat'""",
            (session['session_id'], source_id, session['current_resume_generation'])).fetchone()
        text = row['content'] if row else None
    elif kind == 'jd' and type(revision) is int:
        row = connection.execute(f"""SELECT b.text FROM source_blocks b
            JOIN sessions s ON s.session_id=b.session_id WHERE b.session_id=?
            AND b.source_id=? AND b.source_revision=? AND b.role='jd' AND {_ACTIVE_SOURCE}""",
            (session['session_id'], source_id, revision)).fetchone()
        text = row['text'] if row else None
    else:
        text = None
    if text is None:
        raise RepositoryError('stale_state', 'JD链接对应的用户输入已失效。')
    return text


def bind_links(connection, session, *, kind, source_id, source_revision=None):
    text = _input(connection, session, kind, source_id, source_revision)
    bindings = _load(connection, session['session_id'], 'web_user_url_bindings') or {}
    results = []
    for match in re.finditer(r'https?://[^\s<>"\u3000]+', text):
        url = public_url_shape(match.group().rstrip('。，；、）)」】'))
        digest = hashlib.sha256(text.encode()).hexdigest()
        token = hashlib.sha256(f'{kind}\0{source_id}\0{source_revision}\0{digest}\0{url}'.encode()).hexdigest()
        value = {'kind': kind, 'sourceId': source_id, 'sourceRevision': source_revision,
                 'inputHash': digest, 'url': url}
        bindings[token] = value
        results.append({'bindingId': token, 'url': url})
    _put(connection, session['session_id'], 'web_user_url_bindings', bindings)
    return results


def resolve_link(connection, session, binding_id):
    bindings = _load(connection, session['session_id'], 'web_user_url_bindings') or {}
    value = bindings.get(binding_id) if isinstance(binding_id, str) else None
    if not value:
        raise RepositoryError('scope_violation', 'JD链接未绑定当前会话的用户输入。', False)
    text = _input(connection, session, value['kind'], value['sourceId'], value['sourceRevision'])
    if hashlib.sha256(text.encode()).hexdigest() != value['inputHash']:
        raise RepositoryError('stale_state', 'JD链接对应的输入版本已变化。')
    return public_url_shape(value['url'])
