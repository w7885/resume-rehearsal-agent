from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.agent.llm import Completion
from app.agent.ports import (
    DraftBlock,
    EvidenceHit,
    Issue,
    Message,
    RepoError,
    SessionContext,
    SessionInfo,
    SourceRef,
    SourceText,
    Suggestion,
    verify_proposal_snapshot,
)


def seed_test_suggestion(repo, context, proposal):
    """Capture trusted input before inserting a constructed test candidate."""
    from app.agent.adapters.a_repo import ARepoAdapter
    from app.agent.ports import capture_proposal_snapshot
    from app.material_tools import MaterialTools

    adapter = ARepoAdapter(tools=MaterialTools(repo, context), context=context,
                           session_id=context.session_id)
    snapshot = capture_proposal_snapshot(
        adapter.get_session_context(context.session_id), proposal['target']['blockId'],
        run_id=context.run_id,
    )
    return repo.save_suggestion(context, proposal, snapshot=snapshot)


def _default_sources() -> list[SourceText]:
    return [
        SourceText(
            source_id="src_1",
            kind="resume",
            text="校园二手交易平台。负责前端页面开发，使用 React，完成 3 个页面。",
        ),
        SourceText(
            source_id="src_2",
            kind="supplement",
            text="我整理了8条同学反馈，合并成5项需求，并写成需求文档。",
        ),
        SourceText(
            source_id="src_3",
            kind="jd",
            text="前端开发实习：熟悉 React、TypeScript，有组件库经验。",
        ),
    ]


class FakeRepo:
    def __init__(
        self,
        *,
        session_id: str = "s_01",
        direction: str = "前端开发实习",
        direction_confirmed: bool = False,
        sources: list[SourceText] | None = None,
        messages: list[Message] | None = None,
        draft_blocks: list[DraftBlock] | None = None,
        suggestions: list[Suggestion] | None = None,
        experience_order: list[str] | None = None,
        current_focus_id: str | None = "exp_01",
    ) -> None:
        self.session = SessionInfo(
            session_id=session_id,
            direction=direction,
            direction_confirmed=direction_confirmed,
            primary_material_id="mat_01",
            experience_order=experience_order
            if experience_order is not None
            else ["exp_01", "exp_02", "exp_03"],
            current_focus_id=current_focus_id,
        )
        self.sources = sources if sources is not None else _default_sources()
        self.messages = messages or []
        self.draft_blocks = draft_blocks or []
        self.suggestions = suggestions or []
        self.issues: list[Issue] = []
        self._suggestion_seq = len(self.suggestions)
        self._issue_seq = 0

    def get_session_context(self, session_id: str) -> SessionContext:
        if session_id != self.session.session_id:
            raise RepoError("session_not_found", f"unknown session {session_id}")
        return SessionContext(
            session=self.session.model_copy(deep=True),
            messages=[m.model_copy(deep=True) for m in self.messages],
            draft_blocks=[b.model_copy(deep=True) for b in self.draft_blocks],
            suggestions=[s.model_copy(deep=True) for s in self.suggestions],
            issues=[i.model_copy(deep=True) for i in self.issues],
            sources=[s.model_copy(deep=True) for s in self.sources],
        )

    def search_evidence(
        self, session_id: str, query: str, limit: int = 5
    ) -> list[EvidenceHit]:
        if session_id != self.session.session_id:
            raise RepoError("session_not_found", f"unknown session {session_id}")
        q = re.sub(r"\s+", "", query)
        if len(q) < 2:
            return []
        qgrams = {q[i : i + 2] for i in range(len(q) - 1)}
        hits: list[EvidenceHit] = []
        for source in self.sources:
            grams = {source.text[i : i + 2] for i in range(len(source.text) - 1)}
            score = len(qgrams & grams)
            if score:
                hits.append(
                    EvidenceHit(
                        source_id=source.source_id,
                        quote=source.text[:160],
                        score=float(score),
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.source_id))
        return hits[: max(1, min(limit, 10))]

    def read_material(
        self, session_id: str, source_ids: list[str]
    ) -> list[SourceText]:
        if session_id != self.session.session_id:
            raise RepoError("session_not_found", f"unknown session {session_id}")
        by_id = {source.source_id: source for source in self.sources}
        missing = [sid for sid in source_ids if sid not in by_id]
        if missing:
            raise RepoError("source_not_found", f"unknown sources {missing}")
        return [by_id[sid].model_copy(deep=True) for sid in source_ids]

    def save_suggestion(
        self,
        session_id: str,
        experience_id: str,
        operation: str,
        before: str | None,
        after: str,
        reason: str,
        source_refs: list[SourceRef],
        gaps: list[str],
        *, snapshot=None,
    ) -> Suggestion:
        if session_id != self.session.session_id:
            raise RepoError("session_not_found", f"unknown session {session_id}")
        if experience_id not in self.session.experience_order:
            raise RepoError(
                "scope_violation", f"experience {experience_id} is not in session"
            )
        verify_proposal_snapshot(snapshot, self.get_session_context(session_id),
                                 session_id, experience_id,
                                 run_id=snapshot.run_id if snapshot is not None else None)
        if before != snapshot.before:
            raise RepoError('stale_anchor', 'Suggestion does not use the frozen before', False)
        frozen_sources = {sid: revision for sid, revision, _ in snapshot.source_versions}
        frozen_refs = []
        for ref in source_refs:
            expected = frozen_sources.get(ref.source_id)
            if expected is None or ref.source_revision not in (0, expected):
                raise RepoError('stale_state', 'Source does not belong to the input snapshot', False)
            frozen_refs.append(ref.model_copy(update={'source_revision': expected}))
        self._suggestion_seq += 1
        suggestion = Suggestion(
            suggestion_id=f"sg_{self._suggestion_seq}",
            experience_id=experience_id,
            status="pending",
            operation=operation,
            before=before,
            after=after,
            reason=reason,
            source_refs=frozen_refs,
            gaps=list(gaps),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.suggestions.append(suggestion)
        return suggestion.model_copy(deep=True)

    def save_issues(self, session_id: str, issues: list[Issue]) -> list[str]:
        if session_id != self.session.session_id:
            raise RepoError("session_not_found", f"unknown session {session_id}")
        ids: list[str] = []
        for issue in issues:
            self._issue_seq += 1
            stored = issue.model_copy(deep=True)
            stored.issue_id = f"issue_{self._issue_seq}"
            self.issues.append(stored)
            ids.append(stored.issue_id)
        return ids

    def apply_suggestion_decision(
        self,
        suggestion_id: str,
        decision: str,
        via: str,
        message_id: str | None = None,
    ) -> None:
        for suggestion in self.suggestions:
            if suggestion.suggestion_id == suggestion_id:
                suggestion.status = "accepted" if decision == "accept" else "rejected"
                return
        raise RepoError("invalid_input", f"unknown suggestion {suggestion_id}")


class FakeLLM:
    def __init__(
        self,
        decide_script: list[Any] | None = None,
        route_script: list[Any] | None = None,
    ) -> None:
        self.decide_script = list(decide_script or [])
        self.route_script = list(route_script or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def decide(self, **kwargs: Any) -> Completion:
        self.calls.append(("decide", kwargs))
        item = self._next(self.decide_script)
        return Completion(
            data=item,
            prompt_tokens=120,
            completion_tokens=40,
            cost_cents=1,
        )

    async def route(self, **kwargs: Any) -> Any:
        self.calls.append(("route", kwargs))
        return self._next(self.route_script)

    @staticmethod
    def _next(script: list[Any]) -> Any:
        if not script:
            raise AssertionError("FakeLLM script exhausted")
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
