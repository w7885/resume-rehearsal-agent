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
from app.resume_structure import SectionKind, section_kinds

Slot = Literal[
    "responsibility", "context", "action", "result",
    "effect", "evidence", "tech", "confirm",
    "details", "strength", "preference",
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
    active: bool = True
    resolution_reason: str | None = None
    # Kept separately from outcome: uncertainty allows one recall prompt, while
    # an explicit absence or refusal must stop that interview dimension.
    answer_kind: Literal["legacy", "answered", "absent", "uncertain", "refused"] = "legacy"
    answer_message_id: str | None = None
    answer_quote: str | None = None
    asked_at: str = ""
    answered_at: str | None = None


class ExperienceDoc(BaseModel):
    experience_id: str
    seq: int
    kind: SectionKind
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
    supplement_quotes: dict[str, str] = Field(default_factory=dict)


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


def _last_effect_denial(doc: ExperienceDoc) -> Question | None:
    return next((q for q in reversed(doc.questions)
                 if q.slot == 'effect' and q.outcome != 'asked'
                 and q.answer_kind == 'absent'), None)


def _fact_after_stop(fact: Fact, question: Question) -> bool:
    # Unknown historical timestamps cannot silently revive a denied outcome.
    if not fact.created_at or not question.answered_at or not fact.source_refs:
        return False
    try:
        return (datetime.fromisoformat(fact.created_at.replace('Z', '+00:00'))
                > datetime.fromisoformat(question.answered_at.replace('Z', '+00:00')))
    except (ValueError, TypeError):
        return False


def supported_experience_slots(
    doc: ExperienceDoc, *, source_texts: dict[str, str] | None = None,
    source_kinds: dict[str, str] | None = None,
) -> set[str]:
    """Conservative hints from original wording, never unbacked model memory.

    This is not a semantic truth checker. Supplement quotes must be located in
    an admitted personal source before they can suppress a question.
    """
    stop = _last_effect_denial(doc)
    texts = [(doc.raw_text, stop is None)]
    for fact in doc.facts:
        if fact.status != 'active':
            continue
        for ref in fact.source_refs:
            source = (source_texts or {}).get(ref.source_id)
            if ((source_kinds or {}).get(ref.source_id) in {'resume', 'supplement'}
                    and source is not None and ref.quote and ref.quote in source):
                texts.append((ref.quote, stop is None or _fact_after_stop(fact, stop)))
    slots: set[str] = set()
    for text, allow_effect in texts:
        for clause in re.split(r'[。；;\n]', text):
            if re.search(r'如果|假如|假设|可能|不确定|记不清|尚未|未曾|没有|没做|并未|不是|未参与|未负责', clause):
                continue
            collective_only = (re.search(r'团队|小组|项目组|我们|全组', clause)
                               and not re.search(r'本人|我(?!们)|独立(?:负责|完成|实现|开发)', clause))
            if not collective_only and re.search(r'(?:我|本人)?(?:独立)?负责\s*[^，,。；;\s]{2,}|(?:我|本人)独立(?:完成|实现|开发)', clause):
                slots.add('responsibility')
            if re.search(r'(?:实现|完成|整理|编写|接入|排查|增加|归纳|合并|设计|开发|测试)\s*[^，,。；;\s]{2,}', clause):
                slots.add('action')
            if (allow_effect and not re.search(r'目标|目的|旨在|希望|计划|预计|预期|有望|以便|用于|为了|以期|试图|尝试|期望|(?:以|为|来)(?:提升|提高|缩短|减少|降低|节省|避免|改善)', clause)
                    and re.search(r'(?:提升|提高|缩短|减少|降低|节省|避免|替代|改善)\s*[^，,。；;\s]{2,}', clause)):
                slots.add('effect')
    return slots


def effect_state(doc: ExperienceDoc) -> Literal["confirmed", "followup", "absent", "missing"]:
    stop = _last_effect_denial(doc)
    if stop is not None:
        if any(f.slot == 'effect' and f.status == 'active' and _fact_after_stop(f, stop)
               for f in doc.facts):
            return 'confirmed'
        return 'absent'
    if any(
        fact.slot == "effect" and fact.status == "active" for fact in doc.facts
    ):
        return "confirmed"
    if 'effect' in supported_experience_slots(doc):
        return 'confirmed'
    settled = [
        question
        for question in doc.questions
        if question.slot == "effect"
        and question.outcome in ("answered", "declined")
    ]
    if any(question.answer_kind in {'absent', 'refused'} for question in settled):
        return 'absent'
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
    answer_kind: Literal["legacy", "answered", "absent", "uncertain", "refused"] = "legacy",
    answer_quote: str | None = None,
) -> Question:
    for question in doc.questions:
        if question.id != question_id:
            continue
        question.outcome = "declined" if declined else "answered"
        question.answer_kind = answer_kind
        question.answer_message_id = message_id
        question.answer_quote = answer_quote
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
    kinds = section_kinds([block.markdown for block in ctx.draft_blocks])
    order = []
    changed = False
    for block, kind in zip(ctx.draft_blocks, kinds):
        if kind is None:
            continue
        order.append(block.block_id)
        from app.agent.section_policy import section_title
        title = _block_title(block.markdown) if kind in {'project', 'work'} else section_title(block.markdown, kind)
        existing = load_experience(store, session_id, block.block_id)
        if existing is None:
            save_experience(
                store,
                session_id,
                ExperienceDoc(
                    experience_id=block.block_id,
                    seq=len(order),
                    kind=kind,
                    title=title,
                    raw_text=block.markdown,
                    source_ids=[ref.source_id for ref in block.source_refs],
                    block_id=block.block_id,
                    block_revision=block.block_revision,
                ),
                None,
            )
        elif existing[0].kind != kind or existing[0].seq != len(order):
            if existing[0].kind in {'project', 'work'} and kind not in {'project', 'work'}:
                for question in existing[0].questions:
                    if question.outcome == 'asked' and question.slot in {'responsibility', 'action', 'effect', 'result', 'tech'}:
                        question.active = False
                        question.resolution_reason = '章节重新诊断为信息段，原经历采访不再适用；保留原记录。'
            existing[0].kind = kind
            existing[0].seq = len(order)
            existing[0].title = title
            save_experience(store, session_id, existing[0], existing[1])
    if order != doc.experience_order:
        doc.experience_order = order
        changed = True
    if doc.current_focus_id not in order:
        doc.current_focus_id = order[0] if order else None
        changed = True
    if changed or revision is None:
        revision = save_session(store, session_id, doc, revision)
    return doc, revision
