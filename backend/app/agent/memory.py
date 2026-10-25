from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.ports import (
    Issue,
    Phase,
    SessionContext,
    SourceRef,
    Suggestion,
)
from app.agent.store import AgentStateStore

Slot = Literal[
    "responsibility", "context", "action", "result",
    "effect", "evidence", "tech", "confirm",
]
FactStatus = Literal["active", "superseded"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Fact(BaseModel):
    id: str
    slot: Slot
    statement: str
    status: FactStatus = "active"
    origin: str = "answer"
    source_refs: list[SourceRef] = Field(default_factory=list)
    superseded_by: str | None = None
    message_id: str | None = None
    created_at: str = ""


class Question(BaseModel):
    id: str
    slot: Slot | None = None
    text: str
    fingerprint: str
    round: int = 0
    outcome: Literal["asked", "answered", "declined"] = "asked"
    answer_message_id: str | None = None
    asked_at: str = ""
    answered_at: str | None = None


class ExperienceDoc(BaseModel):
    experience_id: str
    seq: int
    kind: Literal["project", "work"]
    title: str = ""
    role: str | None = None
    period: str | None = None
    raw_text: str = ""
    source_ids: list[str] = Field(default_factory=list)
    block_id: str | None = None
    block_revision: int = 0
    skipped: bool = False
    archived: bool = False
    round: int = 0
    facts: list[Fact] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    declined: list[Slot] = Field(default_factory=list)
    last_suggestion_id: str | None = None
    last_run_outcome: str | None = None
    no_fact_streak: int = 0
    accepted_at: str | None = None
    summary: str = ""
    updated_at: str = ""


class SessionDoc(BaseModel):
    schema_version: int = 1
    direction: str = ""
    goal: str = ""
    goal_asked: bool = False
    awaiting_goal: bool = False
    primary_material_id: str | None = None
    jd_keywords: list[str] = Field(default_factory=list)
    experience_order: list[str] = Field(default_factory=list)
    current_focus_id: str | None = None
    first_diagnosis: dict[str, Any] | None = None
    open_issue_ids: list[str] = Field(default_factory=list)
    run_index: dict[str, str] = Field(default_factory=dict)
    pending_clarification: dict[str, Any] | None = None
    summary: str = ""
    auto_advance: bool = False
    updated_at: str = ""


class StepTrace(BaseModel):
    seq: int
    action: Literal["tool", "ask_user", "finish"]
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any] | None = None
    rationale: str = ""
    tokens: int = 0
    cost_cents: int = 0


class RunDoc(BaseModel):
    run_id: str
    request_id: str
    generation: int = 0
    state_revision: int = 0
    mode: Literal["live", "mock"] = "live"
    trigger: str = "user_message"
    action: str | None = None
    focus_experience_id: str | None = None
    outcome: str | None = None
    fallback_used: bool = False
    steps: list[StepTrace] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cents: int = 0
    started_at: str = ""
    ended_at: str | None = None


def load_experience(
    store: AgentStateStore, session_id: str, experience_id: str
) -> tuple[ExperienceDoc, int] | None:
    stored = store.get(session_id, "experience", experience_id)
    if stored is None:
        return None
    return ExperienceDoc.model_validate(stored.doc), stored.revision


def save_experience(
    store: AgentStateStore,
    session_id: str,
    doc: ExperienceDoc,
    expected_revision: int | None,
) -> int:
    doc.updated_at = now_iso()
    return store.put(
        session_id, "experience", doc.experience_id, doc.model_dump(mode="json"), expected_revision
    )


def load_session(
    store: AgentStateStore, session_id: str
) -> tuple[SessionDoc, int] | None:
    stored = store.get(session_id, "session", session_id)
    if stored is None:
        return None
    return SessionDoc.model_validate(stored.doc), stored.revision


def save_session(
    store: AgentStateStore,
    session_id: str,
    doc: SessionDoc,
    expected_revision: int | None,
) -> int:
    doc.updated_at = now_iso()
    return store.put(
        session_id, "session", session_id, doc.model_dump(mode="json"), expected_revision
    )


def effect_state(doc: ExperienceDoc) -> Literal["confirmed", "followup", "absent", "missing"]:
    if any(
        fact.slot == "effect" and fact.status == "active" for fact in doc.facts
    ):
        return "confirmed"
    settled = [
        question
        for question in doc.questions
        if question.slot == "effect"
        and question.outcome in ("answered", "declined")
    ]
    if len(settled) >= 2:
        return "absent"
    if len(settled) == 1:
        return "followup"
    return "missing"


def derive_phase(doc: ExperienceDoc, sug: Suggestion | None) -> Phase:
    if doc.skipped:
        return "skipped"
    if sug is not None and sug.status == "accepted":
        return "confirmed"
    if sug is not None and sug.status == "pending":
        return "awaiting_confirm"
    if doc.last_suggestion_id and sug is not None and sug.status == "invalidated":
        return "drafting"
    if doc.round == 0 and not doc.facts:
        return "pending"
    if doc.last_run_outcome == "limited":
        return "limited"
    return "interviewing"


@dataclass
class ReconcileResult:
    phases: dict[str, Phase] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    missing_blocks: list[str] = field(default_factory=list)


def reconcile(
    session_id: str,
    ctx: SessionContext,
    store: AgentStateStore,
    *,
    now: str | None = None,
) -> ReconcileResult:
    now = now or now_iso()
    suggestions = {s.suggestion_id: s for s in ctx.suggestions}
    block_ids = {block.block_id for block in ctx.draft_blocks}
    result = ReconcileResult()

    for experience_id in ctx.session.experience_order:
        loaded = load_experience(store, session_id, experience_id)
        if loaded is None:
            continue
        doc, revision = loaded
        sug = (
            suggestions.get(doc.last_suggestion_id)
            if doc.last_suggestion_id
            else None
        )
        phase = derive_phase(doc, sug)

        if doc.block_id and doc.block_id not in block_ids:
            phase = "pending"
            result.missing_blocks.append(experience_id)
            result.issues.append(
                Issue(
                    kind="missing_block",
                    summary=f"Draft 中找不到 {doc.block_id}，该段需要重新确认",
                    experience_id=experience_id,
                )
            )

        if sug is not None and sug.status == "accepted" and doc.accepted_at is None:
            doc.accepted_at = now
            save_experience(store, session_id, doc, revision)

        result.phases[experience_id] = phase

    return result


class NewFact(BaseModel):
    slot: Slot
    statement: str
    source_refs: list[SourceRef] = Field(default_factory=list)
    message_id: str | None = None
    supersedes_fact_ids: list[str] = Field(default_factory=list)


_FILLER_PREFIXES = ("我觉得", "我认为", "我感觉", "大概", "应该是", "可能", "其实")
_NEGATION_MARKERS = ("没", "不", "未", "只", "仅", "并非", "没有")


def normalize_statement(text: str) -> str:
    value = re.sub(r"\s+", "", text.strip())
    for prefix in _FILLER_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value.rstrip("。！？!?，,；;")


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _next_fact_id(doc: ExperienceDoc) -> str:
    used = {fact.id for fact in doc.facts}
    index = 1
    while f"f{index}" in used:
        index += 1
    return f"f{index}"


def merge_facts(
    doc: ExperienceDoc, new_facts: list[NewFact], message_id: str | None = None
) -> list[Fact]:
    added: list[Fact] = []
    for incoming in new_facts:
        if incoming.slot == "confirm":
            raise ValueError("confirm is not a fact slot")
        statement = normalize_statement(incoming.statement)
        if not statement:
            continue

        active = [fact for fact in doc.facts if fact.status == "active"]
        if not incoming.supersedes_fact_ids and any(
            fact.slot == incoming.slot and _similar(fact.statement, statement) >= 0.9
            for fact in active
        ):
            continue

        # Facts sharing a slot need not contradict each other. Replacement
        # IDs are checked against an explicit correction in the trusted tool.
        supersedes = [
            fact
            for fact in active
            if fact.slot == incoming.slot
            and fact.id in incoming.supersedes_fact_ids
        ]

        fact = Fact(
            id=_next_fact_id(doc),
            slot=incoming.slot,
            statement=statement,
            source_refs=incoming.source_refs,
            message_id=incoming.message_id or message_id,
            created_at=now_iso(),
        )
        for old in supersedes:
            old.status = "superseded"
            old.superseded_by = fact.id
        doc.facts.append(fact)
        added.append(fact)
    return added


_QUESTION_NOISE = ("请问", "那么", "所以", "呢", "吗", "吧", "啊", "呀")


def normalize_question(text: str) -> str:
    # Compare the actual question, so optional guidance cannot hide a repeat.
    text = re.split(r'[？?]', text, maxsplit=1)[0]
    value = re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()]+", "", text)
    for word in _QUESTION_NOISE:
        value = value.replace(word, "")
    return value


def question_fingerprint(text: str) -> str:
    digest = hashlib.sha1(normalize_question(text).encode("utf-8")).hexdigest()
    return f"sha1:{digest}"


def question_similarity(a: str, b: str) -> float:
    return _similar(normalize_question(a), normalize_question(b))


def record_question(
    doc: ExperienceDoc, text: str, slot: Slot | None = None
) -> Question:
    question = Question(
        id=f"q{len(doc.questions) + 1}",
        slot=slot,
        text=text,
        fingerprint=question_fingerprint(text),
        round=doc.round + 1,
        asked_at=now_iso(),
    )
    doc.questions.append(question)
    return question


def record_answer(
    doc: ExperienceDoc,
    question_id: str,
    message_id: str | None = None,
    *,
    declined: bool = False,
) -> Question:
    for question in doc.questions:
        if question.id != question_id:
            continue
        question.outcome = "declined" if declined else "answered"
        question.answer_message_id = message_id
        question.answered_at = now_iso()
        if declined and question.slot and question.slot != "confirm":
            if question.slot not in doc.declined:
                doc.declined.append(question.slot)
        _recompute_round(doc)
        return question
    raise KeyError(f"unknown question {question_id}")


def record_declined(doc: ExperienceDoc, slots: list[Slot]) -> None:
    for slot in slots:
        if slot != "confirm" and slot not in doc.declined:
            doc.declined.append(slot)


def _recompute_round(doc: ExperienceDoc) -> None:
    answered = [
        question
        for question in doc.questions
        if question.outcome in ("answered", "declined") and question.slot != "confirm"
    ]
    doc.round = max(0, len(answered) - 1)


_HEADER_LINE = re.compile(r"^(项目|工作|实习|教育|技能|校园|实践|获奖)经历\s*\d*\s*$")
_DATE_PART = re.compile(r"^[\d.\-/年月日—–~～\s]+$")


def _block_title(markdown: str) -> str:
    from app.resume_structure import ENTRY_DATE, heading
    for line in markdown.splitlines():
        text = heading(line).lstrip("-* ").strip()
        if not text or _HEADER_LINE.match(text):
            continue
        # Tabular PDFs put the date first, padded with spaces: drop it before naming.
        columns = [part.strip() for part in re.split(r"\s{2,}", text) if part.strip()]
        if len(columns) > 1 and ENTRY_DATE.match(columns[0]):
            text = " ".join(columns[1:])
        parts = [part.strip() for part in re.split(r"[｜|]", text) if part.strip()]
        parts = [part for part in parts if not _DATE_PART.match(part)] or parts
        return (parts[0] if parts else text)[:20]
    return "未命名经历"


def sync_experiences(
    session_id: str,
    ctx: SessionContext,
    store: AgentStateStore,
    doc: SessionDoc | None,
    revision: int | None,
) -> tuple[SessionDoc, int]:
    if doc is None:
        doc = SessionDoc(current_focus_id=ctx.session.current_focus_id)
        revision = None
    from app.resume_structure import block_kinds
    kinds = block_kinds([block.markdown for block in ctx.draft_blocks])
    order = list(doc.experience_order)
    changed = False
    for block, kind in zip(ctx.draft_blocks, kinds):
        if kind is None:
            # Frozen sections are parsed and editable but never interviewed.
            if block.block_id in order:
                order.remove(block.block_id)
                changed = True
            continue
        existing = load_experience(store, session_id, block.block_id)
        if existing is None:
            save_experience(
                store,
                session_id,
                ExperienceDoc(
                    experience_id=block.block_id,
                    seq=len(order) + 1,
                    kind=kind,
                    title=_block_title(block.markdown),
                    raw_text=block.markdown,
                    source_ids=[ref.source_id for ref in block.source_refs],
                    block_id=block.block_id,
                    block_revision=block.block_revision,
                ),
                None,
            )
        elif existing[0].kind != kind:
            existing[0].kind = kind
            save_experience(store, session_id, existing[0], existing[1])
        if block.block_id not in order:
            order.append(block.block_id)
            changed = True
    if changed:
        doc.experience_order = order
    if doc.current_focus_id not in order:
        doc.current_focus_id = order[0] if order else None
        changed = True
    if changed or revision is None:
        revision = save_session(store, session_id, doc, revision)
    return doc, revision
