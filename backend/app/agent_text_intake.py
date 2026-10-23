"""Confirm pasted chat material without turning ordinary conversation into facts."""
import hashlib
import json

from app.repository import Repository, RepositoryError, RequestContext


def fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def activate(runtime, candidate):
    """Import only the exact saved, scoped message the user just confirmed."""
    with runtime.repo._operation(runtime.context, write=True) as (connection, session):
        bound = Repository(runtime.repo.path, _connection=connection)
        if bound.has_draft_history(RequestContext(runtime.sid)):
            raise RepositoryError('replacement_required', '当前已有简历，不能自动覆盖。')
        row = connection.execute("SELECT * FROM messages WHERE session_id=? AND message_id=? AND role='user' AND input_channel='chat' AND use_status='active' AND resume_generation=?",
                                 (runtime.sid, candidate['messageId'], session['current_resume_generation'])).fetchone()
        if row is None or fingerprint(row['content']) != candidate['contentHash']:
            raise RepositoryError('stale_state', '待确认的文字已不适用于当前简历。')
        context = RequestContext(runtime.sid, session['state_revision'])
        source = bound.save_source(context, text=row['content'], role='resume',
                                   origin={'kind': 'message', 'messageId': row['message_id']}, use_status='active')
        current = bound.get_session(RequestContext(runtime.sid))
        bound.create_initial_draft(RequestContext(runtime.sid, current['stateRevision']), [
            {'markdown': row['content'], 'sourceRefs': [{'sourceId': source['sourceId'],
                'sourceRevision': source['sourceRevision'], 'quote': row['content']}]}])
        current = bound.get_session(RequestContext(runtime.sid))
        connection.execute('INSERT OR REPLACE INTO agent_turn_effects VALUES (?,?,?,?,?)',
                           (runtime.sid, runtime.user_id, current['stateRevision'], current['draftRevision'], current['currentResumeGeneration']))
    runtime.initial_import = True


async def handle(runtime, turn):
    """True means a question was saved; False lets the existing Agent continue."""
    with runtime.repo._operation(runtime.context) as (connection, session):
        if runtime.repo.has_draft_history(RequestContext(runtime.sid)):
            return False
        candidates = []
        for prompt in turn['candidatePrompts']:
            if prompt['state'] != 'reply_saved' or (turn['replyToMessageId'] and turn['replyToMessageId'] != prompt['promptMessageId']):
                continue
            row = connection.execute('SELECT materials_json FROM agent_material_prompts WHERE session_id=? AND prompt_message_id=?',
                                     (runtime.sid, prompt['promptMessageId'])).fetchone()
            if row:
                candidates.extend(item for item in json.loads(row[0]) if 'messageId' in item)
        # Bound recovery context also covers a resume pasted before an old fallback.
        rows = connection.execute("SELECT message_id,content FROM messages WHERE session_id=? AND role='user' AND input_channel='chat' AND use_status='active' AND resume_generation=? AND trim(content)!='' ORDER BY rowid DESC LIMIT 6",
                                  (runtime.sid, session['current_resume_generation'])).fetchall()
    if candidates:
        choice = await runtime._json('判断用户是否确认把指定聊天文字作为本次待修改的简历。返回 status=confirm/decline/clarify 和 messageId。仅明确确认且范围唯一时 confirm；否定、不是本人材料、仅供参考或示例时 decline；条件、假设、引用或范围不明时 clarify。messageId 只能来自候选，不能将其他消息或新答复当成原文。',
                                     {'userContent': turn['userMessage']['content'], 'candidates': candidates})
        allowed = {item['messageId']: item for item in candidates}
        if choice.get('status') == 'confirm' and choice.get('messageId') in allowed:
            activate(runtime, allowed[choice['messageId']])
            runtime._continue()
            runtime._reply('已将确认的文字原样保存为初始稿，接下来会依据原文分析。')
            return False
        if choice.get('status') == 'decline':
            runtime._reply('好的，这段文字保留在对话中，不导入当前稿。请提供本次要修改的简历，或说明希望处理的内容。', 'question')
        else:
            runtime._reply('请确认：是否把刚才指定的那段文字作为本次要修改的简历？', 'question', materials=candidates)
        return True
    # No text is silently truncated and imported. Oversized messages can use uploads.
    messages = [{'messageId': row['message_id'], 'content': row['content']} for row in reversed(rows) if len(row['content']) <= 20000]
    if not messages:
        if turn['userMessage']['content'].strip():
            runtime._reply('这段文字较长，请保存为 Markdown 文件上传；我会保留这里的原话，不截取片段导入。', 'question')
            return True
        return False
    choice = await runtime._json('识别用户是否提供了本次要修改的简历原文。返回 status=candidate/none 和 messageId。candidate 仅指包含实际履历的完整简历或经历段落；不要把求职意向、操作指令、提问、假设、否定或仅供参考的示例当成履历。结合最新用户原话判断是否仍要处理先前材料；用户拒绝或转向时 none。不能合成原文，编号只能选自 messages。此步骤只发起用途确认，不授权写入事实。',
                                 {'userContent': turn['userMessage']['content'], 'messages': messages})
    allowed = {item['messageId']: item for item in messages}
    if choice.get('status') != 'candidate' or choice.get('messageId') not in allowed:
        if len(turn['userMessage']['content']) > 20000:
            runtime._reply('这段文字较长，请保存为 Markdown 文件上传；我会保留这里的原话，不截取片段导入。', 'question')
            return True
        return False
    selected = allowed[choice['messageId']]
    runtime._reply('已收到简历文字。是否将这段文字作为本次要修改的简历？确认后我会开始分析。', 'question',
                   materials=[{'messageId': selected['messageId'], 'contentHash': fingerprint(selected['content'])}])
    return True
