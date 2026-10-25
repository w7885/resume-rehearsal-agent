from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

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
    ) -> Suggestion: ...

    def save_issues(self, session_id: str, issues: list[Issue]) -> list[str]: ...

    def apply_suggestion_decision(
        self,
        suggestion_id: str,
        decision: Literal["accept", "reject"],
        via: Literal["button", "message"],
        message_id: str | None = None,
    ) -> None: ...
