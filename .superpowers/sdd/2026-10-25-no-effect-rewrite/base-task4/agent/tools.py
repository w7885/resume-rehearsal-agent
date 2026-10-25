from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.agent.memory import (
    ExperienceDoc,
    derive_phase,
    load_experience,
    merge_facts,
    save_experience,
)
from app.agent.ports import RepoPort, RepoError
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
        "读取本会话来源的原文。",
        ReadMaterialArgs,
        '{"source_ids": ["src_1"]}',
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
        "把用户刚说的事实写入记忆；会自动去重与覆盖旧说法。",
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
        "提交一条待用户确认的整块改写建议。",
        ProposeChangeArgs,
        '{"operation": "replace", "target": "block-project-001", "before": "当前整段原文", "after": "整段改写后的完整文本", "reason": "补充了量化结果", "source_refs": [{"source_id": "source-supplement-001", "quote": "8 条同学反馈"}], "gaps": []}',
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
    return {
        "sources": [
            {
                "source_id": source.source_id,
                "kind": source.kind,
                "text": source.text[:2000],
            }
            for source in sources
        ]
    }


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
    doc, revision = _require_experience(ctx, args.target)
    suggestion = ctx.repo.save_suggestion(
        session_id=ctx.session_id,
        experience_id=args.target,
        operation=args.operation,
        before=args.before,
        after=args.after,
        reason=args.reason,
        source_refs=args.source_refs,
        gaps=args.gaps,
    )
    doc.last_suggestion_id = suggestion.suggestion_id
    doc.last_run_outcome = "waiting_user"
    save_experience(ctx.store, ctx.session_id, doc, revision)
    return {
        "suggestion_id": suggestion.suggestion_id,
        "status": suggestion.status,
        "after": suggestion.after,
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
