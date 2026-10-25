from __future__ import annotations

import json
from typing import Any, Callable

from app.agent.events import RunEvent

SCHEMA_VERSION = "0.1"
_CONFLICT = {"invalid_source_ref", "unsupported_number", "role_inflation", "stale_anchor"}
_DIAGNOSIS = {"missing_evidence"}
_PHASE = {"requested": "started", "completed": "completed", "failed": "failed"}


class EventMapper:
    def __init__(
        self,
        *,
        generation: int,
        mode: str = "live",
        assistant_writer: Callable[[str], str] | None = None,
    ) -> None:
        self.generation = generation
        self.mode = mode
        self.assistant_writer = assistant_writer

    def map(self, event: RunEvent) -> dict[str, Any] | None:
        payload = event.payload
        if event.type == "run_finished":
            return self._wrap(
                event,
                "done",
                {
                    "outcome": payload.get("outcome", "limited"),
                    "summary": payload.get("summary", ""),
                    "unresolvedIssueIds": list(
                        payload.get("unresolved_issue_ids") or []
                    ),
                },
            )
        if event.type == "tool_progress":
            return self._wrap(
                event,
                "tool_progress",
                {
                    "toolName": payload.get("tool", ""),
                    "phase": _PHASE.get(payload.get("state", ""), "progress"),
                    "message": payload.get("message", ""),
                    "sourceIds": list(payload.get("source_ids") or []),
                },
            )
        if event.type == "suggestion":
            return self._wrap(
                event,
                "suggestion",
                {"suggestionId": payload.get("suggestion_id", "")},
            )
        if event.type == "question":
            content = payload.get("text", "")
            data = {
                "questionId": payload.get("question_id"),
                "content": content,
                "relatedSourceIds": list(payload.get("related_source_ids") or []),
            }
            if self.assistant_writer is not None:
                data["messageId"] = self.assistant_writer(content)
            return self._wrap(event, "question", data)
        if event.type == "error":
            return self._wrap(
                event,
                "error",
                {
                    "code": payload.get("code", "run_failed"),
                    "message": payload.get("message", ""),
                    "recoverable": bool(payload.get("recoverable", False)),
                },
            )
        if event.type == "warning":
            code = payload.get("code", "")
            kind = "conflict" if code in _CONFLICT else "diagnosis" if code in _DIAGNOSIS else "progress"
            return self._wrap(
                event,
                kind,
                {"code": code, "message": payload.get("message", "")},
            )
        return self._wrap(event, "progress", dict(payload))

    def _wrap(
        self, event: RunEvent, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "sessionId": event.session_id,
            "runId": event.run_id,
            "generation": self.generation,
            "sequence": event.sequence,
            "type": kind,
            "mode": self.mode,
            "payload": payload,
        }


def sse_line(mapped: dict[str, Any]) -> str:
    data = json.dumps(mapped, ensure_ascii=False)
    return f"event: {mapped['type']}\ndata: {data}\n\n"
