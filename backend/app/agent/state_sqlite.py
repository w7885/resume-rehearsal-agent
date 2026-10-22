from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.agent.ports import Scope
from app.agent.store import AgentStateStore, StateMeta, StaleState, StoredDoc


class SqliteStateStore(AgentStateStore):
    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path).resolve())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def get(self, session_id: str, scope: Scope, key: str) -> StoredDoc | None:
        with self._connect() as connection:
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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
            connection.commit()
            return revision
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list(self, session_id: str, scope: Scope) -> list[StateMeta]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, revision FROM agent_state "
                "WHERE session_id = ? AND scope = ? ORDER BY key",
                (session_id, scope),
            ).fetchall()
        return [StateMeta(row["key"], row["revision"]) for row in rows]
