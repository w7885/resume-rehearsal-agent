"""Host-owned target clarification state, separate from personal fact memory."""
import json

from app.repository import RepositoryError
from app.web_provider import PublicTarget


def _load(connection, session_id, key):
    row=connection.execute("SELECT doc_json FROM agent_state WHERE session_id=? AND scope='session' AND key=?",(session_id,key)).fetchone()
    return json.loads(row[0]) if row else None


def _put(connection, session_id, key, value):
    connection.execute('''INSERT INTO agent_state(session_id,scope,key,revision,doc_json) VALUES (?,'session',?,1,?)
        ON CONFLICT(session_id,scope,key) DO UPDATE SET revision=revision+1,doc_json=excluded.doc_json''',
        (session_id,key,json.dumps(value,ensure_ascii=False)))


def saved_target(connection, session_id, base_hash):
    stored=_load(connection,session_id,'web_public_target')
    if not stored or stored.get('base_hash')!=base_hash:
        return None
    fields=stored.get('fields')
    try:
        PublicTarget(**fields)
    except (TypeError,ValueError):
        return None
    return fields


def record_prompt(connection, session, message_id, binding):
    from app.web_citations import goal_state
    current=goal_state(connection,session)
    owner=connection.execute("SELECT 1 FROM assistant_messages WHERE session_id=? AND message_id=? AND kind='question' AND bound_state_revision=?",
                             (session['session_id'],message_id,session['state_revision'])).fetchone()
    if (not isinstance(binding,dict) or set(binding)!={'goalHash','webSettingsRevision'}
            or owner is None
            or binding['goalHash']!=current['hash'] or not session['web_enabled']
            or binding['webSettingsRevision']!=session['web_settings_revision']):
        raise RepositoryError('stale_state','目标澄清询问依据已变化。')
    _put(connection,session['session_id'],'web_target_prompt',
         {'message_id':message_id,'base_hash':current['baseHash'],'web_settings_revision':session['web_settings_revision']})


def pending_prompt(connection, session_id):
    return _load(connection,session_id,'web_target_prompt')


def is_deferred(connection, session_id, base_hash):
    value=_load(connection,session_id,'web_target_deferred')
    return bool(value and value.get('base_hash')==base_hash)


def defer_target(connection, session, base_hash, user_message_id):
    _put(connection,session['session_id'],'web_target_deferred',
         {'base_hash':base_hash,'source_user_message_id':user_message_id})


def remember_target(connection, session, *, base_hash, fields, user_message_id):
    from app.web_citations import goal_state
    if goal_state(connection,session)['baseHash']!=base_hash:
        raise RepositoryError('stale_state','用户目标在澄清期间已变化。')
    PublicTarget(**fields)
    _put(connection,session['session_id'],'web_public_target',
         {'base_hash':base_hash,'fields':fields,'source_user_message_id':user_message_id})
