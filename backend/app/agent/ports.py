from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from app.proposal_snapshot import ProposalSnapshot, SnapshotMismatch

Scope = Literal["session", "experience", "run", "budget"]
Phase = Literal[
    "pending",
    "interviewing",
    "drafting",
    "awaiting_confirm",
    "confirmed",
    "skipped",
    "limited",
]
SuggestionStatus = Literal["accepted", "pending", "rejected", "invalidated"]


class RepoError(Exception):
    def __init__(self, code: str, message: str, recoverable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable


class Message(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    text: str
    created_at: str | None = None


class SessionInfo(BaseModel):
    session_id: str
    direction: str = ""
    direction_confirmed: bool = False
    jd_keywords: list[str] = Field(default_factory=list)
    primary_material_id: str | None = None
    experience_order: list[str] = Field(default_factory=list)
    current_focus_id: str | None = None
    pending_clarification: dict[str, Any] | None = None
    summary: str = ""
    state_revision: int = 1
    draft_revision: int = 1
    current_resume_generation: int = 1


class SourceRef(BaseModel):
    source_id: str
    quote: str
    source_revision: int = 0


class DraftBlock(BaseModel):
    block_id: str
    experience_id: str | None = None
    block_revision: int = 1
    markdown: str = ""
    source_refs: list[SourceRef] = Field(default_factory=list)


class Suggestion(BaseModel):
    suggestion_id: str
    experience_id: str
    status: SuggestionStatus
    operation: str = "replace"
    before: str | None = None
    after: str = ""
    reason: str = ""
    source_refs: list[SourceRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    created_at: str | None = None
    block_id: str | None = None
    expected_block_revision: int | None = None


class Issue(BaseModel):
    issue_id: str | None = None
    kind: str
    summary: str
    experience_id: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)
    affected_block_ids: list[str] = Field(default_factory=list)


class SourceText(BaseModel):
    source_id: str
    kind: Literal["resume", "jd", "supplement", "other"] = "supplement"
    text: str
    source_revision: int = 1


class EvidenceHit(BaseModel):
    source_id: str
    quote: str
    score: float = 0.0


class SessionContext(BaseModel):
    session: SessionInfo
    messages: list[Message] = Field(default_factory=list)
    draft_blocks: list[DraftBlock] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    sources: list[SourceText] = Field(default_factory=list)
    revision_feedback: list[dict[str, Any]] = Field(default_factory=list)


def capture_proposal_snapshot(ctx: SessionContext, target_id: str, *, block_id=None, run_id=None):
    if target_id not in ctx.session.experience_order:
        raise RepoError('scope_violation', '目标不属于本次生成范围。', False)
    matches = [b for b in ctx.draft_blocks if
               (b.block_id == block_id if block_id is not None else
                b.block_id == target_id or b.experience_id == target_id)]
    if len(matches) != 1:
        return None
    block = matches[0]
    if block.block_id != target_id and block.experience_id != target_id:
        raise RepoError('scope_violation', '目标段落与输入快照不属于同一经历。', False)
    return ProposalSnapshot(
        session_id=ctx.session.session_id, target_id=target_id, block_id=block.block_id,
        state_revision=ctx.session.state_revision, draft_revision=ctx.session.draft_revision,
        resume_generation=ctx.session.current_resume_generation,
        block_revision=block.block_revision, before=block.markdown,
        source_versions=tuple(sorted((s.source_id, s.source_revision, s.kind) for s in ctx.sources)),
        run_id=run_id,
    )


def verify_proposal_snapshot(snapshot, ctx: SessionContext, session_id, target_id, *, run_id=None):
    if not isinstance(snapshot, ProposalSnapshot):
        raise RepoError('missing_snapshot', '缺少可信的生成输入快照，不能保存建议。', False)
    if snapshot.session_id != session_id or snapshot.target_id != target_id:
        raise RepoError('scope_violation', '建议目标与生成输入快照不一致。', False)
    matches = [b for b in ctx.draft_blocks if b.block_id == snapshot.block_id]
    if len(matches) != 1:
        raise RepoError('stale_state', '快照目标已不在当前稿中。', False)
    block = matches[0]
    if block.block_id != target_id and block.experience_id != target_id:
        raise RepoError('scope_violation', '当前段落与快照经历归属不一致。', False)
    try:
        snapshot.verify(
            session_id=ctx.session.session_id, block_id=block.block_id,
            state_revision=ctx.session.state_revision, draft_revision=ctx.session.draft_revision,
            resume_generation=ctx.session.current_resume_generation,
            block_revision=block.block_revision, before=block.markdown,
            source_versions=tuple((s.source_id, s.source_revision, s.kind) for s in ctx.sources),
            run_id=run_id,
        )
    except SnapshotMismatch as error:
        raise RepoError(error.code, str(error), False) from error
    return block


class RepoPort(Protocol):
    def get_session_context(self, session_id: str) -> SessionContext: ...

    def search_evidence(
        self, session_id: str, query: str, limit: int = 5
    ) -> list[EvidenceHit]: ...

    def read_material(
        self, session_id: str, source_ids: list[str]
    ) -> list[SourceText]: ...

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
        *, snapshot: ProposalSnapshot | None = None,
    ) -> Suggestion: ...

    def save_issues(self, session_id: str, issues: list[Issue]) -> list[str]: ...

    def apply_suggestion_decision(
        self,
        suggestion_id: str,
        decision: Literal["accept", "reject"],
        via: Literal["button", "message"],
        message_id: str | None = None,
    ) -> None: ...
