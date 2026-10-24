from __future__ import annotations

import asyncio
import uuid
from typing import AsyncIterator

from app.agent.events import EventSink, RunEvent
from app.agent.loop import run_loop
from app.agent.memory import (
    ExperienceDoc,
    RunDoc,
    StepTrace,
    derive_phase,
    load_experience,
    load_session,
    next_unconfirmed,
    now_iso,
    record_answer,
    save_experience,
    save_session,
    sync_experiences,
    SessionDoc,
)
from app.agent.ports import Issue, Phase, SessionContext
from app.agent.route import RouteResult, route_message
from app.agent.store import AgentStateStore

_NOTICES = {
    "meta": "这次先专注改当前这段经历。",
    "chitchat": "我们先回到简历这段经历。",
    "refuse": "这个问题我先不展开，继续看当前这段。",
    "global_edit": "整体改写还没开放，先改当前这段。",
}
_LOCKS: dict[str, asyncio.Lock] = {}
_GOAL_QUESTION = "这次想投什么岗位/方向？简历往哪边侧重？"
_GOAL_CANCEL = {"算了", "取消", "不改了"}
_DECLINE_WORDS = (
    "没做过", "不会", "没有", "不记得", "记不清", "忘记", "忘了",
    "不确定", "不清楚", "应该吧", "大概吧", "可能吧", "好像", "跳过", "算了",
)


def _asked_question(doc: ExperienceDoc):
    return next(
        (question for question in reversed(doc.questions) if question.outcome == "asked"),
        None,
    )


def sse_text(event: RunEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


def _load_docs(
    repo, store: AgentStateStore, session_id: str, ctx: SessionContext
) -> list[ExperienceDoc]:
    docs: list[ExperienceDoc] = []
    for experience_id in ctx.session.experience_order:
        loaded = load_experience(store, session_id, experience_id)
        if loaded is not None:
            docs.append(loaded[0])
    return docs


def _phases(ctx: SessionContext, docs: list[ExperienceDoc]) -> dict[str, Phase]:
    suggestions = {s.suggestion_id: s for s in ctx.suggestions}
    return {
        doc.experience_id: derive_phase(
            doc,
            suggestions.get(doc.last_suggestion_id)
            if doc.last_suggestion_id
            else None,
        )
        for doc in docs
    }


def _next_experience_id(repo, store: AgentStateStore, session_id: str, current_id: str | None) -> str | None:
    fresh = repo.get_session_context(session_id)
    # The accepted decision bumped the draft revision; rebuild scoped memory first.
    _load_session_doc(repo, store, session_id, fresh)
    docs = _load_docs(repo, store, session_id, fresh)
    suggestions = {s.suggestion_id: s for s in fresh.suggestions}
    by_id = {doc.experience_id: doc for doc in docs}
    phases = {
        doc.experience_id: derive_phase(
            doc,
            suggestions.get(doc.last_suggestion_id)
            if doc.last_suggestion_id
            else None,
        )
        for doc in docs
    }
    return next_unconfirmed(
        [doc.experience_id for doc in sorted(docs, key=lambda item: item.seq)],
        by_id,
        phases,
        current_id,
    )


def _load_session_doc(
    repo, store: AgentStateStore, session_id: str, ctx: SessionContext
) -> tuple[SessionDoc, int]:
    loaded = load_session(store, session_id)
    if loaded is not None:
        doc, revision = loaded
    else:
        info = ctx.session
        doc = SessionDoc(
            direction=info.direction,
            primary_material_id=info.primary_material_id,
            jd_keywords=list(info.jd_keywords),
            experience_order=list(info.experience_order),
            current_focus_id=info.current_focus_id
            or (info.experience_order[0] if info.experience_order else None),
            pending_clarification=info.pending_clarification,
            summary=info.summary,
        )
        revision = None
    return sync_experiences(session_id, ctx, store, doc, revision)


def _persist_doc(store: AgentStateStore, session_id: str, doc: ExperienceDoc) -> None:
    loaded = load_experience(store, session_id, doc.experience_id)
    save_experience(
        store, session_id, doc, loaded[1] if loaded is not None else None
    )


def _save_run(
    store: AgentStateStore, session_id: str, run: RunDoc
) -> None:
    store.put(session_id, "run", run.run_id, run.model_dump(mode="json"), None)
    loaded = load_session(store, session_id)
    if loaded is not None:
        session, revision = loaded
        session.run_index[run.request_id] = run.run_id
        save_session(store, session_id, session, revision)


async def _execute(
    *,
    run_id: str,
    request_id: str,
    session_id: str,
    text: str,
    message_id: str | None,
    repo,
    store: AgentStateStore,
    llm,
    sink: EventSink,
    expected_state_revision: int | None = None,
    cancel_event: asyncio.Event | None = None,
    start_analysis: bool = False,
) -> None:
    started_at = now_iso()
    ctx = repo.get_session_context(session_id)
    session_doc, session_revision = _load_session_doc(repo, store, session_id, ctx)

    def close(
        outcome: str,
        *,
        action: str | None,
        steps: list[StepTrace] | None = None,
        fallback_used: bool = False,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_cents: int = 0,
        next_experience_id: str | None = None,
        announce: bool = True,
    ) -> None:
        if announce:
            sink.emit(
                "run_finished",
                outcome=outcome,
                fallback_used=fallback_used,
                next_experience_id=next_experience_id,
            )
        run = RunDoc(
            run_id=run_id,
            request_id=request_id,
            state_revision=session_revision,
            action=action,
            focus_experience_id=session_doc.current_focus_id,
            outcome=outcome,
            fallback_used=fallback_used,
            steps=steps or [],
            events=[event.model_dump(mode="json") for event in sink.events],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_cents=cost_cents,
            started_at=started_at,
            ended_at=now_iso(),
        )
        _save_run(store, session_id, run)
        finisher = getattr(repo, "finish_run", None)
        if callable(finisher):
            finisher(outcome)

    if (
        expected_state_revision is not None
        and expected_state_revision != ctx.session.state_revision
    ):
        sink.emit(
            "error",
            code="stale_state",
            message="会话已更新，请刷新后重试。",
            recoverable=False,
        )
        close("limited", action=None)
        return

    existing = session_doc.run_index.get(request_id)
    if existing:
        stored = store.get(session_id, "run", existing)
        payload = stored.doc if stored is not None else {}
        sink.emit(
            "run_started",
            focus_experience_id=session_doc.current_focus_id,
            replayed=True,
        )
        for event in payload.get("events", []):
            if event.get("type") in ("run_started", "run_finished"):
                continue
            sink.emit(event["type"], **event.get("payload", {}))
        sink.emit(
            "run_finished",
            outcome=payload.get("outcome") or "unknown",
            fallback_used=payload.get("fallback_used", False),
            replayed=True,
        )
        return

    ctx.session.current_focus_id = session_doc.current_focus_id
    docs = _load_docs(repo, store, session_id, ctx)
    phases = _phases(ctx, docs)
    by_id = {doc.experience_id: doc for doc in docs}
    focus_id = session_doc.current_focus_id
    focus_doc = by_id.get(focus_id or "")
    has_question = bool(
        focus_doc and any(q.outcome == "asked" for q in focus_doc.questions)
    )

    sink.emit("run_started", focus_experience_id=focus_id)

    goal_reply = (
        session_doc.awaiting_goal
        and not has_question
        and bool(text.strip())
        and text.strip() not in _GOAL_CANCEL
    )

    try:
        if goal_reply:
            session_doc.goal = text.strip()[:200]
            session_doc.awaiting_goal = False
            decision = RouteResult(
                action="answer",
                target_experience_id=focus_id,
                scope="experience",
                rationale="目标答复",
            )
        else:
            decision = RouteResult(action='answer', target_experience_id=focus_id, scope='experience',
                                   rationale='材料用途已确认，开始首次分析') if start_analysis else await route_message(
                message=text,
                ctx=ctx,
                docs=docs,
                phases=phases,
                pending=session_doc.pending_clarification,
                has_pending_question=has_question,
                llm=llm,
            )
    except Exception as exc:
        sink.emit("error", code="route_failed", message=str(exc)[:200])
        close("limited", action=None)
        return

    sink.emit(
        "route",
        action=decision.action,
        target_experience_id=decision.target_experience_id,
        rationale=decision.rationale,
    )

    previous_focus = focus_id
    if decision.target_experience_id:
        session_doc.current_focus_id = decision.target_experience_id
        focus_id = decision.target_experience_id
        focus_doc = by_id.get(focus_id)
    if decision.action == "clarify":
        session_doc.pending_clarification = decision.pending_clarification
    else:
        session_doc.pending_clarification = None
    session_revision = save_session(
        store, session_id, session_doc, session_revision
    )

    action = decision.action
    if action == "drop":
        close("dropped", action=action)
        return

    if action == "clarify":
        if decision.clarification is not None:
            sink.emit(
                "clarification", **decision.clarification.model_dump(mode="json")
            )
        close("clarify", action=action)
        return

    advancing = action == "advance"
    if action == "advance":
        if decision.target_experience_id is None:
            sink.emit(
                "notice",
                code="no_experience",
                text="识别到的项目/实习都已处理完。",
            )
            close("completed", action=action)
            return
        sink.emit(
            "focus_changed",
            experience_id=decision.target_experience_id,
            echo=decision.echo,
        )
        action = "answer"

    if action == "focus":
        if decision.target_experience_id == previous_focus:
            action = "correct"
        else:
            sink.emit(
                "focus_changed",
                experience_id=decision.target_experience_id,
                echo=decision.echo,
            )
            close("focused", action=action)
            return

    if action == "cancel":
        close("cancelled", action=action)
        return

    if action == "global_edit":
        sink.emit(
            "notice",
            code=action,
            text=_NOTICES[action],
            implemented=False,
        )
        repo.save_issues(
            session_id,
            [
                Issue(
                    kind="global_edit_unsupported",
                    summary="整体改写未实现，已按当前段处理",
                    experience_id=None,
                )
            ],
        )
        # Whole-resume rewriting is unsupported; fall through to the current segment.
        action = "answer"

    if action in ("meta", "chitchat", "refuse"):
        sink.emit("notice", code=action, text=_NOTICES[action])
        close(action, action=action)
        return

    if focus_doc is None:
        sink.emit(
            "notice",
            code="no_experience",
            text="简历里还没识别到项目/实习经历，请上传或粘贴一段项目/实习。",
        )
        close("no_focus", action=action)
        return

    if (
        action in ("answer", "correct")
        and not has_question
        and not session_doc.goal
        and not ctx.session.direction_confirmed
        and not session_doc.goal_asked
    ):
        session_doc.goal_asked = True
        session_doc.awaiting_goal = True
        save_session(store, session_id, session_doc, session_revision)
        sink.emit(
            "question",
            question_id="goal",
            text=_GOAL_QUESTION,
            slot=None,
            related_source_ids=[],
            fallback=False,
        )
        close("waiting_user", action=action)
        return

    if action == "confirm":
        pending = next(
            (
                suggestion
                for suggestion in ctx.suggestions
                if suggestion.suggestion_id == focus_doc.last_suggestion_id
                and suggestion.status == "pending"
            ),
            None,
        )
        if pending is None:
            sink.emit(
                "notice", code="no_pending_suggestion", text="当前没有待确认的改写。"
            )
            close("waiting_user", action=action)
            return
        repo.apply_suggestion_decision(
            pending.suggestion_id, "accept", "message", message_id
        )
        sink.emit(
            "suggestion_applied",
            suggestion_id=pending.suggestion_id,
            experience_id=pending.experience_id,
        )
        close(
            "completed",
            action=action,
            next_experience_id=_next_experience_id(
                repo, store, session_id, focus_doc.experience_id
            ),
        )
        return

    answered = False
    if action == "skip":
        question = _asked_question(focus_doc)
        if question is None:
            focus_doc.skipped = True
            _persist_doc(store, session_id, focus_doc)
            sink.emit("focus_skipped", experience_id=focus_doc.experience_id)
            close("skipped", action=action)
            return
        record_answer(focus_doc, question.id, message_id, declined=True)
        _persist_doc(store, session_id, focus_doc)
        answered = True

    if action == "correct":
        pending = next(
            (
                suggestion
                for suggestion in ctx.suggestions
                if suggestion.suggestion_id == focus_doc.last_suggestion_id
                and suggestion.status == "pending"
            ),
            None,
        )
        if pending is not None:
            repo.apply_suggestion_decision(
                pending.suggestion_id, "reject", "message", message_id
            )

    if action == "answer" and not advancing:
        question = _asked_question(focus_doc)
        if question is not None:
            declined = any(word in text for word in _DECLINE_WORDS)
            record_answer(
                focus_doc, question.id, message_id, declined=declined
            )
            _persist_doc(store, session_id, focus_doc)
            answered = True

    # ponytail: every interview-style turn runs the single-segment loop; other
    # actions were handled above and need no model call.
    result = await run_loop(
        run_id=run_id,
        session_id=session_id,
        repo=repo,
        store=store,
        llm=llm,
        focus_experience_id=focus_id,
        message_id=message_id,
        answered_question=answered,
        session_goal=session_doc.goal,
        sink=sink,
        emit_start=False,
        cancel_event=cancel_event,
    )
    close(
        result.outcome,
        action=action,
        steps=result.steps,
        fallback_used=result.fallback_used,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_cents=result.cost_cents,
        next_experience_id=result.next_experience_id,
        announce=False,
    )


async def stream_run(
    *,
    session_id: str,
    text: str,
    repo,
    store: AgentStateStore,
    llm,
    request_id: str | None = None,
    message_id: str | None = None,
    run_id: str | None = None,
    expected_state_revision: int | None = None,
    cancel_event: asyncio.Event | None = None,
    start_analysis: bool = False,
) -> AsyncIterator[RunEvent]:
    run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
    request_id = request_id or run_id
    sink = EventSink(session_id, run_id)
    queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
    sink.listeners.append(queue.put_nowait)

    async def runner() -> None:
        lock = _LOCKS.setdefault(session_id, asyncio.Lock())
        try:
            async with lock:
                await _execute(
                    run_id=run_id,
                    request_id=request_id,
                    session_id=session_id,
                    text=text,
                    message_id=message_id,
                    repo=repo,
                    store=store,
                    llm=llm,
                    sink=sink,
                    expected_state_revision=expected_state_revision,
                    cancel_event=cancel_event,
                    start_analysis=start_analysis,
                )
        except Exception as exc:
            sink.emit("error", code="run_failed", message=str(exc)[:200])
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(runner())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        if not task.done():
            task.cancel()


async def handle_message(
    *,
    session_id: str,
    text: str,
    repo,
    store: AgentStateStore,
    llm,
    request_id: str | None = None,
    message_id: str | None = None,
    expected_state_revision: int | None = None,
    cancel_event: asyncio.Event | None = None,
) -> list[RunEvent]:
    events: list[RunEvent] = []
    async for event in stream_run(
        session_id=session_id,
        text=text,
        repo=repo,
        store=store,
        llm=llm,
        request_id=request_id,
        message_id=message_id,
        expected_state_revision=expected_state_revision,
        cancel_event=cancel_event,
    ):
        events.append(event)
    return events
