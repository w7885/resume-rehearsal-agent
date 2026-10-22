"""Run-checked B state store, namespaced by current resume generation."""
import json
import hashlib

from app.agent.store import StateMeta, StaleState, StoredDoc


class GuardedStateStore:
    def __init__(self, runtime):
        self.runtime = runtime

    def _key(self, connection, session, key):
        refs = [tuple(row) for row in connection.execute("SELECT source_id,source_revision FROM source_blocks WHERE session_id=? AND use_status='active' AND (resume_generation=? OR role='jd') ORDER BY source_id,source_revision", (session['session_id'],session['current_resume_generation']))]
        digest = hashlib.sha256(json.dumps(refs).encode()).hexdigest()
        return f"g{session['current_resume_generation']}:d{session['draft_revision']}:s{digest}:{key}"

    def _scope(self, sid, scope):
        if sid != self.runtime.sid or scope not in {'session', 'experience', 'run'}:
            from app.repository import RepositoryError
            raise RepositoryError('scope_violation', 'Agent 状态范围无效。', False)

    def get(self, session_id, scope, key):
        self._scope(session_id, scope)
        with self.runtime.repo._operation(self.runtime.context) as (connection, session):
            row = connection.execute('SELECT * FROM agent_state WHERE session_id=? AND scope=? AND key=?', (session_id, scope, self._key(connection,session,key))).fetchone()
            return StoredDoc(json.loads(row['doc_json']), row['revision']) if row else None

    def put(self, session_id, scope, key, doc, expected_revision):
        self._scope(session_id, scope)
        with self.runtime.repo._operation(self.runtime.context, write=True) as (connection, session):
            key = self._key(connection,session,key)
            row = connection.execute('SELECT revision FROM agent_state WHERE session_id=? AND scope=? AND key=?', (session_id, scope, key)).fetchone()
            if (row[0] if row else None) != expected_revision:
                raise StaleState('Agent state has changed')
            revision = (row[0] if row else 0) + 1
            connection.execute('INSERT INTO agent_state(session_id,scope,key,doc_json,revision) VALUES (?,?,?,?,?) ON CONFLICT(session_id,scope,key) DO UPDATE SET doc_json=excluded.doc_json,revision=excluded.revision', (session_id, scope, key, json.dumps(doc, ensure_ascii=False), revision))
            return revision

    def list(self, session_id, scope):
        self._scope(session_id, scope)
        with self.runtime.repo._operation(self.runtime.context) as (connection, session):
            prefix = self._key(connection,session,'')
            return [StateMeta(row['key'][len(prefix):], row['revision']) for row in connection.execute('SELECT key,revision FROM agent_state WHERE session_id=? AND scope=? ORDER BY key', (session_id, scope)) if row['key'].startswith(prefix)]
