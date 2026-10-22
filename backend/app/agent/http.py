from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.agent.event_mapper import EventMapper, sse_line
from app.agent.router import stream_run
from app.agent.store import AgentStateStore


class AgentRunHandle(Protocol):
    run_id: str
    generation: int
    repo: Any


@dataclass
class AgentDependencies:
    begin_run: Callable[[str, int], AgentRunHandle]
    store: AgentStateStore
    llm_factory: Callable[[str], Any]
    budget_status: Callable[[str], dict[str, Any]]


class RunRequest(BaseModel):
    message: str = Field(min_length=1)
    expectedStateRevision: int = Field(ge=1)
    messageId: str | None = None
    requestId: str | None = None


def create_agent_router(deps: AgentDependencies) -> APIRouter:
    router = APIRouter()
    active: dict[str, tuple[AgentRunHandle, asyncio.Event]] = {}

    @router.post("/api/sessions/{session_id}/runs")
    async def start_run(session_id: str, body: RunRequest, request: Request):
        handle = deps.begin_run(session_id, body.expectedStateRevision)
        cancel_event = asyncio.Event()
        active[handle.run_id] = (handle, cancel_event)
        mapper = EventMapper(
            generation=handle.generation,
            assistant_writer=handle.repo.save_assistant_message,
        )

        async def generator():
            try:
                async for event in stream_run(
                    session_id=session_id,
                    text=body.message,
                    repo=handle.repo,
                    store=deps.store,
                    llm=deps.llm_factory(session_id),
                    request_id=body.requestId,
                    message_id=body.messageId,
                    run_id=handle.run_id,
                    expected_state_revision=body.expectedStateRevision,
                    cancel_event=cancel_event,
                ):
                    mapped = mapper.map(event)
                    if mapped is None:
                        continue
                    handle.repo.append_event(mapped)
                    yield sse_line(mapped)
            finally:
                active.pop(handle.run_id, None)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        entry = active.get(run_id)
        if entry is None:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": {
                        "code": "cancelled",
                        "message": "本次运行已结束或不存在。",
                        "recoverable": False,
                    },
                },
            )
        handle, cancel_event = entry
        cancel_event.set()
        handle.repo.cancel_run()
        return {"ok": True, "data": {"runId": run_id, "status": "cancelled"}}

    @router.get("/api/sessions/{session_id}/budget")
    async def budget(session_id: str):
        return {"ok": True, "data": deps.budget_status(session_id)}

    return router
