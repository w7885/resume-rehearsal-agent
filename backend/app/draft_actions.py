"""User-only draft transactions. No model calls or semantic rewriting."""

from collections import Counter
import json
import re

from app.repository import RepositoryError, _ACTIVE_SOURCE, _choice, _id, _integer, _now, _text


def join_markdown(blocks):
    """Keep stored separators; separate independently proposed adjacent blocks."""
    result = ""
    for block in blocks:
        text = block["markdown"]
        if result and text and not result.endswith(("\n", "\r")) and not text.startswith(("\n", "\r")):
            result += "\n\n"
        result += text
    return result


def split_markdown(markdown):
    """Lossless coarse blocks; keep fenced code and nested lists together.

    Blank separators belong to the preceding block. Never infer identity from
    similar text; repeated equal blocks are intentionally treated as ambiguous.
    """
    blocks, current, fence, separator = [], [], None, False
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        top_level = bool(line.strip()) and not line.startswith((" ", "\t"))
        heading = bool(re.match(r"^#{1,6}\s", line))
        if current and fence is None and top_level and (separator or heading):
            blocks.append("".join(current))
            current = []
        current.append(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip():
                fence = None
        separator = not line.strip()
    if current:
        blocks.append("".join(current))
    return blocks


def _user_mutation(repo, context):
    repo._user_path(context)
    _integer(context.expected_state_revision, "预期会话版本")


def _current(connection, session_id):
    return connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()


def _result(repo, connection, session_id):
    session = _current(connection, session_id)
    return {"session": repo._session(session), "draft": repo._draft(connection, session),
            "stateRevision": session["state_revision"], "draftRevision": session["draft_revision"]}


def _write_block(connection, session, markdown, position, kind, refs=(), *, block_id=None, revision=1):
    block_id = block_id or _id("block")
    connection.execute(
        "INSERT INTO draft_blocks(block_id, block_revision, session_id, position, markdown, resume_generation, write_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (block_id, revision, session["session_id"], position, markdown, session["current_resume_generation"], kind))
    for ref in refs:
        connection.execute("INSERT INTO draft_sources VALUES (?, ?, ?, ?, ?, ?)",
                           (session["session_id"], block_id, revision, ref["sourceId"], ref["sourceRevision"], ref["quote"]))
    return block_id


def _invalidate_target(connection, session_id, block_id):
    connection.execute("UPDATE suggestions SET status = 'invalidated' WHERE session_id = ? AND target_block_id = ? AND status = 'pending'",
                       (session_id, block_id))


def apply_decision(repo, context, suggestion_id, payload):
    _user_mutation(repo, context)
    _text(suggestion_id, "建议编号")
    decision = _choice(payload["decision"], {"accept", "reject", "revise"}, "建议决定")
    feedback = _text(payload.get("feedback", ""), "反馈", empty=decision != "revise")
    expected_block = _integer(payload["expectedBlockRevision"], "预期段落版本")
    with repo._operation(context, write=True) as (connection, session):
        row = connection.execute("SELECT * FROM suggestions WHERE session_id = ? AND suggestion_id = ?",
                                 (context.session_id, suggestion_id)).fetchone()
        if row is None:
            raise RepositoryError("source_not_found", "没有找到请求的建议。")
        if row["status"] != "pending" or row["resume_generation"] != session["current_resume_generation"]:
            raise RepositoryError("stale_state", "建议已处理或已经过期，请重新读取。")
        block = repo._current_block(connection, session, row["target_block_id"], expected_block)
        if row["expected_block_revision"] != expected_block or row["before_text"] != block["markdown"]:
            raise RepositoryError("stale_state", "建议的目标旧文或版本已经改变。")
        proposal = repo._suggestion(connection, row)
        # Decisions cannot revive references to corrected or historical sources.
        refs = repo._refs(connection, context.session_id, proposal["sourceRefs"], required=True)
        if decision == "accept":
            if not any(repo._find_source(connection, context.session_id, ref["sourceId"], ref["sourceRevision"])["role"] in {"resume", "supplement"} for ref in refs):
                raise RepositoryError("invalid_input", "个人经历修改缺少个人材料依据。")
            if row["operation"] == "insert_after":
                # Descending updates avoid transient unique-position conflicts.
                later = connection.execute("SELECT block_id, block_revision, position FROM draft_blocks WHERE session_id = ? AND is_current = 1 AND position > ? ORDER BY position DESC",
                                           (context.session_id, block["position"])).fetchall()
                for item in later:
                    connection.execute("UPDATE draft_blocks SET position = ? WHERE block_id = ? AND block_revision = ? AND session_id = ?",
                                       (item["position"] + 1, item["block_id"], item["block_revision"], context.session_id))
                _write_block(connection, session, row["after_text"], block["position"] + 1, "accepted_suggestion", refs)
            else:
                connection.execute("UPDATE draft_blocks SET is_current = 0 WHERE session_id = ? AND block_id = ? AND block_revision = ?",
                                   (context.session_id, block["block_id"], block["block_revision"]))
                if row["operation"] == "replace":
                    _write_block(connection, session, row["after_text"], block["position"], "accepted_suggestion", refs,
                                 block_id=block["block_id"], revision=block["block_revision"] + 1)
            _invalidate_target(connection, context.session_id, block["block_id"])
            connection.execute("UPDATE sessions SET draft_revision = draft_revision + 1 WHERE session_id = ?", (context.session_id,))
        status = {"accept": "accepted", "reject": "rejected", "revise": "invalidated"}[decision]
        connection.execute("UPDATE suggestions SET status = ?, decided_at = ? WHERE session_id = ? AND suggestion_id = ?",
                           (status, _now(), context.session_id, suggestion_id))
        connection.execute("INSERT INTO suggestion_decisions(session_id, suggestion_id, decision, feedback) VALUES (?, ?, ?, ?)",
                           (context.session_id, suggestion_id, decision, feedback))
        repo._touch(connection, context.session_id)
        result = _result(repo, connection, context.session_id)
        result["suggestion"] = repo._suggestion(connection, connection.execute(
            "SELECT * FROM suggestions WHERE session_id = ? AND suggestion_id = ?", (context.session_id, suggestion_id)).fetchone())
        return result


def update_draft(repo, context, markdown):
    _user_mutation(repo, context)
    _text(markdown, "Markdown 正文", empty=True)
    with repo._operation(context, write=True) as (connection, session):
        old = connection.execute("SELECT * FROM draft_blocks WHERE session_id = ? AND is_current = 1 ORDER BY position",
                                 (context.session_id,)).fetchall()
        if join_markdown(old) == markdown:
            return _result(repo, connection, context.session_id)
        parts = split_markdown(markdown)
        old_counts, new_counts = Counter(row["markdown"] for row in old), Counter(parts)
        unique = {row["markdown"]: row for row in old if old_counts[row["markdown"]] == 1 and new_counts[row["markdown"]] == 1}
        connection.execute("UPDATE draft_blocks SET is_current = 0 WHERE session_id = ? AND is_current = 1", (context.session_id,))
        for row in old:
            if row["markdown"] not in unique:
                _invalidate_target(connection, context.session_id, row["block_id"])
        for position, part in enumerate(parts):
            if part in unique:
                row = unique[part]
                connection.execute("UPDATE draft_blocks SET is_current = 1, position = ? WHERE session_id = ? AND block_id = ? AND block_revision = ?",
                                   (position, context.session_id, row["block_id"], row["block_revision"]))
            else:
                # Changed or ambiguous text receives no inherited evidence.
                _write_block(connection, session, part, position, "user_edit")
        connection.execute("UPDATE sessions SET draft_revision = draft_revision + 1 WHERE session_id = ?", (context.session_id,))
        repo._touch(connection, context.session_id)
        return _result(repo, connection, context.session_id)


def replace_draft(repo, context, payload):
    _user_mutation(repo, context)
    _integer(payload["expectedDraftRevision"], "预期稿件版本")
    if payload.get("confirmed") is not True or payload.get("completeConfirmed") is not True:
        raise RepositoryError("invalid_input", "替换需要明确授权，并确认新版完整且属于同人同岗位。")
    _text(payload["materialId"], "新版材料编号")
    _text(payload["authorizationMessageId"], "用户授权消息编号")
    with repo._operation(context, write=True) as (connection, session):
        if payload["expectedDraftRevision"] != session["draft_revision"]:
            raise RepositoryError("stale_state", "当前稿件已经更新。")
        authorization = connection.execute("SELECT * FROM messages WHERE session_id = ? AND message_id = ? AND role = 'user' AND use_status = 'active' AND resume_generation = ?",
                                           (context.session_id, payload["authorizationMessageId"], session["current_resume_generation"])).fetchone()
        if authorization is None or not authorization["content"].strip():
            raise RepositoryError("invalid_input", "缺少本会话当前有效的用户明确替换要求。")
        material = connection.execute("SELECT * FROM attachments WHERE session_id = ? AND attachment_id = ?",
                                      (context.session_id, payload["materialId"])).fetchone()
        if material is None:
            raise RepositoryError("source_not_found", "没有找到新版材料。")
        if material["role"] != "resume" or material["parse_status"] != "parsed" or material["use_status"] not in {"pending", "active"} or material["resume_generation"] != session["current_resume_generation"]:
            raise RepositoryError("invalid_input", "新版须为当前代次、已完整解析且归属明确的简历材料。")
        # A-T07 supports only lossless pasted/Markdown candidates. Binary formats
        # remain pending until parsers and completeness checks are implemented.
        if material["media_type"] != "text/markdown":
            raise RepositoryError("unsupported_format", "当前请用完整 Markdown 或粘贴简历进行替换。")
        rows = connection.execute("SELECT b.* FROM source_blocks b WHERE b.session_id = ? AND b.attachment_id = ? AND b.role = 'resume' AND b.use_status IN ('pending', 'active') AND b.source_revision = (SELECT max(v.source_revision) FROM source_blocks v WHERE v.source_id = b.source_id) ORDER BY b.line_start, b.source_id",
                                  (context.session_id, material["attachment_id"])).fetchall()
        try:
            markdown = material["original_data"].decode("utf-8-sig")
        except UnicodeDecodeError:
            raise RepositoryError("invalid_input", "新版原文编码无效。") from None
        if not markdown.strip() or len(rows) != 1 or rows[0]["text"] != markdown:
            raise RepositoryError("invalid_input", "新版来源与完整原文不一致，请重新粘贴完整简历。")
        source = rows[0]
        before = repo._draft(connection, session)
        previous_scope = [row[0] for row in connection.execute(
            f"SELECT b.source_id FROM source_blocks b JOIN sessions s ON s.session_id = b.session_id WHERE b.session_id = ? AND {_ACTIVE_SOURCE} ORDER BY b.source_id", (context.session_id,))]
        generation = session["current_resume_generation"] + 1
        for table in ("attachments", "source_blocks"):
            connection.execute(f"UPDATE {table} SET use_status = 'history' WHERE session_id = ? AND role != 'jd'", (context.session_id,))
        connection.execute("UPDATE messages SET use_status = 'history' WHERE session_id = ?", (context.session_id,))
        # Preserve only messages parenting the retained active JD, not old chats.
        connection.execute("UPDATE messages SET use_status = 'active', resume_generation = ? WHERE session_id = ? AND message_id IN (SELECT message_id FROM source_blocks WHERE session_id = ? AND role = 'jd' AND use_status = 'active')",
                           (generation, context.session_id, context.session_id))
        connection.execute("UPDATE attachments SET use_status = 'active', resume_generation = ? WHERE session_id = ? AND attachment_id = ?", (generation, context.session_id, material["attachment_id"]))
        connection.execute("UPDATE source_blocks SET use_status = 'active', resume_generation = ? WHERE session_id = ? AND source_id = ? AND source_revision = ?", (generation, context.session_id, source["source_id"], source["source_revision"]))
        connection.execute("UPDATE draft_blocks SET is_current = 0 WHERE session_id = ? AND is_current = 1", (context.session_id,))
        connection.execute("UPDATE suggestions SET status = 'invalidated' WHERE session_id = ? AND status = 'pending'", (context.session_id,))
        connection.execute("UPDATE issues SET is_current = 0 WHERE session_id = ? AND is_current = 1", (context.session_id,))
        connection.execute("UPDATE sessions SET current_resume_generation = ?, draft_revision = draft_revision + 1 WHERE session_id = ?", (generation, context.session_id))
        updated = _current(connection, context.session_id)
        _write_block(connection, updated, markdown, 0, "replacement", [{"sourceId": source["source_id"], "sourceRevision": source["source_revision"], "quote": markdown}])
        next_scope = [row[0] for row in connection.execute(
            f"SELECT b.source_id FROM source_blocks b JOIN sessions s ON s.session_id = b.session_id WHERE b.session_id = ? AND {_ACTIVE_SOURCE} ORDER BY b.source_id", (context.session_id,))]
        replacement_id = _id("replacement")
        connection.execute("INSERT INTO draft_replacements(replacement_id, session_id, request_id, previous_generation, next_generation, before_markdown, after_markdown, previous_scope_json, next_scope_json, authorization_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (replacement_id, context.session_id, payload["requestId"], session["current_resume_generation"], generation,
                            before["markdown"], markdown, json.dumps(previous_scope), json.dumps(next_scope),
                            json.dumps({"messageId": authorization["message_id"], "text": authorization["content"]}, ensure_ascii=False)))
        repo._touch(connection, context.session_id)
        return {**_result(repo, connection, context.session_id), "replacementId": replacement_id}
