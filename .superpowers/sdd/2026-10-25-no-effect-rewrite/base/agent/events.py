from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field


class RunEvent(BaseModel):
    sequence: int
    session_id: str
    run_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EventSink:
    def __init__(self, session_id: str, run_id: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.sequence = 0
        self.events: list[RunEvent] = []
        self.listeners: list[Callable[[RunEvent], None]] = []

    def emit(self, type: str, **payload: Any) -> RunEvent:
        self.sequence += 1
        event = RunEvent(
            sequence=self.sequence,
            session_id=self.session_id,
            run_id=self.run_id,
            type=type,
            payload=payload,
        )
        self.events.append(event)
        for listener in self.listeners:
            listener(event)
        return event
