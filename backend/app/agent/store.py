from __future__ import annotations

import copy
from typing import Any, NamedTuple, Protocol

from app.agent.ports import Scope


class StoredDoc(NamedTuple):
    doc: dict[str, Any]
    revision: int


class StateMeta(NamedTuple):
    key: str
    revision: int


class StaleState(Exception):
    pass


class AgentStateStore(Protocol):
    def get(self, session_id: str, scope: Scope, key: str) -> StoredDoc | None: ...

    def put(
        self,
        session_id: str,
        scope: Scope,
        key: str,
        doc: dict[str, Any],
        expected_revision: int | None,
    ) -> int: ...

    def list(self, session_id: str, scope: Scope) -> list[StateMeta]: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._docs: dict[tuple[str, str, str], StoredDoc] = {}

    def get(self, session_id: str, scope: Scope, key: str) -> StoredDoc | None:
        stored = self._docs.get((session_id, scope, key))
        if stored is None:
            return None
        return StoredDoc(copy.deepcopy(stored.doc), stored.revision)

    def put(
        self,
        session_id: str,
        scope: Scope,
        key: str,
        doc: dict[str, Any],
        expected_revision: int | None,
    ) -> int:
        k = (session_id, scope, key)
        current = self._docs.get(k)
        actual = current.revision if current is not None else None
        if expected_revision != actual:
            raise StaleState(
                f"{scope}/{key}: expected revision {expected_revision}, got {actual}"
            )
        revision = 1 if current is None else current.revision + 1
        self._docs[k] = StoredDoc(copy.deepcopy(doc), revision)
        return revision

    def list(self, session_id: str, scope: Scope) -> list[StateMeta]:
        metas = [
            StateMeta(key, stored.revision)
            for (sid, sc, key), stored in self._docs.items()
            if sid == session_id and sc == scope
        ]
        return sorted(metas, key=lambda meta: meta.key)
