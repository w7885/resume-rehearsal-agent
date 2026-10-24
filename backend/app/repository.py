"""Session-scoped business persistence. No model, HTTP, or arbitrary SQL API."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.db import DEFAULT_DATABASE_PATH, database, transaction


class RepositoryError(ValueError):
    def __init__(self, code: str, message: str, recoverable: bool = True):
        super().__init__(message)
        self.code, self.message, self.recoverable = code, message, recoverable


def _text(value, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise RepositoryError("invalid_input", f"{label}必须是有效文字。")
    return value


def _integer(value, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise RepositoryError("invalid_input", f"{label}必须是数据库支持范围内、大于等于 {minimum} 的整数。")
    return value


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise RepositoryError("invalid_input", f"{label}不在允许范围内。")
    return value


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise RepositoryError("invalid_input", "字段缺失或包含未允许的字段。")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id(prefix):
    return f"{prefix}-{uuid4().hex}"


@dataclass(frozen=True)
class RequestContext:
    """Constructed by the trusted Host, never from model tool arguments."""
    session_id: str
    expected_state_revision: int | None = None
    run_id: str | None = None
    generation: int | None = None

    def __post_init__(self):
        _text(self.session_id, "会话编号")
        if self.expected_state_revision is not None:
            _integer(self.expected_state_revision, "上下文版本")
        if self.run_id is not None or self.generation is not None:
            _text(self.run_id, "运行编号")
            _integer(self.generation, "运行代号")
            _integer(self.expected_state_revision, "运行输入版本")


# Current JD survives a resume replacement; old personal material does not.
# Both source snapshots and their original material/message must be usable.
_ACTIVE_SOURCE = """
    b.use_status = 'active' AND b.role IN ('resume', 'supplement', 'jd')
    AND b.source_revision = (SELECT max(v.source_revision) FROM source_blocks v
                            WHERE v.source_id = b.source_id AND v.session_id = b.session_id)
    AND (b.role = 'jd' OR b.resume_generation = s.current_resume_generation)
    AND (
      (b.attachment_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM attachments a WHERE a.attachment_id = b.attachment_id AND a.session_id = b.session_id
        AND a.parse_status IN ('parsed', 'partial')
        AND a.role = b.role
        AND (a.use_status = 'active' OR (b.role = 'jd' AND a.use_status = 'history'))
        AND (b.role = 'jd' OR a.resume_generation = s.current_resume_generation)))
      OR
      (b.message_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM messages m WHERE m.message_id = b.message_id AND m.session_id = b.session_id
        AND m.role = 'user'
        AND (m.use_status = 'active' OR (b.role = 'jd' AND m.use_status = 'history'))
        AND (b.role = 'jd' OR m.resume_generation = s.current_resume_generation)))
    )
"""


class Repository:
    def __init__(self, path: str | Path = DEFAULT_DATABASE_PATH, *, _connection=None):
        # Path is a trusted application setting, not a model-visible parameter.
        self.path = Path(path).resolve()
        # Used only by the trusted HTTP service for one short request transaction.
        # Never reuse a bound Repository across transactions or threads.
        self._connection = _connection
        self._touched_sessions = set()

    @contextmanager
    def _connection_scope(self, *, write=False):
        if self._connection is not None:
            if not self._connection.in_transaction:
                raise RepositoryError("storage_failed", "业务事务已经结束。")
            yield self._connection
            return
        with database(self.path, read_only=not write) as connection:
            if write:
                with transaction(connection):
                    yield connection
            else:
                connection.execute("BEGIN")
                try:
                    yield connection
                finally:
                    connection.rollback()

    @contextmanager
    def _operation(self, context: RequestContext, *, write=False):
        if not isinstance(context, RequestContext):
            raise RepositoryError("invalid_input", "缺少程序绑定的会话上下文。")
        with self._connection_scope(write=write) as connection:
            yield connection, self._check_context(connection, context)

    def _check_context(self, connection, context):
        session = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (context.session_id,)).fetchone()
        if session is None:
            raise RepositoryError("session_not_found", "没有找到当前会话。")
        if context.expected_state_revision is not None and context.expected_state_revision != session["state_revision"]:
            raise RepositoryError("stale_state", "会话已更新，请读取最新状态。")
        if context.run_id is not None:
            run = connection.execute("SELECT * FROM runs WHERE session_id = ? AND run_id = ?",
                                     (context.session_id, context.run_id)).fetchone()
            latest = connection.execute("SELECT max(generation) FROM runs WHERE session_id = ?", (context.session_id,)).fetchone()[0]
            if run is None or run["status"] != "running" or run["generation"] != context.generation or latest != context.generation:
                raise RepositoryError("cancelled", "本次运行已结束、取消或被替代。")
            if run["input_state_revision"] != session["state_revision"] or run["resume_generation"] != session["current_resume_generation"]:
                raise RepositoryError("stale_state", "运行依据已经过期。")
        return session

    @staticmethod
    def _user_path(context):
        if not isinstance(context, RequestContext):
            raise RepositoryError("invalid_input", "缺少程序绑定的会话上下文。")
        if context.run_id is not None:
            raise RepositoryError("scope_violation", "模型运行不能修改原始材料或正式稿。", False)

    def _touch(self, connection, session_id):
        if self._connection is connection and session_id in self._touched_sessions:
            return
        connection.execute("UPDATE sessions SET state_revision = state_revision + 1, updated_at = ?, status = 'idle' WHERE session_id = ?",
                           (_now(), session_id))
        connection.execute("UPDATE runs SET status = 'interrupted' WHERE session_id = ? AND status = 'running'", (session_id,))
        if self._connection is connection:
            self._touched_sessions.add(session_id)

    @staticmethod
    def _session(row):
        return {"sessionId": row["session_id"], "direction": row["direction"],
                "stateRevision": row["state_revision"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                "draftRevision": row["draft_revision"], "currentResumeGeneration": row["current_resume_generation"], "status": row["status"], "mode": row["mode"],
                "title": row["chat_title"] or row["direction"], "directionStatus": row["direction_status"],
                "targetDirection": row["direction"] if row["direction_status"] == "provided" else None}

    def create_session(self, direction: str):
        direction = _text(direction, "求职方向").strip()
        session_id = _id("session")
        with self._connection_scope(write=True) as connection:
            connection.execute("INSERT INTO sessions(session_id, direction) VALUES (?, ?)", (session_id, direction))
            return self._session(connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone())

    def list_sessions(self):
        with self._connection_scope() as connection:
            return [self._session(row) for row in connection.execute("SELECT * FROM sessions ORDER BY updated_at DESC, session_id")]

    def get_session(self, context: RequestContext):
        with self._operation(context) as (_, session):
            return self._session(session)

    @staticmethod
    def _message(row):
        return {"messageId": row["message_id"], "sessionId": row["session_id"], "role": row["role"],
                "content": row["content"], "createdAt": row["created_at"], "kind": row["kind"],
                "replyToMessageId":row['reply_to_message_id'] if 'reply_to_message_id' in row.keys() else None,
                "savedBeforeRevision":row['saved_before_revision'] if 'saved_before_revision' in row.keys() else None,
                "savedAfterRevision":row['saved_after_revision'] if 'saved_after_revision' in row.keys() else None}

    def save_message(self, context: RequestContext, content: str, *, role="user", kind="message", input_channel="legacy"):
        _choice(input_channel, {"legacy", "chat"}, "消息入口")
        _text(content, "消息正文", empty=input_channel == "chat")
        _choice(role, {"user", "assistant"}, "消息角色")
        _choice(kind, {"message", "supplement", "correction", "skip"}, "消息类型")
        if role == "user":
            self._user_path(context)
        elif kind != "message":
            raise RepositoryError("invalid_input", "Agent 消息不能充当用户补充或纠正。")
        if input_channel == "chat" and role != "user":
            raise RepositoryError("scope_violation", "聊天用户入口不能伪造助手发言。", False)
        with self._operation(context, write=True) as (connection, session):
            message_id = _id("message")
            connection.execute("INSERT INTO messages(message_id, session_id, role, content, kind, resume_generation, input_channel) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (message_id, context.session_id, role, content, kind, session["current_resume_generation"], input_channel))
            if role == "user":
                self._touch(connection, context.session_id)
            return self._message(connection.execute("SELECT * FROM messages WHERE message_id = ? AND session_id = ?", (message_id, context.session_id)).fetchone())

    def list_messages(self, context: RequestContext):
        """User-history read; Agent context uses the narrower current-scope read."""
        with self._operation(context) as (connection, _):
            return [self._message(row) for row in connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid", (context.session_id,))]

    def get_conversation(self, context: RequestContext):
        from app.chat_service import read_conversation
        return read_conversation(self, context)

    def read_material_page(self, context: RequestContext, material_id: str, page_number: int):
        from app.page_service import read_page_image
        return read_page_image(self, context, material_id, page_number)

    @staticmethod
    def _material(row):
        return {"materialId": row["attachment_id"], "sessionId": row["session_id"], "name": row["name"],
                "role": row["role"], "parseStatus": row["parse_status"], "useStatus": row["use_status"],
                "mediaType": row["media_type"], "originalOrder": row["original_order"],
                "parseReport": json.loads(row["parse_report_json"]) if "parse_report_json" in row.keys() and row["parse_report_json"] else None,
                "recognitionReport": json.loads(row["recognition_report_json"]) if "recognition_report_json" in row.keys() and row["recognition_report_json"] else None}

    def save_material(self, context: RequestContext, *, name: str, media_type: str, original_data: bytes,
                      role="unknown", parse_status="pending", use_status="pending"):
        self._user_path(context)
        _text(name, "文件名称")
        _text(media_type, "文件类型")
        if not isinstance(original_data, bytes) or not original_data:
            raise RepositoryError("invalid_input", "原始材料必须包含非空二进制内容。")
        _choice(role, {"resume", "jd", "supplement", "unknown"}, "材料角色")
        _choice(parse_status, {"pending", "parsed", "partial", "failed"}, "解析状态")
        _choice(use_status, {"pending", "active", "history"}, "材料用途")
        if use_status == "active" and (role == "unknown" or parse_status not in {"parsed", "partial"}):
            raise RepositoryError("invalid_input", "归属不明或尚不可读取的材料不能作为有效材料。")
        with self._operation(context, write=True) as (connection, session):
            material_id = _id("material")
            order = connection.execute("SELECT coalesce(max(original_order), -1) + 1 FROM attachments WHERE session_id = ?", (context.session_id,)).fetchone()[0]
            connection.execute("INSERT INTO attachments(attachment_id, session_id, name, media_type, content_hash, original_data, original_order, role, parse_status, use_status, resume_generation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (material_id, context.session_id, name, media_type, hashlib.sha256(original_data).hexdigest(), original_data, order, role, parse_status, use_status, session["current_resume_generation"]))
            self._touch(connection, context.session_id)
            return self._material(connection.execute("SELECT * FROM attachments WHERE attachment_id = ? AND session_id = ?", (material_id, context.session_id)).fetchone())

    def read_material_file(self, context: RequestContext, material_id: str):
        """Trusted user path for original bytes; not exposed to model tools."""
        self._user_path(context)
        _text(material_id, "材料编号")
        with self._operation(context) as (connection, _):
            row = connection.execute("SELECT * FROM attachments WHERE session_id = ? AND attachment_id = ?", (context.session_id, material_id)).fetchone()
            if row is None:
                raise RepositoryError("source_not_found", "没有找到请求的材料。")
            return {**self._material(row), "originalData": row["original_data"]}

    @staticmethod
    def _source(row):
        return {"sourceId": row["source_id"], "sessionId": row["session_id"], "sourceRevision": row["source_revision"],
                "role": row["role"], "origin": json.loads(row["origin_json"]), "text": row["text"]}

    @staticmethod
    def _find_source(connection, session_id, source_id, revision=None):
        _text(source_id, "来源编号")
        if revision is None:
            row = connection.execute("SELECT * FROM source_blocks WHERE session_id = ? AND source_id = ? ORDER BY source_revision DESC LIMIT 1", (session_id, source_id)).fetchone()
        else:
            _integer(revision, "来源版本")
            row = connection.execute("SELECT * FROM source_blocks WHERE session_id = ? AND source_id = ? AND source_revision = ?", (session_id, source_id, revision)).fetchone()
        if row is None:
            # Only an existence bit is checked outside scope, never foreign text.
            exists = connection.execute("SELECT 1 FROM source_blocks WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
            own = connection.execute("SELECT 1 FROM source_blocks WHERE source_id = ? AND session_id = ? LIMIT 1", (source_id, session_id)).fetchone()
            if exists and not own:
                raise RepositoryError("scope_violation", "请求的来源不属于当前会话。", False)
            raise RepositoryError("source_not_found", "没有找到请求的来源。")
        return row

    @staticmethod
    def _require_active(connection, row):
        active = connection.execute(
            f"SELECT 1 FROM source_blocks b JOIN sessions s ON s.session_id = b.session_id WHERE b.session_id = ? AND b.source_id = ? AND b.source_revision = ? AND {_ACTIVE_SOURCE}",
            (row["session_id"], row["source_id"], row["source_revision"]),
        ).fetchone()
        if active is None:
            raise RepositoryError("stale_state", "来源待澄清、已过期或不适用于当前任务。")

    def save_source(self, context: RequestContext, *, text: str, role: str, origin: dict,
                    use_status="pending", source_id=None, expected_source_revision=None):
        self._user_path(context)
        _text(text, "来源原文")
        _choice(role, {"resume", "jd", "supplement", "unknown"}, "来源角色")
        _choice(use_status, {"pending", "active", "history"}, "来源用途")
        _keys(origin, {"kind"}, {"materialId", "messageId", "page", "paragraph", "lineStart", "lineEnd", "part", "table", "row", "column", "recognitionVersion", "renderVersion"})
        kind = _choice(origin["kind"], {"material", "message"}, "来源类型")
        origin_key = "materialId" if kind == "material" else "messageId"
        _text(origin.get(origin_key), "原始材料编号")
        if (kind == "material" and "messageId" in origin) or (kind == "message" and "materialId" in origin):
            raise RepositoryError("invalid_input", "来源只能指向一份材料或一条消息。")
        if "part" in origin:
            _text(origin["part"], "文档部分")
        for key in ('recognitionVersion','renderVersion'):
            if key in origin:
                _text(origin[key], key)
        for key in ("page", "paragraph", "lineStart", "lineEnd", "table", "row", "column"):
            if key in origin:
                _integer(origin[key], key)
        if ("lineStart" in origin) != ("lineEnd" in origin) or origin.get("lineEnd", 1) < origin.get("lineStart", 1):
            raise RepositoryError("invalid_input", "来源行号范围无效。")
        with self._operation(context, write=True) as (connection, session):
            if kind == "material":
                parent = connection.execute("SELECT * FROM attachments WHERE session_id = ? AND attachment_id = ?", (context.session_id, origin[origin_key])).fetchone()
            else:
                parent = connection.execute("SELECT * FROM messages WHERE session_id = ? AND message_id = ? AND role = 'user'", (context.session_id, origin[origin_key])).fetchone()
            if parent is None:
                raise RepositoryError("source_not_found", "原始材料或用户消息不存在于当前会话。")
            if use_status == "active":
                if role == "unknown" or parent["use_status"] != "active" or parent["resume_generation"] != session["current_resume_generation"]:
                    raise RepositoryError("stale_state", "归属不明或历史材料不能自动转为当前事实。")
                if kind == "material" and (parent["parse_status"] not in {"parsed", "partial"} or parent["role"] != role):
                    raise RepositoryError("invalid_input", "来源角色与材料分类或解析状态不一致。")
            if source_id is None:
                if expected_source_revision is not None:
                    raise RepositoryError("invalid_input", "新来源不能指定旧版本。")
                source_id, revision = _id("source"), 1
            else:
                previous = self._find_source(connection, context.session_id, source_id)
                if _integer(expected_source_revision, "预期来源版本") != previous["source_revision"]:
                    raise RepositoryError("stale_state", "来源已经被修正，请重新读取。")
                revision = previous["source_revision"] + 1
                connection.execute("UPDATE source_blocks SET use_status = 'history' WHERE session_id = ? AND source_id = ?", (context.session_id, source_id))
                connection.execute("UPDATE suggestions SET status = 'invalidated' WHERE session_id = ? AND status = 'pending' AND suggestion_id IN (SELECT suggestion_id FROM suggestion_sources WHERE session_id = ? AND source_id = ?)",
                                   (context.session_id, context.session_id, source_id))
            connection.execute("INSERT INTO source_blocks(source_id, source_revision, session_id, attachment_id, message_id, role, text, origin_json, page_number, line_start, line_end, use_status, resume_generation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (source_id, revision, context.session_id, origin.get("materialId"), origin.get("messageId"), role, text,
                                json.dumps(origin, ensure_ascii=False), origin.get("page"), origin.get("lineStart"), origin.get("lineEnd"), use_status, session["current_resume_generation"]))
            self._touch(connection, context.session_id)
            return self._source(self._find_source(connection, context.session_id, source_id))

    def read_material(self, context: RequestContext, source_ids: list[str]):
        if not isinstance(source_ids, list) or not source_ids:
            raise RepositoryError("invalid_input", "请提供非空来源编号列表。")
        for source_id in source_ids:
            _text(source_id, "来源编号")
        with self._operation(context) as (connection, _):
            rows = [self._find_source(connection, context.session_id, source_id) for source_id in dict.fromkeys(source_ids)]
            for row in rows:
                self._require_active(connection, row)
            return [self._source(row) for row in rows]

    def search_evidence(self, context: RequestContext, query: str, limit: int = 5):
        if not isinstance(query, str) or not query.strip():
            raise RepositoryError("invalid_input", "查询关键词不能为空。")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise RepositoryError("invalid_input", "返回条数必须是 1 到 10 的整数。")
        with self._operation(context) as (connection, _):
            rows = connection.execute(
                f"SELECT b.* FROM source_blocks b JOIN sessions s ON s.session_id = b.session_id WHERE b.session_id = ? AND b.role IN ('resume', 'supplement') AND {_ACTIVE_SOURCE} AND instr(b.text, ?) > 0 ORDER BY b.source_id COLLATE BINARY LIMIT ?",
                (context.session_id, query.strip(), limit),
            )
            return [self._source(row) for row in rows]

    def read_source_snapshot(self, context: RequestContext, source_id: str, revision: int):
        """User-only historical evidence card; not a current Agent retrieval."""
        self._user_path(context)
        with self._operation(context) as (connection, _):
            row = self._find_source(connection, context.session_id, source_id, revision)
            try:
                self._require_active(connection, row)
                historical = False
            except RepositoryError:
                historical = True
            return {"source": self._source(row), "historical": historical}

    def _refs(self, connection, session_id, references, *, required=False, personal=False):
        if not isinstance(references, list) or (required and not references):
            raise RepositoryError("invalid_input", "请提供有效的来源引用列表。")
        normalized, seen = [], {}
        for reference in references:
            _keys(reference, {"sourceId", "sourceRevision", "quote"})
            row = self._find_source(connection, session_id, reference["sourceId"], reference["sourceRevision"])
            self._require_active(connection, row)
            quote = _text(reference["quote"], "引用原文")
            if quote not in row["text"]:
                raise RepositoryError("invalid_input", "引用文字与对应来源原文不符。")
            if personal and row["role"] not in {"resume", "supplement"}:
                raise RepositoryError("invalid_input", "个人经历改写必须引用简历或用户补充。")
            key = (row["source_id"], row["source_revision"])
            if key in seen:
                if seen[key] != quote:
                    raise RepositoryError("invalid_input", "同一来源版本请使用一个完整引用片段。")
                continue
            seen[key] = quote
            normalized.append({"sourceId": row["source_id"], "sourceRevision": row["source_revision"], "quote": quote})
        return normalized

    @staticmethod
    def _draft(connection, session):
        from app.draft_actions import join_markdown
        blocks = []
        for row in connection.execute("SELECT * FROM draft_blocks WHERE session_id = ? AND is_current = 1 AND resume_generation = ? ORDER BY position",
                                      (session["session_id"], session["current_resume_generation"])):
            refs = [{"sourceId": ref["source_id"], "sourceRevision": ref["source_revision"], "quote": ref["quote"]}
                    for ref in connection.execute("SELECT * FROM draft_sources WHERE session_id = ? AND block_id = ? AND block_revision = ? ORDER BY source_id", (session["session_id"], row["block_id"], row["block_revision"]))]
            blocks.append({"blockId": row["block_id"], "blockRevision": row["block_revision"], "markdown": row["markdown"], "sourceRefs": refs})
        return {"sessionId": session["session_id"], "draftRevision": session["draft_revision"], "blocks": blocks, "markdown": join_markdown(blocks)}

    def get_draft(self, context: RequestContext):
        with self._operation(context) as (connection, session):
            return self._draft(connection, session)

    def has_draft_history(self, context: RequestContext):
        """Clearing current text must not turn the next upload into first import."""
        with self._operation(context) as (connection, _):
            return connection.execute("SELECT 1 FROM draft_blocks WHERE session_id = ? LIMIT 1", (context.session_id,)).fetchone() is not None

    def apply_suggestion_decision(self, context: RequestContext, suggestion_id: str, payload: dict):
        from app.draft_actions import apply_decision
        return apply_decision(self, context, suggestion_id, payload)

    def update_draft(self, context: RequestContext, markdown: str):
        from app.draft_actions import update_draft
        return update_draft(self, context, markdown)

    def replace_draft(self, context: RequestContext, payload: dict):
        from app.draft_actions import replace_draft
        return replace_draft(self, context, payload)

    def create_initial_draft(self, context: RequestContext, blocks: list[dict]):
        """Trusted initial import only; cannot overwrite an existing draft."""
        self._user_path(context)
        if not isinstance(blocks, list) or not blocks:
            raise RepositoryError("invalid_input", "初始稿必须包含正文片段。")
        with self._operation(context, write=True) as (connection, session):
            if connection.execute("SELECT 1 FROM draft_blocks WHERE session_id = ? LIMIT 1", (context.session_id,)).fetchone():
                raise RepositoryError("stale_state", "当前稿已经存在，不能重复导入覆盖。")
            prepared = []
            for block in blocks:
                _keys(block, {"markdown", "sourceRefs"})
                prepared.append((_text(block["markdown"], "正文"), self._refs(connection, context.session_id, block["sourceRefs"], required=True, personal=True)))
            for position, (markdown, refs) in enumerate(prepared):
                block_id = _id("block")
                connection.execute("INSERT INTO draft_blocks(block_id, block_revision, session_id, position, markdown, resume_generation, write_kind) VALUES (?, 1, ?, ?, ?, ?, 'import')",
                                   (block_id, context.session_id, position, markdown, session["current_resume_generation"]))
                for ref in refs:
                    connection.execute("INSERT INTO draft_sources VALUES (?, ?, 1, ?, ?, ?)", (context.session_id, block_id, ref["sourceId"], ref["sourceRevision"], ref["quote"]))
            connection.execute("UPDATE sessions SET draft_revision = draft_revision + 1 WHERE session_id = ?", (context.session_id,))
            self._touch(connection, context.session_id)
            current = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (context.session_id,)).fetchone()
            return self._draft(connection, current)

    @staticmethod
    def _current_block(connection, session, block_id, revision=None):
        _text(block_id, "目标段落编号")
        row = connection.execute("SELECT * FROM draft_blocks WHERE session_id = ? AND block_id = ? AND is_current = 1 AND resume_generation = ?",
                                 (session["session_id"], block_id, session["current_resume_generation"])).fetchone()
        if row is None:
            raise RepositoryError("stale_state", "目标段落不属于当前稿或已经失效。")
        if revision is not None and _integer(revision, "目标段落版本") != row["block_revision"]:
            raise RepositoryError("stale_state", "目标段落已经更新。")
        return row

    @staticmethod
    def _suggestion(connection, row):
        refs = [{"sourceId": ref["source_id"], "sourceRevision": ref["source_revision"], "quote": ref["quote"]}
                for ref in connection.execute("SELECT * FROM suggestion_sources WHERE session_id = ? AND suggestion_id = ? ORDER BY source_id", (row["session_id"], row["suggestion_id"]))]
        result = {"suggestionId": row["suggestion_id"], "sessionId": row["session_id"], "operation": row["operation"],
                "target": {"blockId": row["target_block_id"], "expectedBlockRevision": row["expected_block_revision"]},
                "before": row["before_text"], "after": row["after_text"], "reason": row["reason"],
                "sourceRefs": refs, "gaps": json.loads(row["gaps_json"]), "status": row["status"]}
        decision = connection.execute("SELECT decision, feedback, created_at FROM suggestion_decisions WHERE session_id = ? AND suggestion_id = ?",
                                      (row["session_id"], row["suggestion_id"])).fetchone()
        if decision is not None:
            result["decision"] = {"decision": decision["decision"], "feedback": decision["feedback"], "createdAt": decision["created_at"]}
        if row["supersedes_suggestion_id"] is not None:
            result["supersedesSuggestionId"] = row["supersedes_suggestion_id"]
        return result

    def save_suggestion(self, context: RequestContext, proposal: dict):
        _keys(proposal, {"operation", "target", "before", "after", "reason", "sourceRefs", "gaps"}, {"supersedesSuggestionId"})
        _keys(proposal["target"], {"blockId", "expectedBlockRevision"})
        _integer(proposal["target"]["expectedBlockRevision"], "目标段落版本")
        operation = _choice(proposal["operation"], {"replace", "insert_after", "delete"}, "建议操作")
        _text(proposal["before"], "原文", empty=True)
        _text(proposal["after"], "改文", empty=operation == "delete")
        _text(proposal["reason"], "修改理由")
        if operation == "delete" and proposal["after"] != "":
            raise RepositoryError("invalid_input", "删除建议的改文必须为空。")
        if not isinstance(proposal["gaps"], list) or any(not isinstance(gap, str) or not gap.strip() for gap in proposal["gaps"]):
            raise RepositoryError("invalid_input", "信息缺口必须是文字列表。")
        with self._operation(context, write=True) as (connection, session):
            block = self._current_block(connection, session, proposal["target"]["blockId"], proposal["target"]["expectedBlockRevision"])
            supersedes = proposal.get("supersedesSuggestionId")
            if supersedes is not None:
                _text(supersedes, "原建议编号")
                original = connection.execute("SELECT 1 FROM suggestions s JOIN suggestion_decisions d ON d.suggestion_id = s.suggestion_id AND d.session_id = s.session_id WHERE s.session_id = ? AND s.suggestion_id = ? AND s.resume_generation = ? AND s.status = 'invalidated' AND d.decision = 'revise'",
                                              (context.session_id, supersedes, session["current_resume_generation"])).fetchone()
                if original is None:
                    raise RepositoryError("stale_state", "只能关联当前简历中用户要求调整的建议。")
            if proposal["before"] != block["markdown"]:
                raise RepositoryError("stale_state", "建议原文与当前段落不一致。")
            refs = self._refs(connection, context.session_id, proposal["sourceRefs"], required=True)
            if not any(self._find_source(connection, context.session_id, ref["sourceId"], ref["sourceRevision"])["role"] in {"resume", "supplement"} for ref in refs):
                raise RepositoryError("invalid_input", "岗位要求不能单独作为个人经历的改写依据。")
            suggestion_id = _id("suggestion")
            connection.execute("INSERT INTO suggestions(suggestion_id, session_id, operation, target_block_id, expected_block_revision, expected_state_revision, before_text, after_text, reason, gaps_json, resume_generation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (suggestion_id, context.session_id, operation, block["block_id"], block["block_revision"], session["state_revision"], proposal["before"], proposal["after"], proposal["reason"], json.dumps(proposal["gaps"], ensure_ascii=False), session["current_resume_generation"]))
            if supersedes is not None:
                connection.execute("UPDATE suggestions SET supersedes_suggestion_id = ? WHERE suggestion_id = ? AND session_id = ?", (supersedes, suggestion_id, context.session_id))
            for ref in refs:
                connection.execute("INSERT INTO suggestion_sources VALUES (?, ?, ?, ?, ?)", (context.session_id, suggestion_id, ref["sourceId"], ref["sourceRevision"], ref["quote"]))
            return self._suggestion(connection, connection.execute("SELECT * FROM suggestions WHERE suggestion_id = ? AND session_id = ?", (suggestion_id, context.session_id)).fetchone())

    def get_suggestion(self, context: RequestContext, suggestion_id: str):
        self._user_path(context)
        _text(suggestion_id, "建议编号")
        with self._operation(context) as (connection, _):
            row = connection.execute("SELECT * FROM suggestions WHERE session_id = ? AND suggestion_id = ?", (context.session_id, suggestion_id)).fetchone()
            if row is None:
                raise RepositoryError("source_not_found", "没有找到请求的建议。")
            return self._suggestion(connection, row)

    @staticmethod
    def _issue(connection, row):
        refs = [{"sourceId": ref["source_id"], "sourceRevision": ref["source_revision"], "quote": ref["quote"]}
                for ref in connection.execute("SELECT * FROM issue_sources WHERE session_id = ? AND issue_id = ? ORDER BY source_id", (row["session_id"], row["issue_id"]))]
        blocks = [ref[0] for ref in connection.execute("SELECT block_id FROM issue_blocks WHERE session_id = ? AND issue_id = ? ORDER BY block_id", (row["session_id"], row["issue_id"]))]
        return {"issueId": row["issue_id"], "sessionId": row["session_id"], "kind": row["kind"], "summary": row["summary"],
                "sourceRefs": refs, "affectedBlockIds": blocks, "status": row["status"]}

    def save_issues(self, context: RequestContext, issues: list[dict]):
        if not isinstance(issues, list):
            raise RepositoryError("invalid_input", "问题必须是列表。")
        with self._operation(context, write=True) as (connection, session):
            result = []
            for issue in issues:
                _keys(issue, {"kind", "summary", "sourceRefs", "affectedBlockIds"})
                _text(issue["kind"], "问题类型")
                _text(issue["summary"], "问题说明")
                refs = self._refs(connection, context.session_id, issue["sourceRefs"])
                if not isinstance(issue["affectedBlockIds"], list):
                    raise RepositoryError("invalid_input", "受影响段落必须是列表。")
                for block_id in issue["affectedBlockIds"]:
                    _text(block_id, "段落编号")
                blocks = [self._current_block(connection, session, block_id) for block_id in dict.fromkeys(issue["affectedBlockIds"])]
                issue_id = _id("issue")
                connection.execute("INSERT INTO issues(issue_id, session_id, kind, summary, resume_generation) VALUES (?, ?, ?, ?, ?)", (issue_id, context.session_id, issue["kind"], issue["summary"], session["current_resume_generation"]))
                for ref in refs:
                    connection.execute("INSERT INTO issue_sources VALUES (?, ?, ?, ?, ?)", (context.session_id, issue_id, ref["sourceId"], ref["sourceRevision"], ref["quote"]))
                for block in blocks:
                    connection.execute("INSERT INTO issue_blocks VALUES (?, ?, ?, ?)", (context.session_id, issue_id, block["block_id"], block["block_revision"]))
                result.append(self._issue(connection, connection.execute("SELECT * FROM issues WHERE issue_id = ? AND session_id = ?", (issue_id, context.session_id)).fetchone()))
            return result

    def get_session_context(self, context: RequestContext):
        with self._operation(context) as (connection, session):
            sources = [self._source(row) for row in connection.execute(
                f"SELECT b.* FROM source_blocks b JOIN sessions s ON s.session_id = b.session_id WHERE b.session_id = ? AND {_ACTIVE_SOURCE} ORDER BY b.source_id",
                (context.session_id,))]
            messages = [self._message(row) for row in connection.execute("SELECT * FROM messages WHERE session_id = ? AND use_status = 'active' AND resume_generation = ? ORDER BY created_at, rowid", (context.session_id, session["current_resume_generation"]))]
            # Pending material metadata can be classified later; no raw bytes or
            # unresolved text is included in Agent factual memory.
            materials = [self._material(row) for row in connection.execute("SELECT * FROM attachments WHERE session_id = ? AND (resume_generation = ? OR (role = 'jd' AND use_status = 'active')) ORDER BY coalesce(reading_order,original_order),original_order", (context.session_id, session["current_resume_generation"]))]
            suggestions = []
            # Accepted rows let the agent remember which experiences are done after
            # a draft edit invalidates revision-scoped state; pending rows come first.
            for row in connection.execute("SELECT * FROM suggestions WHERE session_id = ? AND status IN ('pending', 'accepted') AND resume_generation = ? ORDER BY status = 'accepted', created_at, suggestion_id", (context.session_id, session["current_resume_generation"])):
                proposal = self._suggestion(connection, row)
                if row["status"] == "pending":
                    try:
                        block = self._current_block(connection, session, row["target_block_id"], row["expected_block_revision"])
                        if block["markdown"] != row["before_text"]:
                            continue
                        self._refs(connection, context.session_id, proposal["sourceRefs"], required=True)
                    except RepositoryError:
                        continue
                suggestions.append(proposal)
            issues = []
            for row in connection.execute("SELECT * FROM issues WHERE session_id = ? AND is_current = 1 AND status = 'open' AND resume_generation = ? ORDER BY created_at, rowid", (context.session_id, session["current_resume_generation"])):
                issue = self._issue(connection, row)
                try:
                    self._refs(connection, context.session_id, issue["sourceRefs"])
                    for block_id in issue["affectedBlockIds"]:
                        self._current_block(connection, session, block_id)
                except RepositoryError:
                    continue
                issues.append(issue)
            feedback = [self._suggestion(connection, row) for row in connection.execute(
                "SELECT s.* FROM suggestions s JOIN suggestion_decisions d ON d.session_id = s.session_id AND d.suggestion_id = s.suggestion_id WHERE s.session_id = ? AND s.resume_generation = ? AND d.decision IN ('reject', 'revise') ORDER BY d.created_at, s.rowid",
                (context.session_id, session["current_resume_generation"]))]
            return {"session": self._session(session), "messages": messages, "materials": materials, "sources": sources,
                    "draft": self._draft(connection, session), "suggestions": suggestions, "issues": issues, "decisionFeedback": feedback}

    def get_session_detail(self, context: RequestContext):
        """User restoration includes history; never use as Agent factual memory."""
        self._user_path(context)
        with self._operation(context) as (connection, session):
            messages = [self._message(row) for row in connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid", (context.session_id,))]
            materials = [self._material(row) for row in connection.execute(
                "SELECT * FROM attachments WHERE session_id = ? ORDER BY coalesce(reading_order,original_order),original_order", (context.session_id,))]
            suggestions = [self._suggestion(connection, row) for row in connection.execute(
                "SELECT * FROM suggestions WHERE session_id = ? ORDER BY created_at, rowid", (context.session_id,))]
            issues = [{**self._issue(connection, row), "isCurrent": bool(row["is_current"])} for row in connection.execute(
                "SELECT * FROM issues WHERE session_id = ? ORDER BY created_at, rowid", (context.session_id,))]
            events = [{"runId": row["run_id"], "sequence": row["sequence"], "type": row["type"], "mode": row["mode"], "payload": json.loads(row["payload_json"])}
                      for row in connection.execute("SELECT e.* FROM run_events e JOIN runs r ON r.session_id = e.session_id AND r.run_id = e.run_id WHERE e.session_id = ? AND r.resume_generation = ? ORDER BY r.generation, e.sequence",
                                                    (context.session_id, session["current_resume_generation"]))]
            return {"session": self._session(session), "messages": messages, "materials": materials,
                    "draft": self._draft(connection, session), "suggestions": suggestions, "issues": issues, "events": events}

    def get_material_detail(self, context: RequestContext, material_id: str):
        self._user_path(context)
        _text(material_id, "材料编号")
        with self._operation(context) as (connection, _):
            row = connection.execute("SELECT * FROM attachments WHERE session_id = ? AND attachment_id = ?",
                                     (context.session_id, material_id)).fetchone()
            if row is None:
                raise RepositoryError("source_not_found", "没有找到请求的材料。")
            sources = [self._source(source) for source in connection.execute(
                "SELECT * FROM source_blocks WHERE session_id = ? AND attachment_id = ? "
                "ORDER BY coalesce(page_number, json_extract(origin_json, '$.paragraph'), line_start, 0), source_id, source_revision", (context.session_id, material_id))]
            return {"material": self._material(row), "sources": sources}

    def delete_session(self, context: RequestContext):
        self._user_path(context)
        with self._operation(context, write=True) as (connection, _):
            self._touch(connection, context.session_id)
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (context.session_id,))
            return {"sessionId": context.session_id, "deleted": True}
