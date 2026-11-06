from __future__ import annotations

import json
from pathlib import Path

from app.db import database, transaction
from app.postgres import database_target
from app.agent.ports import Scope
from app.agent.store import AgentStateStore, StateMeta, StaleState, StoredDoc


class SqliteStateStore(AgentStateStore):
    def __init__(self, path: str | Path) -> None:
        self.path = database_target(path)

    def get(self, session_id: str, scope: Scope, key: str) -> StoredDoc | None:
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT doc_json, revision FROM agent_state "
                "WHERE session_id = ? AND scope = ? AND key = ?",
                (session_id, scope, key),
            ).fetchone()
        if row is None:
            return None
        return StoredDoc(json.loads(row["doc_json"]), row["revision"])

    def put(
        self,
        session_id: str,
        scope: Scope,
        key: str,
        doc: dict,
        expected_revision: int | None,
    ) -> int:
        payload = json.dumps(doc, ensure_ascii=False)
        with database(self.path) as connection, transaction(connection):
            row = connection.execute(
                "SELECT revision FROM agent_state "
                "WHERE session_id = ? AND scope = ? AND key = ?",
                (session_id, scope, key),
            ).fetchone()
            actual = row["revision"] if row is not None else None
            if expected_revision != actual:
                raise StaleState(
                    f"{scope}/{key}: expected revision {expected_revision}, got {actual}"
                )
            if row is None:
                connection.execute(
                    "INSERT INTO agent_state(session_id, scope, key, doc_json, revision) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (session_id, scope, key, payload),
                )
                revision = 1
            else:
                revision = actual + 1
                connection.execute(
                    "UPDATE agent_state SET doc_json = ?, revision = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE session_id = ? AND scope = ? AND key = ?",
                    (payload, revision, session_id, scope, key),
                )
            return revision
    def list(self, session_id: str, scope: Scope) -> list[StateMeta]:
        with database(self.path) as connection:
            rows = connection.execute(
                "SELECT key, revision FROM agent_state "
                "WHERE session_id = ? AND scope = ? ORDER BY key",
                (session_id, scope),
            ).fetchall()
        return [StateMeta(row["key"], row["revision"]) for row in rows]
