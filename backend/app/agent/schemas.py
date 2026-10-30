from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, StrictBool

from app.agent.memory import NewFact, Slot
from app.agent.ports import Issue, Phase, SourceRef
from app.resume_structure import SectionKind

ToolName = Literal[
    "read_material",
    "search_evidence",
    "list_experiences",
    "get_experience_memory",
    "save_facts",
    "record_issues",
    "propose_change",
]


class ReadMaterialArgs(BaseModel):
    source_ids: list[str] = Field(min_length=1)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=2000, ge=1, le=2000, strict=True)


class SearchEvidenceArgs(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=10)


class ListExperiencesArgs(BaseModel):
    pass


class GetExperienceMemoryArgs(BaseModel):
    experience_id: str


class SaveFactsArgs(BaseModel):
    experience_id: str
    facts: list[NewFact] = Field(min_length=1)


class RecordIssueArgs(BaseModel):
    issues: list[Issue] = Field(min_length=1)


class ProposeChangeArgs(BaseModel):
    operation: Literal["replace", "insert_after", "insert_before"] = "replace"
    target: str
    before: str | None = None
    after: str
    reason: str = ""
    source_refs: list[SourceRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class ToolStep(BaseModel):
    action: Literal["tool"]
    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=40)


class AskStep(BaseModel):
    action: Literal["ask_user"]
    message: str
    slot: Slot | None = None
    related_source_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(default="", max_length=40)


class FinishStep(BaseModel):
    action: Literal["finish"]
    outcome: Literal["completed", "limited"]
    summary: str = ""
    rationale: str = Field(default="", max_length=40)


StepDecision = Annotated[
    ToolStep | AskStep | FinishStep, Field(discriminator="action")
]
StepDecisionAdapter = TypeAdapter(StepDecision)


class CandidateBrief(BaseModel):
    experience_id: str
    seq: int
    kind: SectionKind
    title: str
    role: str | None = None
    phase: Phase


class Clarification(BaseModel):
    code: Literal[
        "out_of_range",
        "type_empty",
        "ambiguous_relative",
        "ambiguous_title",
        "multiple_match",
        "no_experience",
    ]
    question: str = Field(max_length=60)
    quoted_reference: str = Field(max_length=12)
    candidates: list[CandidateBrief] = Field(default_factory=list, max_length=5)
    allow_free_text: bool = True


class RouteDecision(BaseModel):
    action: Literal[
        "answer",
        "correct",
        "skip",
        "confirm",
        "refuse",
        "focus",
        "global_edit",
        "meta",
        "chitchat",
    ]
    target_experience_id: str | None = None
    scope: Literal["experience", "whole_resume", "jd", "unknown"] = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    clarification: Clarification | None = None
    rationale: str = Field(default="", max_length=40)
    answers_pending: StrictBool = False
    answer_quote: str | None = Field(default=None, max_length=4000)
    reply: str | None = Field(default=None, max_length=600)


IntentName = Literal[
    "answer",
    "correct",
    "skip",
    "refuse",
    "focus",
    "global_edit",
    "clarify",
    "meta",
    "chitchat",
    "cancel",
]
DispositionName = Literal["suppress", "pause", "resolve", "resume"]


class SubIntent(BaseModel):
    disposition: DispositionName | None = None
    intent: IntentName | None = None
    quote: str | None = Field(default=None, max_length=4000)
    target_experience_id: str | None = None


class PendingResolution(BaseModel):
    decision: Literal["accept", "reject", "revise"] | None = None
    suggestion_id: str | None = None
    feedback: str | None = Field(default=None, max_length=4000)


class RouterOutput(BaseModel):
    disposition: DispositionName | None = None
    intent: IntentName | None = None
    suppress_kind: Literal["thanks", "ack"] | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    quote: str | None = Field(default=None, max_length=4000)
    target_experience_id: str | None = None
    scope: Literal["experience", "whole_resume", "jd", "unknown"] | None = None
    clarification: Clarification | None = None
    answers_pending: StrictBool = False
    answer_quote: str | None = Field(default=None, max_length=4000)
    reply: str | None = Field(default=None, max_length=600)
    pending_resolution: PendingResolution | None = None
    mixed: list[SubIntent] = Field(default_factory=list)
    meta_note: str | None = Field(default=None, max_length=200)


class SegmentSeed(BaseModel):
    kind: SectionKind
    title: str
    role: str | None = None
    period: str | None = None
    raw_text: str


class SegmentResult(BaseModel):
    experiences: list[SegmentSeed] = Field(default_factory=list)
    jd_keywords: list[str] = Field(default_factory=list)
