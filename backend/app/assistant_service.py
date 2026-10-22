"""A-T11.1 reply persistence/association only; never resolves user decisions."""
import json
from app.repository import RepositoryError, RequestContext, _choice, _id, _integer, _keys, _text

QUESTION_KINDS={'question','change_question'}


def host_only(context):
    if not isinstance(context,RequestContext) or context.run_id is None:
        raise RepositoryError('scope_violation','助手回执必须由绑定已保存运行的 Host 提交。',False)


def user_turn(connection,context,session,user_id):
    row=connection.execute("SELECT * FROM messages WHERE session_id = ? AND message_id = ? AND role = 'user' AND input_channel = 'chat' AND use_status = 'active' AND resume_generation = ?",(context.session_id,user_id,session['current_resume_generation'])).fetchone()
    latest=connection.execute("SELECT message_id FROM messages WHERE session_id = ? AND role = 'user' ORDER BY rowid DESC LIMIT 1",(context.session_id,)).fetchone()
    if row is None:
        raise RepositoryError('source_not_found','没有找到当前会话已保存的用户回合。')
    same_state = row['saved_after_revision'] == session['state_revision']
    if not same_state:
        effect = connection.execute('SELECT * FROM agent_turn_effects WHERE session_id=? AND user_message_id=?', (context.session_id,user_id)).fetchone()
        same_state = bool(effect and effect['state_revision']==session['state_revision']
                          and effect['draft_revision']==session['draft_revision']
                          and effect['resume_generation']==session['current_resume_generation'])
    if not same_state:
        # After this exact turn's decision transaction, a fresh Host run may
        # report the saved outcome. Any unrelated subsequent edit still fails.
        resolution = connection.execute('SELECT * FROM chat_decision_resolutions WHERE session_id = ? AND user_message_id = ?',
                                        (context.session_id, user_id)).fetchone()
        if resolution is not None and resolution['status'] == 'resolved':
            receipt = json.loads(resolution['receipt_json'])
            same_state = (resolution['input_state_revision'] == row['saved_after_revision']
                          and receipt['stateRevision'] == session['state_revision']
                          and receipt['draftRevision'] == session['draft_revision'])
    if not same_state or latest['message_id']!=user_id:
        raise RepositoryError('stale_state','用户回合之后已有其他输入或修改，请按最新状态处理。')
    return row


def valid_suggestion(repo,connection,session,suggestion_id):
    row=connection.execute('SELECT * FROM suggestions WHERE session_id = ? AND suggestion_id = ?',(session['session_id'],suggestion_id)).fetchone()
    if row is None:
        raise RepositoryError('source_not_found','建议不存在于当前会话。')
    if row['status']!='pending' or row['resume_generation']!=session['current_resume_generation']:
        raise RepositoryError('stale_state','建议已处理或不再适用当前简历。')
    block=repo._current_block(connection,session,row['target_block_id'],row['expected_block_revision'])
    if block['markdown']!=row['before_text']:
        raise RepositoryError('stale_state','询问目标原文已改变。')
    proposal=repo._suggestion(connection,row)
    repo._refs(connection,session['session_id'],proposal['sourceRefs'],required=True)
    return proposal


def question_state(repo,connection,session,meta):
    if meta['kind'] not in QUESTION_KINDS:return None
    if meta['resume_generation']!=session['current_resume_generation']:return 'history'
    try:
        for reference in json.loads(meta['source_refs_json']):
            repo._refs(connection,session['session_id'],[reference])
        for link in connection.execute('SELECT * FROM assistant_suggestion_links WHERE session_id = ? AND message_id = ?',(session['session_id'],meta['message_id'])):
            saved=json.loads(link['snapshot_json'])
            current=valid_suggestion(repo,connection,session,link['suggestion_id'])
            if any(current[key]!=saved[key] for key in ('operation','target','before','after','sourceRefs')):return 'outdated'
    except RepositoryError:return 'outdated'
    state=meta['bound_state_revision']
    if session['state_revision']==state:
        run=connection.execute('SELECT status,generation FROM runs WHERE session_id = ? AND run_id = ?',(session['session_id'],meta['run_id'])).fetchone()
        latest=connection.execute('SELECT max(generation) FROM runs WHERE session_id = ?',(session['session_id'],)).fetchone()[0]
        return 'awaiting_reply' if run and run['status'] in {'running','waiting_user','completed'} and run['generation']==latest else 'outdated'
    # Capture the ordinary S -> S+1 reply without treating it as authorization.
    replies=connection.execute("SELECT rowid AS ordinal,* FROM messages WHERE session_id = ? AND role = 'user' AND saved_before_revision = ? AND saved_after_revision = ? AND resume_generation = ?",(session['session_id'],state,session['state_revision'],session['current_resume_generation']))
    for reply in replies:
        candidates=json.loads(reply['reply_candidates_json'] or '[]')
        if meta['message_id'] not in candidates:continue
        if reply['reply_to_message_id'] and reply['reply_to_message_id']!=meta['message_id']:continue
        newer=connection.execute("SELECT 1 FROM assistant_messages a JOIN messages m ON m.message_id = a.message_id AND m.session_id = a.session_id WHERE a.session_id = ? AND a.kind IN ('question','change_question') AND m.rowid > ? LIMIT 1",(session['session_id'],reply['ordinal'])).fetchone()
        if newer is None:return 'reply_saved'
    return 'outdated'


def reply_candidates(repo,connection,session):
    rows=connection.execute("SELECT * FROM assistant_messages WHERE session_id = ? AND resume_generation = ? AND bound_state_revision = ? AND kind IN ('question','change_question') ORDER BY rowid",(session['session_id'],session['current_resume_generation'],session['state_revision'])).fetchall()
    return [row['message_id'] for row in rows if question_state(repo,connection,session,row)=='awaiting_reply']


def validate_reply_target(connection,session,message_id):
    meta=connection.execute("SELECT * FROM assistant_messages WHERE session_id = ? AND message_id = ? AND kind IN ('question','change_question')",(session['session_id'],message_id)).fetchone()
    if meta is None:raise RepositoryError('source_not_found','所回复的询问不存在于当前会话。')
    if meta['resume_generation']!=session['current_resume_generation']:
        raise RepositoryError('stale_state','该询问属于历史简历，请按当前对话补充。')
    # Outdated links may be preserved as user-authored references, but will not
    # pass the reply_saved bridge or become permission to modify any draft.


def metadata_view(repo,connection,session,message_id):
    meta=connection.execute('SELECT * FROM assistant_messages WHERE session_id = ? AND message_id = ?',(session['session_id'],message_id)).fetchone()
    if meta is None:return None
    return {'kind':meta['kind'],'mode':meta['mode'],'replyToUserMessageId':meta['user_message_id'],
            'questionState':question_state(repo,connection,session,meta),'sourceRefs':json.loads(meta['source_refs_json'])}


def prompt_snapshot(repo,connection,session,message_id):
    meta=connection.execute('SELECT * FROM assistant_messages WHERE session_id = ? AND message_id = ?',(session['session_id'],message_id)).fetchone()
    if meta is None or meta['kind'] not in QUESTION_KINDS:raise RepositoryError('source_not_found','询问上下文不存在。')
    return {'promptMessageId':message_id,'state':question_state(repo,connection,session,meta),
            'boundStateRevision':meta['bound_state_revision'],'boundDraftRevision':meta['bound_draft_revision'],
            'resumeGeneration':meta['resume_generation'],'mode':meta['mode'],
            'suggestions':[json.loads(row[0]) for row in connection.execute('SELECT snapshot_json FROM assistant_suggestion_links WHERE session_id = ? AND message_id = ? ORDER BY suggestion_id',(session['session_id'],message_id))],
            'sourceRefs':json.loads(meta['source_refs_json'])}


def get_turn_context(service,context,user_id):
    host_only(context)
    with service.repository._operation(context) as (connection,session):
        if session['mode']!='local':raise RepositoryError('scope_violation','模拟历史不能接入个人 Agent 回合。')
        row=user_turn(connection,context,session,user_id)
        ids=json.loads(row['reply_candidates_json'] or '[]')
        if row['reply_to_message_id'] and row['reply_to_message_id'] not in ids:ids.append(row['reply_to_message_id'])
        resolution=connection.execute('SELECT receipt_json FROM chat_decision_resolutions WHERE session_id = ? AND user_message_id = ?',
                                      (context.session_id,user_id)).fetchone()
        return {'userMessage':service.repository._message(row),'replyToMessageId':row['reply_to_message_id'],
                'decisionReceipt':json.loads(resolution[0]) if resolution else None,
                'candidatePrompts':[prompt_snapshot(service.repository,connection,session,mid) for mid in ids],
                'stateRevision':session['state_revision'],'draftRevision':session['draft_revision']}


def save_assistant(service,context,payload,synthetic=False):
    host_only(context)
    _keys(payload,{'requestId','userMessageId','content','kind','suggestionIds'},{'sourceRefs'})
    for key in ('requestId','userMessageId'):
        if len(_text(payload[key],key))>128:raise RepositoryError('invalid_input','消息标识过长。')
    content=_text(payload['content'],'助手正文')
    if len(content)>50000:raise RepositoryError('invalid_input','助手正文超过当前长度限制。')
    kind=_choice(payload['kind'],{'reply','question','change_question'},'助手消息类型')
    ids=payload['suggestionIds']
    if not isinstance(ids,list) or len(ids)>10 or any(not isinstance(mid,str) or not mid for mid in ids) or len(set(ids))!=len(ids):raise RepositoryError('invalid_input','请提供至多 10 个互不重复的建议编号。')
    if (kind=='change_question')!=bool(ids):raise RepositoryError('invalid_input','修改询问必须绑定真实建议，普通回复不隐含修改目标。')
    refs=payload.get('sourceRefs',[])
    if not isinstance(refs,list) or len(refs)>50:raise RepositoryError('invalid_input','回复引用范围不符合限制。')
    with service.repository._operation(context):pass
    def apply(repo):
        with repo._operation(context,write=True) as (connection,session):
            if session['mode']!='local':raise RepositoryError('scope_violation','模拟历史不能接入个人 Agent 回合。')
            user_turn(connection,context,session,payload['userMessageId'])
            proposals=[valid_suggestion(repo,connection,session,sid) for sid in ids]
            references=repo._refs(connection,context.session_id,refs)
            for proposal in proposals:
                for ref in proposal['sourceRefs']:
                    if ref not in references:references.append(ref)
            mid=_id('message')
            connection.execute("INSERT INTO messages(message_id,session_id,role,content,kind,resume_generation,input_channel,reply_to_message_id,saved_before_revision,saved_after_revision) VALUES (?,?,'assistant',?,'message',?,'chat',?,?,?)",(mid,context.session_id,content,session['current_resume_generation'],payload['userMessageId'],session['state_revision'],session['state_revision']))
            connection.execute('INSERT INTO assistant_messages VALUES (?,?,?,?,?,?,?,?,?,?)',(context.session_id,mid,payload['userMessageId'],context.run_id,kind,'synthetic' if synthetic else 'host',session['state_revision'],session['draft_revision'],session['current_resume_generation'],json.dumps(references,ensure_ascii=False)))
            for proposal in proposals:
                snapshot={key:proposal[key] for key in ('suggestionId','operation','target','before','after','sourceRefs')}
                connection.execute('INSERT INTO assistant_suggestion_links VALUES (?,?,?,?)',(context.session_id,mid,proposal['suggestionId'],json.dumps(snapshot,ensure_ascii=False)))
            row=connection.execute('SELECT * FROM messages WHERE session_id = ? AND message_id = ?',(context.session_id,mid)).fetchone()
            from app.chat_service import message_view
            sequence=connection.execute('SELECT count(*) FROM messages WHERE session_id = ?',(context.session_id,)).fetchone()[0]
            return {'message':message_view(repo,connection,row,session,sequence),'stateRevision':session['state_revision'],'draftRevision':session['draft_revision']}
    return service._mutate(f'session:{context.session_id}','save_assistant_message',{**payload,'runId':context.run_id,'generation':context.generation,'stateRevision':context.expected_state_revision,'synthetic':synthetic},payload['requestId'],apply,context.session_id)
