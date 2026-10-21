"""Atomic, idempotent user operations. No model invocation or acceptance path."""

import hashlib
import json
import time

from app.db import database, transaction
from app.repository import Repository, RepositoryError, RequestContext


REQUEST_TTL_SECONDS = 24 * 60 * 60


class BusinessService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _mutate(self, scope, operation, payload, request_id, callback, session_id=None):
        fingerprint = hashlib.sha256(json.dumps(
            {"operation": operation, "payload": payload}, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        with database(self.repository.path) as connection, transaction(connection):
            now = int(time.time())
            connection.execute("DELETE FROM request_log WHERE expires_at <= ?", (now,))
            old = connection.execute("SELECT * FROM request_log WHERE scope_key = ? AND request_id = ?",
                                     (scope, request_id)).fetchone()
            if old is not None:
                if old["state"] == "deleted":
                    raise RepositoryError("session_not_found", "该请求对应的会话已删除，不能恢复。", False)
                if old["operation"] != operation or old["request_hash"] != fingerprint:
                    raise RepositoryError("request_conflict", "该请求编号已用于不同内容，请勿更换重试内容。")
                return json.loads(old["response_json"])
            # Replay is checked before optimistic versions. The prior successful
            # operation may itself have incremented the session revision.
            bound = Repository(self.repository.path, _connection=connection)
            data = callback(bound)
            response = {"ok": True, "data": data}
            link = data["sessionId"] if operation == "create_session" else session_id
            if operation == "delete_session":
                link = None
            connection.execute(
                "INSERT INTO request_log(scope_key, request_id, operation, request_hash, session_id, response_json, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope, request_id, operation, fingerprint, link, json.dumps(response, ensure_ascii=False), now + REQUEST_TTL_SECONDS),
            )
            return response

    def create_session(self, payload):
        return self._mutate("sessions", "create_session", payload, payload["requestId"],
                            lambda repo: repo.create_session(payload["direction"]))

    def save_message(self, session_id, payload):
        def apply(repo):
            context = RequestContext(session_id, payload["expectedStateRevision"])
            message = repo.save_message(context, payload["content"], kind=payload["kind"])
            source = None
            if payload["kind"] != "skip":
                current = repo.get_session(RequestContext(session_id))
                source = repo.save_source(
                    RequestContext(session_id, current["stateRevision"]),
                    text=payload["content"], role="supplement", use_status="active",
                    origin={"kind": "message", "messageId": message["messageId"]},
                )
            session = repo.get_session(RequestContext(session_id))
            return {"message": message, "source": source, "stateRevision": session["stateRevision"], "draftRevision": session["draftRevision"]}
        return self._mutate(f"session:{session_id}", "save_message", payload, payload["requestId"], apply, session_id)

    def save_material(self, session_id, metadata, raw, media_type, extracted_text=None):
        payload = {**metadata, "mediaType": media_type, "contentHash": hashlib.sha256(raw).hexdigest()}
        def apply(repo):
            context = RequestContext(session_id, metadata["expectedStateRevision"])
            existing_draft = repo.get_draft(context)
            role = metadata["role"]
            ambiguous_resume = role == "resume" and bool(existing_draft["blocks"])
            usable = extracted_text is not None and role != "unknown" and not ambiguous_resume
            material = repo.save_material(
                context, name=metadata["name"], media_type=media_type, original_data=raw, role=role,
                parse_status="parsed" if extracted_text is not None else "pending", use_status="active" if usable else "pending",
            )
            sources = []
            if extracted_text is not None:
                current = repo.get_session(RequestContext(session_id))
                source = repo.save_source(
                    RequestContext(session_id, current["stateRevision"]), text=extracted_text, role=role,
                    use_status="active" if usable else "pending",
                    origin={"kind": "material", "materialId": material["materialId"],
                            "lineStart": 1, "lineEnd": max(1, len(extracted_text.splitlines()))},
                )
                sources.append(source)
            draft_created = False
            if usable and role == "resume" and not existing_draft["blocks"]:
                current = repo.get_session(RequestContext(session_id))
                repo.create_initial_draft(RequestContext(session_id, current["stateRevision"]), [{
                    "markdown": extracted_text,
                    "sourceRefs": [{"sourceId": sources[0]["sourceId"], "sourceRevision": 1, "quote": extracted_text}],
                }])
                draft_created = True
            session = repo.get_session(RequestContext(session_id))
            return {"material": material, "sources": sources, "draftCreated": draft_created,
                    "needsClarification": role == "unknown" or ambiguous_resume,
                    "stateRevision": session["stateRevision"], "draftRevision": session["draftRevision"]}
        return self._mutate(f"session:{session_id}", "save_material", payload, metadata["requestId"], apply, session_id)

    def delete_session(self, session_id, payload):
        return self._mutate(f"session:{session_id}", "delete_session", payload, payload["requestId"],
                            lambda repo: repo.delete_session(RequestContext(session_id, payload["expectedStateRevision"])), session_id)
