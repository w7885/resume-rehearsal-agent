"""A-owned bridge from answered interview turns to scoped, quotable sources."""
from dataclasses import replace
import json
import re

from app.agent.ports import SourceRef
from app.agent_state import GuardedStateStore
from app.repository import Repository, RequestContext


_NONFACT = re.compile(
    r'如果|假如|假设|比如|例如|是否|能否|能不能|可以.{0,15}[吗么？?]|[？?]|'
    r'他说|她说|别人|他人|示例|虚构|编造|帮我写|写成|改成|忽略.{0,8}(?:规则|指令)|'
    r'不确定|不清楚|记不清|好像|可能|应该吧|大概吧|'
    r'直接改|别问|不用问|^(?:请|帮我|替我|给我).{0,20}(?:改|写|润色|优化)|'
    r'^(?:太(?:冗余|啰嗦|口语|随意|长)|精简(?:些|一点)|更正式)'
)
_CONTROL = {'是', '是的', '有', '有的', '没有', '可以', '确认', '采用', '继续', '跳过', '取消', '暂停'}


def factual_answer(text):
    """Conservative admission; rejected/mixed answers remain saved as chat."""
    checked = text
    if re.search(r'比如|例如', text):
        head = re.split(r'比如|例如', text, maxsplit=1)[0]
        if not re.search(r'我(?:负责|使用|实现|完成|新增)|(?:程序|系统|项目|功能).{0,10}(?:支持|实现|提供)|实际变化|变化.{0,10}省|以前我们', head):
            return False
        checked = re.sub(r'比如|例如', '', text)
    return bool(text.strip()) and text.strip().rstrip('。！!') not in _CONTROL and not _NONFACT.search(checked)


def bind_answers(adapter, session_id, doc):
    adapter._check(session_id)
    runtime = adapter.runtime
    store = GuardedStateStore(runtime)
    refs = []
    refs_by_message = {}
    new_context = None
    with runtime.repo._operation(runtime.context, write=True) as (connection, session):
        # Only the questions already associated with this experience are eligible.
        ids = list(dict.fromkeys(q.answer_message_id for q in doc.questions
                                if q.outcome == 'answered' and q.slot not in {None, 'confirm', 'preference'} and q.answer_message_id))
        if not ids:
            return []
        old_prefix = store._key(connection, session, '')
        bound = Repository(runtime.repo.path, _connection=connection)
        old_data = bound.get_session_context(RequestContext(session_id))
        changed = False
        for message_id in ids:
            row = connection.execute("SELECT * FROM messages WHERE session_id=? AND message_id=? AND role='user' AND input_channel='chat' AND use_status='active' AND resume_generation=?",
                                     (session_id, message_id, session['current_resume_generation'])).fetchone()
            if row is None or not factual_answer(row['content']):
                continue
            # Never resurrect a source that was later corrected/retired by a user.
            sources = connection.execute('SELECT * FROM source_blocks WHERE session_id=? AND message_id=? ORDER BY rowid DESC', (session_id, message_id)).fetchall()
            if sources:
                source = next((s for s in sources if s['role'] == 'supplement' and s['use_status'] == 'active'), None)
                if source is None:
                    continue
                ref = SourceRef(source_id=source['source_id'], source_revision=source['source_revision'], quote=source['text'])
            else:
                current = bound.get_session(RequestContext(session_id))
                source = bound.save_source(RequestContext(session_id, current['stateRevision']), text=row['content'],
                                           role='supplement', use_status='active',
                                           origin={'kind': 'message', 'messageId': message_id})
                ref = SourceRef(source_id=source['sourceId'], source_revision=source['sourceRevision'], quote=source['text'])
                changed = True
            refs.append(ref)
            refs_by_message[message_id] = ref
        current = bound.get_session(RequestContext(session_id))
        new_prefix = store._key(connection, session, '')
        # Preserve memory only for these trusted additions, in the same
        # transaction. Arbitrary source edits still invalidate old memory.
        rows = connection.execute("SELECT * FROM agent_state WHERE session_id=? AND scope IN ('session','experience','run')", (session_id,)).fetchall()
        for row in rows:
            if changed and row['key'].startswith(old_prefix):
                key = new_prefix + row['key'][len(old_prefix):]
                connection.execute('UPDATE agent_state SET key=? WHERE session_id=? AND scope=? AND key=?',
                                   (key, session_id, row['scope'], row['key']))
        key = new_prefix + doc.experience_id
        row = connection.execute("SELECT doc_json FROM agent_state WHERE session_id=? AND scope='experience' AND key=?", (session_id, key)).fetchone()
        if row:
            memory = json.loads(row[0])
            memory['source_ids'] = list(dict.fromkeys([*memory.get('source_ids', []), *(r.source_id for r in refs)]))
            for fact in memory.get('facts', []):
                ref = refs_by_message.get(fact.get('message_id'))
                if ref is not None:
                    fact['source_refs'] = [ref.model_dump(mode='json')]
            connection.execute("UPDATE agent_state SET doc_json=?,revision=revision+1 WHERE session_id=? AND scope='experience' AND key=?", (json.dumps(memory, ensure_ascii=False), session_id, key))
        if not changed:
            return refs
        # New facts for the current experience do not undo an explicit decision
        # to retain an unrelated previous experience's original wording.
        from app.agent_progress import _key, _load, _signature
        progress = _load(connection, session)
        updated = bound.get_session_context(RequestContext(session_id))
        for block in old_data['draft']['blocks']:
            saved = progress['reviewed'].get(block['blockId'])
            if saved and saved.get('signature') == _signature(block, old_data['sources']):
                saved['signature'] = _signature(block, updated['sources'])
        connection.execute("UPDATE agent_state SET doc_json=?,revision=revision+1 WHERE session_id=? AND scope='session' AND key=?",
                           (json.dumps(progress, ensure_ascii=False), session_id, _key(session)))
        # save_source uses the user-edit path, which interrupts runs. This is
        # a Host-owned addition validated under the still-current run lock;
        # resume only that exact run on its new input revision atomically.
        connection.execute("UPDATE runs SET status='running',input_state_revision=? WHERE session_id=? AND run_id=?",
                           (current['stateRevision'], session_id, runtime.context.run_id))
        connection.execute("UPDATE sessions SET status='running' WHERE session_id=?", (session_id,))
        connection.execute('INSERT OR REPLACE INTO agent_turn_effects VALUES (?,?,?,?,?)',
                           (session_id, runtime.user_id, current['stateRevision'], current['draftRevision'], current['currentResumeGeneration']))
        new_context = replace(runtime.context, expected_state_revision=current['stateRevision'])
    runtime.context = adapter.context = new_context
    return refs
