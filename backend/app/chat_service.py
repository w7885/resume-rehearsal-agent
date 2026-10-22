"""Chat persistence only. Raw user messages never become facts or decisions."""
import json

from app.repository import RepositoryError, RequestContext, _ACTIVE_SOURCE, _integer, _text


# List metadata without materializing stored original-file BLOBs.
MATERIAL_COLUMNS = """a.attachment_id, a.session_id, a.name, a.media_type,
    a.original_order, a.role, a.parse_status, a.use_status, a.resume_generation,
    a.parse_report_json, a.recognition_report_json, length(a.original_data) AS byte_size"""


def material_view(repo, row, session):
    return {**repo._material(row), "byteSize": row["byte_size"],
            "current": row["use_status"] != "history" and row["resume_generation"] == session["current_resume_generation"]}


def message_view(repo, connection, row, session, sequence):
    from app.assistant_service import metadata_view
    attachments = [material_view(repo, attachment, session) for attachment in connection.execute(
        f"SELECT {MATERIAL_COLUMNS} FROM chat_message_attachments c JOIN attachments a ON a.session_id = c.session_id AND a.attachment_id = c.attachment_id WHERE c.session_id = ? AND c.message_id = ? ORDER BY c.position",
        (session["session_id"], row["message_id"]))]
    return {**repo._message(row), "sequence": sequence, "inputChannel": row["input_channel"],
            "historical": row["use_status"] != "active" or row["resume_generation"] != session["current_resume_generation"],
            "attachments": attachments,
            "assistant": metadata_view(repo,connection,session,row['message_id']) if row['role']=='assistant' else None}


def create_chat_session(repo):
    created = repo.create_session("新对话")
    repo._connection.execute("UPDATE sessions SET direction_status = 'pending' WHERE session_id = ?", (created["sessionId"],))
    return repo.get_session(RequestContext(created["sessionId"]))


def read_conversation(repo, context):
    repo._user_path(context)
    with repo._operation(context) as (connection, session):
        messages = [message_view(repo, connection, row, session, sequence)
                    for sequence, row in enumerate(connection.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY rowid", (context.session_id,)), 1)]
        materials = [material_view(repo, row, session) for row in connection.execute(
            f"SELECT {MATERIAL_COLUMNS} FROM attachments a WHERE a.session_id = ? ORDER BY coalesce(a.reading_order,a.original_order), a.original_order, a.attachment_id", (context.session_id,))]
        # Expose a single delivery notice, never the internal issue checklist.
        conflict = connection.execute("SELECT 1 FROM issues WHERE session_id = ? AND resume_generation = ? AND kind = 'conflict' AND status != 'resolved' LIMIT 1",
                                      (context.session_id, session["current_resume_generation"])).fetchone()
        changed_evidence = connection.execute(f"""SELECT 1 FROM draft_sources r
            JOIN draft_blocks d ON d.session_id = r.session_id AND d.block_id = r.block_id AND d.block_revision = r.block_revision
            JOIN source_blocks b ON b.session_id = r.session_id AND b.source_id = r.source_id AND b.source_revision = r.source_revision
            JOIN sessions s ON s.session_id = r.session_id
            WHERE r.session_id = ? AND d.is_current = 1 AND NOT ({_ACTIVE_SOURCE}) LIMIT 1""", (context.session_id,)).fetchone()
        return {"session": repo._session(session), "messages": messages, "materials": materials,
                "draft": repo._draft(connection, session),
                "draftReviewRequired": bool(conflict or changed_evidence),
                "capabilities": {"agentAvailable": False, "decisionExecutionAvailable": False}}


def save_chat_message(repo, context, payload):
    repo._user_path(context)
    _integer(context.expected_state_revision, "预期上下文版本")
    content = _text(payload["content"], "消息正文", empty=True)
    ids = payload["attachmentIds"]
    if not isinstance(ids, list) or len(ids) > 10 or any(not isinstance(value, str) or not value.strip() for value in ids) or len(set(ids)) != len(ids):
        raise RepositoryError("invalid_input", "请提供最多 10 个互不重复的本会话附件编号。")
    if not content.strip() and not ids:
        raise RepositoryError("invalid_input", "请先输入消息或添加附件。")
    with repo._operation(context, write=True) as (connection, session):
        if session["mode"] != "local":
            raise RepositoryError("scope_violation", "模拟会话在聊天页仅供回顾，请新建个人材料对话。", False)
        attachments = []
        for attachment_id in ids:
            row = connection.execute(f"SELECT {MATERIAL_COLUMNS} FROM attachments a WHERE a.session_id = ? AND a.attachment_id = ?", (context.session_id, attachment_id)).fetchone()
            if row is None:
                raise RepositoryError("source_not_found", "附件不存在或不属于当前会话。")
            if row["use_status"] == "history" or row["resume_generation"] != session["current_resume_generation"]:
                raise RepositoryError("stale_state", "附件属于历史简历，请重新上传后发送。")
            attachments.append(row)
        from app.assistant_service import reply_candidates,validate_reply_target
        reply_to=payload.get('replyToMessageId')
        if reply_to is not None:
            if len(_text(reply_to,'回复询问编号'))>128:raise RepositoryError('invalid_input','回复询问编号过长。')
            validate_reply_target(connection,session,reply_to)
        candidates=reply_candidates(repo,connection,session)
        message = repo.save_message(context, content, input_channel="chat")
        current_revision=repo.get_session(RequestContext(context.session_id))['stateRevision']
        connection.execute('UPDATE messages SET reply_to_message_id = ?,saved_before_revision = ?,saved_after_revision = ?,reply_candidates_json = ? WHERE session_id = ? AND message_id = ?',
            (reply_to,session['state_revision'],current_revision,json.dumps(candidates),context.session_id,message['messageId']))
        for position, attachment_id in enumerate(ids):
            connection.execute("INSERT INTO chat_message_attachments(session_id, message_id, attachment_id, position) VALUES (?, ?, ?, ?)",
                               (context.session_id, message["messageId"], attachment_id, position))
        if session["direction_status"] == "pending" and session["chat_title"] is None:
            title = content.strip().splitlines()[0][:24] if content.strip() else attachments[0]["name"][:24]
            connection.execute("UPDATE sessions SET chat_title = ? WHERE session_id = ?", (title, context.session_id))
        current = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (context.session_id,)).fetchone()
        row = connection.execute("SELECT * FROM messages WHERE session_id = ? AND message_id = ?", (context.session_id, message["messageId"])).fetchone()
        sequence = connection.execute("SELECT count(*) FROM messages WHERE session_id = ?", (context.session_id,)).fetchone()[0]
        return {"session": repo._session(current), "message": message_view(repo, connection, row, current, sequence),
                "stateRevision": current["state_revision"], "draftRevision": current["draft_revision"]}
