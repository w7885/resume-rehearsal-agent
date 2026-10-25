from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.agent.context import assemble_context
from app.agent.events import EventSink, RunEvent
from app.agent.guard import (
    MAX_ROUNDS,
    MAX_STEPS,
    GuardState,
    call_signature,
    check,
)
from app.agent.llm import LLMError
from app.agent.memory import (
    ExperienceDoc,
    StepTrace,
    derive_phase,
    effect_state,
    load_experience,
    reconcile,
    record_question,
    save_experience,
)
from app.agent.ports import Issue, Phase, RepoError, SessionContext
from app.agent.prompts import build_decide_prompt, build_system_prompt
from app.agent.schemas import (
    AskStep,
    FinishStep,
    StepDecision,
    StepDecisionAdapter,
    ToolStep,
)
from app.agent.store import AgentStateStore
from app.agent.tools import TOOL_SPECS, ToolContext, execute_tool, tool_spec_text

_PHRASES = {
    "responsibility": "这段经历中，你本人负责哪部分",
    "context": "这段经历要解决的业务问题或目标是什么",
    "action": "这段经历中，你具体做了哪些步骤",
    "effect": "这段经历上线或交付后，带来了哪些实际变化",
    "evidence": "有没有材料能支持这段经历的描述",
    "tech": "这段经历用到了哪些工具、方法或资源",
}
_EFFECT_FOLLOWUP = "除了使用情况，实际带来了什么变化"
_SLOT_ORDER = ("responsibility", "context", "action", "effect", "evidence", "tech")
_DIRECT_REWRITE = re.compile(r"直接改|别问|不用问|你决定|随便改|开始改写")


def _normalize_action(raw) -> Any:
    if isinstance(raw, dict):
        action = raw.get("action")
        if isinstance(action, str) and action in TOOL_SPECS and "tool" not in raw:
            args = {k: v for k, v in raw.items() if k not in ("action", "rationale")}
            return {
                "action": "tool",
                "tool": action,
                "args": args,
                "rationale": raw.get("rationale", ""),
            }
    return raw


@dataclass
class LoopResult:
    outcome: str
    focus_experience_id: str | None
    next_experience_id: str | None
    fallback_used: bool
    steps: list[StepTrace]
    events: list[RunEvent]
    prompt_tokens: int
    completion_tokens: int
    cost_cents: int


def _load_docs(
    repo, store: AgentStateStore, session_id: str, ctx: SessionContext
) -> list[ExperienceDoc]:
    docs: list[ExperienceDoc] = []
    for experience_id in ctx.session.experience_order:
        loaded = load_experience(store, session_id, experience_id)
        if loaded is not None:
            docs.append(loaded[0])
    return docs


def _fallback_question(doc: ExperienceDoc) -> tuple[str | None, str | None]:
    if doc.no_fact_streak >= 2:
        return None, None
    if doc.round >= MAX_ROUNDS:
        return None, None
    effect = effect_state(doc)
    if effect == "followup":
        return f"{_EFFECT_FOLLOWUP}？", "effect"
    active_slots = {fact.slot for fact in doc.facts if fact.status == "active"}
    for slot in _SLOT_ORDER:
        if slot == "effect" and effect == "absent":
            continue
        if slot in doc.declined or slot in active_slots:
            continue
        text = f"{_PHRASES[slot]}？"
        if len(text) > 60:
            text = text[:59] + "？"
        return text, slot
    return None, None


async def run_loop(
    *,
    run_id: str,
    session_id: str,
    repo,
    store: AgentStateStore,
    llm,
    focus_experience_id: str | None = None,
    message_id: str | None = None,
    state_revision_changed: bool = False,
    sink: EventSink | None = None,
    emit_start: bool = True,
    answered_question: bool = False,
    session_goal: str = "",
    cancel_event: asyncio.Event | None = None,
) -> LoopResult:
    sink = sink or EventSink(session_id, run_id)
    ctx = repo.get_session_context(session_id)
    focus_id = focus_experience_id or ctx.session.current_focus_id
    docs = _load_docs(repo, store, session_id, ctx)
    focus_doc = next((doc for doc in docs if doc.experience_id == focus_id), None)

    if focus_doc is None:
        sink.emit("warning", code="no_focus", message="没有可改写的经历")
        sink.emit("run_finished", outcome="no_focus", fallback_used=False)
        return LoopResult(
            outcome="no_focus",
            focus_experience_id=focus_id,
            next_experience_id=None,
            fallback_used=False,
            steps=[],
            events=sink.events,
            prompt_tokens=0,
            completion_tokens=0,
            cost_cents=0,
        )

    focus_block_text = ""
    if focus_doc is not None:
        for block in ctx.draft_blocks:
            if (
                block.block_id == focus_doc.block_id
                or block.experience_id == focus_doc.experience_id
            ):
                focus_block_text = block.markdown
                break
        if not focus_block_text:
            focus_block_text = focus_doc.raw_text
    call_max_tokens = max(
        2000, min(6000, int(len(focus_block_text) / 1.2) + 800)
    )

    def active_fact_count() -> int:
        return sum(
            1
            for doc in _load_docs(repo, store, session_id, ctx)
            for fact in doc.facts
            if fact.status == "active"
        )

    facts_before = active_fact_count()

    reconciled = reconcile(session_id, ctx, store)
    if reconciled.issues:
        repo.save_issues(session_id, reconciled.issues)
    phases: dict[str, Phase] = reconciled.phases
    source_texts: dict[str, str] = {}
    source_kinds: dict[str, str] = {}
    traces: list[StepTrace] = []
    history: list[str] = []
    fallback_used = False
    total_prompt = 0
    total_completion = 0
    total_cost = 0

    system_prompt = build_system_prompt() + "\n\n" + build_decide_prompt(
        tool_spec_text()
    )
    if emit_start:
        sink.emit("run_started", focus_experience_id=focus_id)

    def refresh() -> None:
        nonlocal ctx, docs, phases, focus_doc
        ctx = repo.get_session_context(session_id)
        docs = _load_docs(repo, store, session_id, ctx)
        suggestions = {s.suggestion_id: s for s in ctx.suggestions}
        phases = {
            doc.experience_id: derive_phase(
                doc,
                suggestions.get(doc.last_suggestion_id)
                if doc.last_suggestion_id
                else None,
            )
            for doc in docs
        }
        focus_doc = next(
            (doc for doc in docs if doc.experience_id == focus_id), None
        )

    def persist(doc: ExperienceDoc) -> None:
        loaded = load_experience(store, session_id, doc.experience_id)
        save_experience(
            store, session_id, doc, loaded[1] if loaded is not None else None
        )

    def last_user_text() -> str:
        texts = [m.text for m in ctx.messages if m.role == "user"][-2:]
        return "\n".join(texts)

    def current_user_text() -> str:
        return next(
            (m.text for m in reversed(ctx.messages) if m.role == "user"), ""
        )

    def guard_state() -> GuardState:
        focus_block = ""
        if focus_doc is not None:
            for block in ctx.draft_blocks:
                if block.block_id == focus_doc.block_id or block.experience_id == focus_doc.experience_id:
                    focus_block = block.markdown
                    break
            if not focus_block:
                focus_block = focus_doc.raw_text
        return GuardState(
            session=ctx,
            focus_doc=focus_doc,
            step_count=len(traces),
            anchor_text=f"{(focus_doc.raw_text if focus_doc else '')}\n{last_user_text()}",
            other_titles=[
                doc.title
                for doc in docs
                if doc.experience_id != focus_id and doc.title
            ],
            source_texts=source_texts,
            source_kinds=source_kinds,
            focus_block_markdown=focus_block,
            state_revision_changed=state_revision_changed,
            direct_rewrite=bool(_DIRECT_REWRITE.search(current_user_text())),
            recent_calls=[
                call_signature(trace.tool or "", trace.args)
                for trace in traces
                if trace.action == "tool" and trace.tool
            ],
        )

    def load_ref_sources(decision: StepDecision) -> None:
        if not isinstance(decision, ToolStep):
            return
        ids = list(focus_doc.source_ids) if focus_doc else []
        if decision.tool == "propose_change":
            for ref in decision.args.get("source_refs") or []:
                if isinstance(ref, dict) and isinstance(ref.get("source_id"), str):
                    ids.append(ref["source_id"])
        for source_id in ids:
            if source_id in source_texts:
                continue
            try:
                sources = repo.read_material(session_id, [source_id])
            except RepoError:
                continue
            for source in sources:
                source_texts[source.source_id] = source.text
                source_kinds[source.source_id] = source.kind

    def pending_suggestion() -> bool:
        if not focus_doc or not focus_doc.last_suggestion_id:
            return False
        return any(
            suggestion.suggestion_id == focus_doc.last_suggestion_id
            and suggestion.status == "pending"
            for suggestion in ctx.suggestions
        )

    def next_experience() -> str | None:
        for doc in sorted(docs, key=lambda item: item.seq):
            if doc.experience_id == focus_id or doc.skipped or doc.archived:
                continue
            if phases.get(doc.experience_id) == "confirmed":
                continue
            return doc.experience_id
        return None

    def settle_streak() -> None:
        if not answered_question or focus_id is None:
            return
        loaded = load_experience(store, session_id, focus_id)
        if loaded is None:
            return
        doc, revision = loaded
        if active_fact_count() > facts_before:
            doc.no_fact_streak = 0
        else:
            doc.no_fact_streak += 1
        save_experience(store, session_id, doc, revision)

    def finish_result(outcome: str) -> LoopResult:
        settle_streak()
        next_id = next_experience() if outcome == "completed" else None
        unresolved = [
            issue.issue_id for issue in ctx.issues if issue.issue_id is not None
        ]
        sink.emit(
            "run_finished",
            outcome=outcome,
            fallback_used=fallback_used,
            next_experience_id=next_id,
            summary={
                "waiting_user": "建议已生成，等待确认。",
                "completed": "本段已完成。",
                "limited": "信息不足，本轮先到这里。",
                "cancelled": "本次运行已取消。",
                "no_focus": "没有可改写的经历。",
            }.get(outcome, ""),
            unresolved_issue_ids=unresolved,
        )
        return LoopResult(
            outcome=outcome,
            focus_experience_id=focus_id,
            next_experience_id=next_id,
            fallback_used=fallback_used,
            steps=traces,
            events=sink.events,
            prompt_tokens=total_prompt,
            completion_tokens=total_completion,
            cost_cents=total_cost,
        )

    def limited(code: str, message: str) -> LoopResult:
        nonlocal ctx, focus_doc
        refresh()
        if focus_doc is not None:
            focus_doc.last_run_outcome = "limited"
            persist(focus_doc)
        repo.save_issues(
            session_id,
            [Issue(kind=code, summary=message, experience_id=focus_id)],
        )
        return finish_result("limited")

    def fallback() -> LoopResult:
        nonlocal fallback_used, focus_doc
        fallback_used = True
        refresh()
        if focus_doc is None:
            return finish_result("limited")
        text, slot = _fallback_question(focus_doc)
        if text is None:
            focus_doc.last_run_outcome = "limited"
            persist(focus_doc)
            return finish_result("limited")
        question = record_question(focus_doc, text, slot)
        focus_doc.last_run_outcome = "waiting_user"
        persist(focus_doc)
        sink.emit(
            "question",
            question_id=question.id,
            text=question.text,
            slot=slot,
            related_source_ids=[],
            fallback=True,
        )
        return finish_result("waiting_user")

    async def ask_model(note: str) -> tuple[StepDecision | None, int, int, int]:
        user = assemble_context(
            ctx, docs, phases, focus_id, session_goal=session_goal
        )
        if history:
            user += "\n\n## 本轮已执行\n" + "\n".join(history)
        if note:
            user += "\n\n## 注意\n" + note
        completion = await llm.decide(
            system=system_prompt,
            user=user,
            run_id=run_id,
            max_tokens=call_max_tokens,
        )
        raw = getattr(completion, "data", completion)
        prompt_tokens = getattr(completion, "prompt_tokens", 0)
        completion_tokens = getattr(completion, "completion_tokens", 0)
        cost = getattr(completion, "cost_cents", 0)
        try:
            decision = StepDecisionAdapter.validate_python(_normalize_action(raw))
        except (ValidationError, ValueError):
            decision = None
        return decision, prompt_tokens, completion_tokens, cost

    for step_number in range(1, MAX_STEPS + 1):
        if cancel_event is not None and cancel_event.is_set():
            sink.emit("run_finished", outcome="cancelled", fallback_used=False)
            return LoopResult(
                outcome="cancelled",
                focus_experience_id=focus_id,
                next_experience_id=None,
                fallback_used=fallback_used,
                steps=traces,
                events=sink.events,
                prompt_tokens=total_prompt,
                completion_tokens=total_completion,
                cost_cents=total_cost,
            )
        decision: StepDecision | None = None
        note = ""
        step_prompt = 0
        step_completion = 0
        step_cost = 0
        gaps: list[str] = []

        for _attempt in range(2):
            try:
                decision, prompt_tokens, completion_tokens, cost = await ask_model(
                    note
                )
            except LLMError as exc:
                if exc.recoverable and _attempt == 0:
                    note = (
                        f"上一次调用失败（{exc.code}）：{exc.message[:80]}。"
                        "请只输出一个符合格式的 JSON 对象。"
                    )
                    continue
                sink.emit("warning", code=exc.code, message=exc.message)
                return limited(exc.code, exc.message)

            step_prompt += prompt_tokens
            step_completion += completion_tokens
            step_cost += cost
            total_prompt += prompt_tokens
            total_completion += completion_tokens
            total_cost += cost

            if decision is None:
                note = "上一次输出不是合法的动作 JSON，请只输出一个符合格式的 JSON 对象。"
                continue

            load_ref_sources(decision)
            result = check(decision, guard_state())
            if result.violation is None:
                gaps = result.gaps
                break

            sink.emit(
                "warning",
                code=result.violation.code,
                message=result.violation.message,
            )
            if not result.violation.retryable:
                return limited(result.violation.code, result.violation.message)
            note = (
                f"上一次动作被拦截（{result.violation.code}）："
                f"{result.violation.message}。请换一个合规动作。"
            )
            decision = None

        if decision is None:
            return fallback()

        if isinstance(decision, ToolStep):
            if gaps:
                merged = list(decision.args.get("gaps") or [])
                for gap in gaps:
                    if gap not in merged:
                        merged.append(gap)
                decision.args["gaps"] = merged
                sink.emit(
                    "warning", code="missing_evidence", message="；".join(gaps)
                )

            sink.emit(
                "tool_progress",
                state="requested",
                seq=step_number,
                tool=decision.tool,
                args=decision.args,
            )
            try:
                observation = await execute_tool(
                    decision,
                    ToolContext(
                        session_id=session_id,
                        repo=repo,
                        store=store,
                        message_id=message_id,
                    ),
                )
            except RepoError as exc:
                observation = {"error": exc.code, "message": str(exc)}
                sink.emit(
                    "tool_progress",
                    state="failed",
                    seq=step_number,
                    tool=decision.tool,
                    error=exc.code,
                )
            else:
                sink.emit(
                    "tool_progress",
                    state="completed",
                    seq=step_number,
                    tool=decision.tool,
                )

            if decision.tool == "propose_change" and "suggestion_id" in observation:
                sink.emit(
                    "suggestion",
                    suggestion_id=observation["suggestion_id"],
                    experience_id=decision.args.get("target"),
                    operation=decision.args.get("operation", "replace"),
                    before=decision.args.get("before"),
                    after=decision.args.get("after", ""),
                    reason=decision.args.get("reason", ""),
                    source_refs=decision.args.get("source_refs", []),
                    gaps=decision.args.get("gaps", []),
                )

            traces.append(
                StepTrace(
                    seq=step_number,
                    action="tool",
                    tool=decision.tool,
                    args=decision.args,
                    observation=observation,
                    rationale=decision.rationale,
                    tokens=step_prompt + step_completion,
                    cost_cents=step_cost,
                )
            )
            history.append(
                f"{step_number}. tool {decision.tool} "
                f"{json.dumps(decision.args, ensure_ascii=False)[:300]} → "
                f"{json.dumps(observation, ensure_ascii=False)[:600]}"
            )
            refresh()
            continue

        if isinstance(decision, AskStep):
            question = record_question(
                focus_doc, decision.message.strip(), decision.slot
            )
            focus_doc.last_run_outcome = "waiting_user"
            persist(focus_doc)
            traces.append(
                StepTrace(
                    seq=step_number,
                    action="ask_user",
                    args={
                        "message": decision.message,
                        "slot": decision.slot,
                    },
                    rationale=decision.rationale,
                    tokens=step_prompt + step_completion,
                    cost_cents=step_cost,
                )
            )
            sink.emit(
                "question",
                question_id=question.id,
                text=question.text,
                slot=decision.slot,
                related_source_ids=decision.related_source_ids,
                fallback=False,
            )
            return finish_result("waiting_user")

        if isinstance(decision, FinishStep):
            refresh()
            outcome = decision.outcome
            if outcome == "completed" and pending_suggestion():
                outcome = "waiting_user"
            if focus_doc is not None:
                focus_doc.last_run_outcome = outcome
                persist(focus_doc)
            sink.emit('section_finished', focus_id=focus_id, outcome=outcome, summary=decision.summary[:400])
            traces.append(
                StepTrace(
                    seq=step_number,
                    action="finish",
                    args={"outcome": decision.outcome, "summary": decision.summary},
                    rationale=decision.rationale,
                    tokens=step_prompt + step_completion,
                    cost_cents=step_cost,
                )
            )
            return finish_result(outcome)

    sink.emit("warning", code="step_limit", message="已达到步数上限，结束本段")
    return limited("step_limit", "已达到步数上限，结束本段")
