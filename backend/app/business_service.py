"""Atomic, idempotent user operations. Never invokes the model."""

import hashlib
import json
import time

from app.db import database, transaction
from app.repository import Repository, RepositoryError, RequestContext


REQUEST_TTL_SECONDS = 24 * 60 * 60


class BusinessService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def prepare_material_page(self, session_id, material_id, payload):
        from app.page_service import prepare_material_page
        return prepare_material_page(self, session_id, material_id, payload)

    def save_recognition_result(self, context, payload, *, synthetic=False):
        from app.recognition_service import save_result
        return save_result(self, context, payload, synthetic)

    def get_cached_recognition(self, context, material_id, page_number, recognition_version):
        from app.recognition_service import cached_result
        return cached_result(self.repository, context, material_id, page_number, recognition_version)

    def reorder_images(self, session_id, payload):
        from app.recognition_service import reorder_images
        return reorder_images(self, session_id, payload)

    def request_recognition_retry(self, session_id, material_id, payload):
        from app.recognition_service import request_retry
        return request_retry(self, session_id, material_id, payload)

    def save_assistant_message(self, context, payload, *, synthetic=False):
        from app.assistant_service import save_assistant
        return save_assistant(self, context, payload, synthetic)

    def get_chat_turn_context(self, context, user_message_id):
        from app.assistant_service import get_turn_context
        return get_turn_context(self, context, user_message_id)

    def apply_resolved_user_decision(self, context, payload, *, synthetic=False):
        from app.chat_decisions import apply_resolution
        return apply_resolution(self, context, payload, synthetic=synthetic)

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
            link = data["sessionId"] if operation in {"create_session", "create_mock_session", "create_chat_session"} else session_id
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

    def create_mock_session(self, payload):
        from app.mock_demo import create_demo
        return self._mutate("sessions", "create_mock_session", payload, payload["requestId"], create_demo)

    def create_chat_session(self, payload):
        from app.chat_service import create_chat_session
        return self._mutate("sessions", "create_chat_session", payload, payload["requestId"], create_chat_session)

    def save_chat_message(self, session_id, payload):
        from app.chat_service import save_chat_message
        return self._mutate(f"session:{session_id}", "save_chat_message", payload, payload["requestId"],
                            lambda repo: save_chat_message(repo, RequestContext(session_id, payload["expectedStateRevision"]), payload), session_id)

    def analyze_mock_session(self, session_id, payload):
        from app.mock_demo import analyze_demo
        return self._mutate(f"session:{session_id}", "analyze_mock_session", payload, payload["requestId"],
                            lambda repo: analyze_demo(repo, RequestContext(session_id, payload["expectedStateRevision"])), session_id)

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
        from app.document_parser import parse_upload
        parsed = parse_upload(media_type, raw, extracted_text)
        payload = {**metadata, "mediaType": media_type, "contentHash": hashlib.sha256(raw).hexdigest()}
        def apply(repo):
            context = RequestContext(session_id, metadata["expectedStateRevision"])
            has_draft_history = repo.has_draft_history(context)
            role = metadata["role"]
            ambiguous_resume = role == "resume" and has_draft_history
            usable = parsed["status"] == "parsed" and role != "unknown" and not ambiguous_resume
            material = repo.save_material(
                context, name=metadata["name"], media_type=media_type, original_data=raw, role=role,
                parse_status=parsed["status"], use_status="active" if usable else "pending",
            )
            repo._connection.execute("UPDATE attachments SET parse_report_json = ? WHERE session_id = ? AND attachment_id = ?",
                                     (json.dumps(parsed["report"], ensure_ascii=False), session_id, material["materialId"]))
            material["parseReport"] = parsed["report"]
            sources = []
            for block in parsed["blocks"]:
                current = repo.get_session(RequestContext(session_id))
                source = repo.save_source(
                    RequestContext(session_id, current["stateRevision"]), text=block["text"], role=role,
                    use_status="active" if usable else "pending",
                    origin={"kind": "material", "materialId": material["materialId"], **block["origin"]},
                )
                sources.append(source)
            draft_created = False
            if usable and role == "resume" and not has_draft_history:
                from app.resume_structure import structure_blocks
                current = repo.get_session(RequestContext(session_id))
                repo.create_initial_draft(RequestContext(session_id, current["stateRevision"]), structure_blocks([{
                    "markdown": source["text"],
                    "sourceRefs": [{"sourceId": source["sourceId"], "sourceRevision": 1, "quote": source["text"]}],
                } for source in sources]))
                draft_created = True
            session = repo.get_session(RequestContext(session_id))
            return {"material": material, "sources": sources, "draftCreated": draft_created,
                    "needsClarification": role == "unknown" or ambiguous_resume,
                    "stateRevision": session["stateRevision"], "draftRevision": session["draftRevision"]}
        return self._mutate(f"session:{session_id}", "save_material", payload, metadata["requestId"], apply, session_id)

    def delete_session(self, session_id, payload):
        return self._mutate(f"session:{session_id}", "delete_session", payload, payload["requestId"],
                            lambda repo: repo.delete_session(RequestContext(session_id, payload["expectedStateRevision"])), session_id)

    def correct_material_source(self, session_id, material_id, source_id, payload):
        from app.repository import RepositoryError
        def apply(repo):
            context = RequestContext(session_id, payload["expectedStateRevision"])
            with repo._operation(context, write=True) as (connection, session):
                if session["mode"] != "local":
                    raise RepositoryError("scope_violation", "模拟历史只供回顾，不能修改为个人材料。", False)
                row = repo._find_source(connection, session_id, source_id)
                parent = connection.execute("SELECT * FROM attachments WHERE session_id = ? AND attachment_id = ?", (session_id, material_id)).fetchone()
                if parent is None or row["attachment_id"] != material_id:
                    raise RepositoryError("source_not_found", "原文不属于当前会话附件。")
                if parent["use_status"] == "history" or row["resume_generation"] != session["current_resume_generation"]:
                    raise RepositoryError("stale_state", "历史材料不能直接修改为当前事实，请重新上传。")
                source = repo.save_source(context, text=payload["text"], role=row["role"], origin=json.loads(row["origin_json"]),
                    use_status=row["use_status"], source_id=source_id, expected_source_revision=payload["expectedSourceRevision"])
                current = repo.get_session(RequestContext(session_id))
                return {"source": source, "stateRevision": current["stateRevision"], "draftRevision": current["draftRevision"]}
        return self._mutate(f"session:{session_id}", "correct_material_source", {**payload, "materialId": material_id, "sourceId": source_id}, payload["requestId"], apply, session_id)

    def apply_suggestion_decision(self, session_id, suggestion_id, payload):
        fingerprint_payload = {**payload, "suggestionId": suggestion_id}
        return self._mutate(f"session:{session_id}", "suggestion_decision", fingerprint_payload, payload["requestId"],
                            lambda repo: repo.apply_suggestion_decision(RequestContext(session_id, payload["expectedStateRevision"]), suggestion_id, payload), session_id)

    def update_draft(self, session_id, payload):
        return self._mutate(f"session:{session_id}", "update_draft", payload, payload["requestId"],
                            lambda repo: repo.update_draft(RequestContext(session_id, payload["expectedStateRevision"]), payload["markdown"]), session_id)

    def replace_draft(self, session_id, payload):
        return self._mutate(f"session:{session_id}", "replace_draft", payload, payload["requestId"],
                            lambda repo: repo.replace_draft(RequestContext(session_id, payload["expectedStateRevision"]), payload), session_id)
