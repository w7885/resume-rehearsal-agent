from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from pydantic import BaseModel

from app.agent.memory import (
    ExperienceDoc,
    derive_phase,
    load_experience,
    merge_facts,
    save_experience,
)
from app.agent.ports import RepoPort, RepoError, verify_proposal_snapshot
from app.agent.ports import SourceRef
from app.proposal_snapshot import ProposalSnapshot
from app.agent.schemas import (
    GetExperienceMemoryArgs,
    ListExperiencesArgs,
    ProposeChangeArgs,
    ReadMaterialArgs,
    RecordIssueArgs,
    SaveFactsArgs,
    SearchEvidenceArgs,
    ToolName,
    ToolStep,
)
from app.agent.store import AgentStateStore


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    description: str
    args_model: type[BaseModel]
    hint: str


TOOL_SPECS: dict[ToolName, ToolSpec] = {
    "read_material": ToolSpec(
        "read_material",
        "按字符位置补读本会话来源原文，每份每次最多2000字符。优先一次读一个来源；有后文时按next_offset继续，引用须使用返回的原句和来源版本。",
        ReadMaterialArgs,
        '{"source_ids": ["src_1"], "offset": 0, "limit": 2000}',
    ),
    "search_evidence": ToolSpec(
        "search_evidence",
        "按关键词检索本会话的来源。",
        SearchEvidenceArgs,
        '{"query": "8条反馈", "limit": 5}',
    ),
    "list_experiences": ToolSpec(
        "list_experiences",
        "列出本会话所有经历及其阶段。",
        ListExperiencesArgs,
        "{}",
    ),
    "get_experience_memory": ToolSpec(
        "get_experience_memory",
        "读取某段的已确认事实、已问问题与 declined 维度。",
        GetExperienceMemoryArgs,
        '{"experience_id": "exp_01"}',
    ),
    "save_facts": ToolSpec(
        "save_facts",
        "把用户明确说过且有个人原句支持的事实写入记忆，默认追加去重。slot只能为responsibility、context、action、result、effect、evidence、tech、details、strength、preference；方法用tech或action，背景用context，交付状态用result，确认不是事实。明确更正时用supersedes_fact_ids指定同维度旧事实。",
        SaveFactsArgs,
        '{"experience_id": "exp_01", "facts": [{"slot": "action", "statement": "合并 8 条反馈为 5 项需求"}, {"slot": "effect", "statement": "老师录入时间从半小时缩短到五分钟"}]}',
    ),
    "record_issues": ToolSpec(
        "record_issues",
        "记录缺口或待补信息，不改正文。",
        RecordIssueArgs,
        '{"issues": [{"kind": "gap", "summary": "未体现 API 集成", "experience_id": "block-project-001"}]}',
    ),
    "propose_change": ToolSpec(
        "propose_change",
        "提交一条待用户确认的整块改写建议。before可省略，由系统绑定当前目标原文快照；after仍需完整、引用仍须准确。",
        ProposeChangeArgs,
        '{"operation": "replace", "target": "block-project-001", "after": "整段改写后的完整文本", "reason": "按已有事实整理表达", "source_refs": [{"source_id": "source-supplement-001", "quote": "8 条同学反馈"}], "gaps": []}',
    ),
}


def tool_spec_text() -> str:
    return "\n".join(
        f"- `{spec.name}`：{spec.description} 参数：`{spec.hint}`"
        for spec in TOOL_SPECS.values()
    )


@dataclass
class ToolContext:
    session_id: str
    repo: RepoPort
    store: AgentStateStore
    message_id: str | None = None
    run_id: str | None = None
    proposal_snapshot: ProposalSnapshot | None = None


def _require_experience(ctx: ToolContext, experience_id: str) -> tuple[ExperienceDoc, int]:
    session = ctx.repo.get_session_context(ctx.session_id).session
    if experience_id not in session.experience_order:
        raise RepoError(
            "scope_violation", f"{experience_id} is not in this session"
        )
    loaded = load_experience(ctx.store, ctx.session_id, experience_id)
    if loaded is None:
        raise RepoError("invalid_input", f"no memory for {experience_id}")
    return loaded


async def _read_material(args: ReadMaterialArgs, ctx: ToolContext) -> dict[str, Any]:
    sources = ctx.repo.read_material(ctx.session_id, args.source_ids)
    pages = []
    for source in sources:
        total = len(source.text)
        start = min(args.offset, total)
        end = min(start + args.limit, total)
        pages.append({
            "source_id": source.source_id,
            "kind": source.kind,
            "source_revision": source.source_revision,
            "offset": start,
            "total_chars": total,
            "has_more": end < total,
            "next_offset": end if end < total else None,
            "text": source.text[start:end],
        })
    return {"sources": pages}


async def _search_evidence(
    args: SearchEvidenceArgs, ctx: ToolContext
) -> dict[str, Any]:
    hits = ctx.repo.search_evidence(ctx.session_id, args.query, args.limit)
    return {
        "hits": [
            {"source_id": hit.source_id, "quote": hit.quote, "score": hit.score}
            for hit in hits
        ]
    }


async def _list_experiences(
    _args: ListExperiencesArgs, ctx: ToolContext
) -> dict[str, Any]:
    session_ctx = ctx.repo.get_session_context(ctx.session_id)
    suggestions = {s.suggestion_id: s for s in session_ctx.suggestions}
    rows = []
    for experience_id in session_ctx.session.experience_order:
        loaded = load_experience(ctx.store, ctx.session_id, experience_id)
        if loaded is None:
            continue
        doc, _ = loaded
        sug = suggestions.get(doc.last_suggestion_id) if doc.last_suggestion_id else None
        rows.append(
            {
                "experience_id": doc.experience_id,
                "seq": doc.seq,
                "kind": doc.kind,
                "title": doc.title,
                "phase": derive_phase(doc, sug),
                "round": doc.round,
            }
        )
    return {"experiences": sorted(rows, key=lambda row: row["seq"])}


async def _get_experience_memory(
    args: GetExperienceMemoryArgs, ctx: ToolContext
) -> dict[str, Any]:
    doc, _ = _require_experience(ctx, args.experience_id)
    return {
        "experience_id": doc.experience_id,
        "seq": doc.seq,
        "kind": doc.kind,
        "title": doc.title,
        "role": doc.role,
        "period": doc.period,
        "round": doc.round,
        "facts": [
            {"id": f.id, "slot": f.slot, "statement": f.statement}
            for f in doc.facts
            if f.status == "active"
        ],
        "questions": [
            {
                "id": q.id,
                "slot": q.slot,
                "text": q.text,
                "outcome": q.outcome,
            }
            for q in doc.questions
        ],
        "declined": doc.declined,
        "summary": doc.summary,
    }


async def _save_facts(args: SaveFactsArgs, ctx: ToolContext) -> dict[str, Any]:
    doc, revision = _require_experience(ctx, args.experience_id)
    current = ctx.repo.get_session_context(ctx.session_id)
    message = next((m for m in current.messages if m.message_id == ctx.message_id and m.role == 'user'), None)
    correction = bool(message and re.search(r'更正|纠正|说错|不对|不是.{0,40}而是|应为|应改为', message.text))
    active = {f.id: f for f in doc.facts if f.status == 'active'}
    for fact in args.facts:
        if fact.supersedes_fact_ids and (not correction or any(
                fid not in active or active[fid].slot != fact.slot for fid in fact.supersedes_fact_ids)):
            raise RepoError('invalid_input', '替代事实必须对应用户明确纠正的同一维度事实编号。')
        # Link the actual saved answer, never model-generated memory, to its
        # admitted primary source. Uncertain answers have no admitted source.
        if message and fact.message_id in (None, ctx.message_id):
            matches = [s for s in current.sources if s.kind == 'supplement' and s.text == message.text]
            if matches:
                fact.source_refs = [SourceRef(source_id=s.source_id, source_revision=s.source_revision, quote=s.text) for s in matches[:1]]
    added = merge_facts(doc, args.facts, ctx.message_id)
    if added:
        save_experience(ctx.store, ctx.session_id, doc, revision)
    return {
        "added": [fact.id for fact in added],
        "active_facts": len([f for f in doc.facts if f.status == "active"]),
    }


async def _record_issues(args: RecordIssueArgs, ctx: ToolContext) -> dict[str, Any]:
    ids = ctx.repo.save_issues(ctx.session_id, args.issues)
    return {"issue_ids": ids}


async def _propose_change(
    args: ProposeChangeArgs, ctx: ToolContext
) -> dict[str, Any]:
    snapshot = ctx.proposal_snapshot
    verify_proposal_snapshot(snapshot, ctx.repo.get_session_context(ctx.session_id),
                             ctx.session_id, args.target, run_id=ctx.run_id)
    if args.before is not None and args.before.strip() != snapshot.before.strip():
        raise RepoError('stale_anchor', '建议旧文与已固定的输入快照不一致。', False)
    doc, revision = _require_experience(ctx, args.target)
    suggestion = ctx.repo.save_suggestion(
        session_id=ctx.session_id,
        experience_id=args.target,
        operation=args.operation,
        before=snapshot.before,
        after=args.after,
        reason=args.reason,
        source_refs=args.source_refs,
        gaps=args.gaps,
        snapshot=snapshot,
    )
    doc.last_suggestion_id = suggestion.suggestion_id
    doc.last_run_outcome = "waiting_user"
    save_experience(ctx.store, ctx.session_id, doc, revision)
    return {
        "suggestion_id": suggestion.suggestion_id,
        "status": suggestion.status,
        "after": suggestion.after,
        "input_snapshot_id": snapshot.fingerprint,
        "input_block_revision": snapshot.block_revision,
        "input_before_sha256": snapshot.before_sha256,
    }


_EXECUTORS = {
    "read_material": _read_material,
    "search_evidence": _search_evidence,
    "list_experiences": _list_experiences,
    "get_experience_memory": _get_experience_memory,
    "save_facts": _save_facts,
    "record_issues": _record_issues,
    "propose_change": _propose_change,
}


def parse_args(step: ToolStep) -> BaseModel:
    spec = TOOL_SPECS.get(step.tool)
    if spec is None:
        raise RepoError("scope_violation", f"tool {step.tool} is not allowed")
    return spec.args_model.model_validate(step.args)


async def execute_tool(step: ToolStep, ctx: ToolContext) -> dict[str, Any]:
    args = parse_args(step)
    return await _EXECUTORS[step.tool](args, ctx)
