from __future__ import annotations

from typing import Any, Mapping

from app.agent.ports import (
    DraftBlock,
    EvidenceHit,
    Issue,
    Message,
    RepoError,
    SessionContext,
    SessionInfo,
    SourceRef,
    SourceText,
    Suggestion,
)

_KIND = {"resume": "resume", "jd": "jd", "supplement": "supplement"}
_FINISH_STATUS: dict[str, tuple[str, str | None]] = {
    "completed": ("completed", None),
    "waiting_user": ("waiting_user", None),
    "clarify": ("waiting_user", None),
    "focused": ("waiting_user", None),
    "limited": ("budget_blocked", "limited"),
    "cancelled": ("interrupted", "cancelled"),
    "dropped": ("completed", None),
    "skipped": ("completed", None),
    "no_focus": ("completed", None),
}


def _unwrap(reply: Any) -> Any:
    if not isinstance(reply, Mapping) or "ok" not in reply:
        raise RepoError("storage_failed", "仓库返回了无法识别的结果。", True)
    if reply["ok"]:
        return reply["data"]
    error = reply.get("error") or {}
    raise RepoError(
        str(error.get("code") or "storage_failed"),
        str(error.get("message") or "仓库调用失败。"),
        bool(error.get("recoverable", True)),
    )


def _ref(row: Mapping[str, Any]) -> SourceRef:
    return SourceRef(
        source_id=row["sourceId"],
        quote=row.get("quote", ""),
        source_revision=int(row.get("sourceRevision") or 0),
    )


def _suggestion(row: Mapping[str, Any]) -> Suggestion:
    target = row.get("target") or {}
    return Suggestion(
        suggestion_id=row["suggestionId"],
        experience_id=target.get("blockId", ""),
        block_id=target.get("blockId"),
        expected_block_revision=target.get("expectedBlockRevision"),
        status=row["status"],
        operation=row.get("operation", "replace"),
        before=row.get("before"),
        after=row.get("after", ""),
        reason=row.get("reason", ""),
        source_refs=[_ref(ref) for ref in row.get("sourceRefs", [])],
        gaps=list(row.get("gaps") or []),
    )


def _issue(row: Mapping[str, Any]) -> Issue:
    affected = list(row.get("affectedBlockIds") or [])
    return Issue(
        issue_id=row.get("issueId"),
        kind=row["kind"],
        summary=row["summary"],
        experience_id=affected[0] if affected else None,
        source_refs=[_ref(ref) for ref in row.get("sourceRefs", [])],
        affected_block_ids=affected,
    )


class ARepoAdapter:
    def __init__(
        self,
        *,
        tools: Any,
        context: Any,
        repository: Any = None,
        user_context: Any = None,
        session_id: str | None = None,
    ) -> None:
        self.tools = tools
        self.context = context
        self.repository = repository
        self.user_context = user_context
        self.session_id = session_id or getattr(context, "session_id", None)

    def _check(self, session_id: str) -> None:
        if self.session_id is not None and session_id != self.session_id:
            raise RepoError("scope_violation", "会话上下文不匹配。", False)

    def get_session_context(self, session_id: str) -> SessionContext:
        self._check(session_id)
        data = _unwrap(self.tools.get_session_context())
        session = data["session"]
        messages = [
            Message(
                message_id=row["messageId"],
                role=row["role"],
                text=row.get("content", ""),
                created_at=row.get("createdAt"),
            )
            for row in data.get("messages", [])
        ]
        blocks = [
            DraftBlock(
                block_id=row["blockId"],
                experience_id=row["blockId"],
                block_revision=int(row.get("blockRevision") or 1),
                markdown=row.get("markdown", ""),
                source_refs=[_ref(ref) for ref in row.get("sourceRefs", [])],
            )
            for row in data.get("draft", {}).get("blocks", [])
        ]
        source_rows = list(data.get('sources', []))
        jd_rows = sorted((row for row in source_rows if row.get('role') == 'jd'), key=lambda row: (
            (row.get('origin') or {}).get('materialId', ''),
            (row.get('origin') or {}).get('page', 1),
            (row.get('origin') or {}).get('paragraph', (row.get('origin') or {}).get('lineStart', 1)),
            (row.get('origin') or {}).get('table', 0),
            (row.get('origin') or {}).get('row', 0),
            (row.get('origin') or {}).get('column', 0)))
        source_rows = [row for row in source_rows if row.get('role') != 'jd'] + jd_rows
        sources = [
            SourceText(
                source_id=row["sourceId"],
                kind=_KIND.get(row.get("role"), "other"),
                text=row.get("text", ""),
                source_revision=int(row.get("sourceRevision") or 1),
            )
            for row in source_rows
        ]
        return SessionContext(
            session=SessionInfo(
                session_id=session["sessionId"],
                direction=session.get("direction", ""),
                direction_confirmed=session.get("directionStatus") == "provided",
                experience_order=[block.block_id for block in blocks],
                current_focus_id=blocks[0].block_id if blocks else None,
                state_revision=int(session.get("stateRevision") or 1),
                draft_revision=int(session.get("draftRevision") or 1),
                current_resume_generation=int(session.get("currentResumeGeneration") or 1),
            ),
            messages=messages,
            draft_blocks=blocks,
            suggestions=[_suggestion(row) for row in data.get("suggestions", [])],
            issues=[_issue(row) for row in data.get("issues", [])],
            sources=sources,
        )

    def search_evidence(
        self, session_id: str, query: str, limit: int = 5
    ) -> list[EvidenceHit]:
        self._check(session_id)
        rows = _unwrap(
            self.tools.dispatch("search_evidence", {"query": query, "limit": limit})
        )
        return [
            EvidenceHit(
                source_id=row["sourceId"],
                quote=row.get("text", "")[:160],
                score=0.0,
            )
            for row in rows
        ]

    def read_material(
        self, session_id: str, source_ids: list[str]
    ) -> list[SourceText]:
        self._check(session_id)
        rows = _unwrap(
            self.tools.dispatch("read_material", {"sourceIds": source_ids})
        )
        return [
            SourceText(
                source_id=row["sourceId"],
                kind=_KIND.get(row.get("role"), "other"),
                text=row.get("text", ""),
                source_revision=int(row.get("sourceRevision") or 1),
            )
            for row in rows
        ]

    def save_suggestion(
        self,
        session_id: str,
        experience_id: str,
        operation: str,
        before: str | None,
        after: str,
        reason: str,
        source_refs: list[SourceRef],
        gaps: list[str],
    ) -> Suggestion:
        self._check(session_id)
        ctx = self.get_session_context(session_id)
        block = next(
            (item for item in ctx.draft_blocks if item.block_id == experience_id),
            None,
        )
        if block is None:
            raise RepoError("stale_state", "目标段落不在当前稿中。", True)
        sources = {source.source_id: source for source in ctx.sources}
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for ref in source_refs:
            source = sources.get(ref.source_id)
            if source is None:
                raise RepoError("source_not_found", "引用的来源不存在。", True)
            if not ref.quote or ref.quote not in source.text:
                raise RepoError("invalid_input", "引用文字与来源原文不符。", True)
            key = (source.source_id, source.source_revision)
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "sourceId": source.source_id,
                    "sourceRevision": source.source_revision,
                    "quote": ref.quote,
                }
            )
        if not any(sources[row["sourceId"]].kind in ("resume", "supplement") for row in refs):
            raise RepoError("invalid_input", "必须引用简历或用户补充。", True)
        if not reason.strip():
            raise RepoError("invalid_input", "修改理由不能为空。", True)
        proposal = {
            "operation": operation
            if operation in {"replace", "insert_after", "delete"}
            else "replace",
            "target": {
                "blockId": block.block_id,
                "expectedBlockRevision": block.block_revision,
            },
            "before": block.markdown,
            "after": after,
            "reason": reason,
            "sourceRefs": refs,
            "gaps": [gap for gap in gaps if gap.strip()],
        }
        if operation not in {'replace', 'insert_after', 'delete'}:
            raise RepoError('invalid_input', '不支持的建议操作。', False)
        if before is not None and before.strip() != block.markdown.strip():
            raise RepoError('stale_state', '建议旧文已改变，不能套用最新正文。', True)
        for ref in source_refs:
            if ref.source_revision not in (0, sources[ref.source_id].source_revision):
                raise RepoError('stale_state', '建议引用的来源版本已改变。', True)
        return _suggestion(_unwrap(self.tools.dispatch("propose_change", proposal)))

    def save_issues(self, session_id: str, issues: list[Issue]) -> list[str]:
        self._check(session_id)
        ctx = self.get_session_context(session_id)
        valid = {block.block_id for block in ctx.draft_blocks}
        payload = []
        for issue in issues:
            candidates = issue.affected_block_ids or (
                [issue.experience_id] if issue.experience_id else []
            )
            payload.append(
                {
                    "kind": issue.kind,
                    "summary": issue.summary,
                    "sourceRefs": [
                        {
                            "sourceId": ref.source_id,
                            "sourceRevision": ref.source_revision,
                            "quote": ref.quote,
                        }
                        for ref in issue.source_refs
                        if ref.quote
                    ],
                    "affectedBlockIds": [
                        block_id for block_id in candidates if block_id in valid
                    ],
                }
            )
        rows = _unwrap(self.tools.dispatch("record_issues", {"issues": payload}))
        return [row["issueId"] for row in rows]

    def apply_suggestion_decision(
        self,
        suggestion_id: str,
        decision: str,
        via: str,
        message_id: str | None = None,
    ) -> None:
        raise RepoError('scope_violation', '决定须由受信 Host 关联真实答复后执行。', False)

    def save_assistant_message(self, text: str) -> str:
        raise RepoError('scope_violation', '回复须由受信 Host 保存询问与来源关联。', False)

    def append_event(self, event: dict[str, Any]) -> None:
        raise RepoError('scope_violation', '事件由受信 Host 持久化。', False)

    def finish_run(self, outcome: str) -> None:
        raise RepoError('scope_violation', '运行状态由受信 Host 维护。', False)

    def cancel_run(self) -> None:
        raise RepoError('scope_violation', '取消由受信 Host 维护。', False)
