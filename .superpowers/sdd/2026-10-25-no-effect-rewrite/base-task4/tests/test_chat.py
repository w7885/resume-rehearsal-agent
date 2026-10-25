"""A-T08.3: persisted raw chat, attachment ownership, recovery, no inference."""

from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from app import db
from app.business_service import BusinessService
from app.chat_service import save_chat_message
from app.repository import Repository, RepositoryError, RequestContext
import test_api


class ChatTests(unittest.TestCase):
    setUp = test_api.ApiTests.setUp
    request = test_api.ApiTests.request
    new_session = test_api.ApiTests.new_session
    version = test_api.ApiTests.version
    material = test_api.ApiTests.material
    count = test_api.ApiTests.count
    assert_error = test_api.ApiTests.assert_error

    def chat(self, request_id="chat-create"):
        response = self.request("POST", "/api/chat/sessions", data={"requestId": request_id})
        self.assertEqual(response["status"], 201, response)
        return response["json"]["data"]

    def conversation(self, session):
        return self.request("GET", f"/api/sessions/{session['sessionId']}/conversation")["json"]["data"]

    def send(self, session, text, ids=None, request_id="chat-message", revision=None, **extra):
        payload = {"requestId": request_id, "expectedStateRevision": self.version(session["sessionId"]) if revision is None else revision,
                   "content": text, "attachmentIds": ids or [], **extra}
        return self.request("POST", f"/api/sessions/{session['sessionId']}/chat/messages", data=payload)

    def snapshot(self):
        with db.database(self.path, read_only=True) as connection:
            return list(connection.iterdump())

    def test_create_without_direction_is_pending_and_replay_does_not_duplicate(self):
        session = self.chat()
        self.assertEqual(self.chat(), session)
        self.assertEqual(self.count("sessions"), 1)
        self.assertEqual(session["directionStatus"], "pending")
        self.assertIsNone(session["targetDirection"])
        self.assertEqual(session["title"], "新对话")
        self.assertEqual(self.conversation(session)["draft"]["markdown"], "")
        self.assertFalse(self.conversation(session)["capabilities"]["agentAvailable"])
        self.assert_error(self.request("POST", "/api/chat/sessions", data={"requestId": "spoof", "mode": "mock"}), 422, "invalid_input")

    def test_hypotheses_and_decisions_remain_exact_raw_messages_not_sources_or_draft(self):
        session = self.chat()
        original_draft = self.conversation(session)["draft"]
        for index, text in enumerate(("  如果我做过 API 集成，是不是更有优势？\n", "可以", "不要写数字", "我想投广州的产品实习")):
            result = self.send(session, text, request_id=f"raw-{index}")
            self.assertEqual(result["status"], 201, result)
            self.assertEqual(result["json"]["data"]["message"]["content"], text)
        restored = self.conversation(session)
        self.assertEqual([m["sequence"] for m in restored["messages"]], [1, 2, 3, 4])
        self.assertEqual({m["inputChannel"] for m in restored["messages"]}, {"chat"})
        self.assertEqual(self.count("source_blocks"), 0)
        self.assertEqual(self.count("suggestion_decisions"), 0)
        self.assertEqual(self.count("runs"), 0)
        self.assertEqual(restored["draft"], original_draft)
        self.assertEqual(restored["session"]["directionStatus"], "pending")
        self.assertIsNone(restored["session"]["targetDirection"])

    def test_attachments_only_message_roundtrips_original_and_keeps_unknown_material_pending(self):
        session = self.chat()
        material = self.material(session, text="# 构造材料\n只用于原件恢复", role="unknown")["json"]["data"]["material"]
        result = self.send(session, "", [material["materialId"]])
        self.assertEqual(result["status"], 201, result)
        restored = self.conversation(session)
        message = restored["messages"][0]
        self.assertEqual(message["content"], "")
        self.assertEqual(message["attachments"][0]["materialId"], material["materialId"])
        self.assertEqual(message["attachments"][0]["role"], "unknown")
        self.assertEqual(message["attachments"][0]["useStatus"], "pending")
        self.assertNotIn("originalData", message["attachments"][0])
        raw = self.request("GET", f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/file")
        self.assertEqual(raw["body"].decode(), "# 构造材料\n只用于原件恢复")
        evidence = Repository(self.path).search_evidence(RequestContext(session["sessionId"]), "构造材料")
        self.assertEqual(evidence, [])
        self.assertEqual(restored["draft"]["markdown"], "")

    def test_attachment_order_and_restart_restore_do_not_depend_on_timestamp_ties(self):
        session = self.chat()
        first = self.material(session, text="第一份", role="unknown", request_id="a")["json"]["data"]["material"]
        second = self.material(session, text="第二份", role="unknown", request_id="b")["json"]["data"]["material"]
        self.send(session, "附件顺序", [second["materialId"], first["materialId"]])
        self.send(session, "第二条原话", request_id="second")
        with db.database(self.path) as connection:
            connection.execute("UPDATE messages SET created_at='2000-01-01T00:00:00Z' WHERE session_id=?", (session["sessionId"],))
        before = self.conversation(session)
        reopened = Repository(self.path).get_conversation(RequestContext(session["sessionId"]))
        self.assertEqual(reopened, before)
        self.assertEqual([m["content"] for m in reopened["messages"]], ["附件顺序", "第二条原话"])
        self.assertEqual([m["materialId"] for m in reopened["messages"][0]["attachments"]], [second["materialId"], first["materialId"]])

    def test_foreign_missing_duplicate_and_historical_attachments_are_rejected_atomically(self):
        session = self.chat()
        other = self.new_session()
        foreign = self.material(other, role="unknown")["json"]["data"]["material"]["materialId"]
        own = self.material(session, role="unknown", request_id="own")["json"]["data"]["material"]["materialId"]
        before = self.count("messages")
        for ids, status, code in (([foreign], 404, "source_not_found"), (["missing"], 404, "source_not_found"), ([own, own], 422, "invalid_input")):
            self.assert_error(self.send(session, "不应写入", ids, request_id="rejected"), status, code)
            self.assertEqual(self.count("messages"), before)
        with db.database(self.path) as connection:
            connection.execute("UPDATE attachments SET use_status='history' WHERE attachment_id=?", (own,))
        self.assert_error(self.send(session, "旧附件", [own], request_id="history"), 409, "stale_state")
        self.assertEqual(self.count("chat_message_attachments"), 0)

    def test_invalid_empty_requests_cannot_forge_role_channel_decision_or_versions(self):
        session = self.chat()
        for extra in ({}, {"role": "assistant"}, {"decision": "accept"}, {"inputChannel": "legacy"}, {"expectedStateRevision": True}, {"attachmentIds": [1]}):
            result = self.send(session, "" if not extra else "原话", request_id="invalid", **extra)
            self.assert_error(result, 422, "invalid_input")
        self.assertEqual(self.count("messages"), 0)

    def test_replay_precedes_old_version_check_and_new_id_same_text_is_distinct(self):
        session = self.chat()
        first = self.send(session, "同一文字", revision=1)
        self.assertEqual(first["status"], 201)
        self.assertEqual(self.send(session, "同一文字", revision=1)["json"], first["json"])
        self.assert_error(self.send(session, "换内容", revision=1), 409, "request_conflict")
        self.assert_error(self.send(session, "旧版本", revision=1, request_id="new-id"), 409, "stale_state")
        self.assertEqual(self.send(session, "同一文字", request_id="independent")["status"], 201)
        self.assertEqual(self.count("messages"), 2)

    def test_concurrent_and_restarted_service_retries_store_one_message(self):
        session = self.chat()
        payload = {"requestId": "concurrent", "expectedStateRevision": 1, "content": "  原话\n", "attachmentIds": []}
        def run(_=None): return BusinessService(Repository(self.path)).save_chat_message(session["sessionId"], payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [1, 2]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(run(), results[0])
        self.assertEqual(self.count("messages"), 1)

    def test_attachment_binding_failure_rolls_back_message_title_state_and_request_cache(self):
        session = self.chat()
        material = self.material(session, role="unknown")["json"]["data"]["material"]
        with db.database(self.path) as connection:
            connection.execute("CREATE TRIGGER fail_chat_link BEFORE INSERT ON chat_message_attachments BEGIN SELECT RAISE(ABORT, 'injected'); END")
        before = self.snapshot()
        self.assert_error(self.send(session, "不能半保存", [material["materialId"]]), 503, "storage_failed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.count("attachments"), 1)

    def test_cache_failure_reverts_message_and_cannot_claim_saved(self):
        session = self.chat()
        with db.database(self.path) as connection:
            connection.execute("CREATE TRIGGER fail_chat_cache BEFORE INSERT ON request_log BEGIN SELECT RAISE(ABORT, 'injected'); END")
        before = self.snapshot()
        self.assert_error(self.send(session, "不应保存"), 503, "storage_failed")
        self.assertEqual(self.snapshot(), before)

    def test_delete_cascades_links_and_old_requests_cannot_rebuild_session(self):
        session = self.chat()
        material = self.material(session, role="unknown")["json"]["data"]["material"]
        rev = self.version(session["sessionId"])
        self.send(session, "旧消息正文", [material["materialId"]], revision=rev)
        self.assertEqual(self.count("chat_message_attachments"), 1)
        result = self.request("DELETE", f"/api/sessions/{session['sessionId']}", data={"requestId": "delete", "expectedStateRevision": self.version(session["sessionId"]), "confirmed": True})
        self.assertEqual(result["status"], 200)
        self.assertEqual(self.count("chat_message_attachments"), 0)
        self.assert_error(self.send(session, "旧消息正文", [material["materialId"]], revision=rev), 404, "session_not_found")
        self.assert_error(self.request("POST", "/api/chat/sessions", data={"requestId": "chat-create"}), 404, "session_not_found")
        self.assertEqual(self.count("sessions"), 0)

    def test_old_api_and_old_current_draft_remain_compatible(self):
        legacy = self.new_session("已明确的后端岗位")
        self.material(legacy)
        before = self.conversation(legacy)
        self.assertEqual(before["session"]["directionStatus"], "provided")
        self.assertEqual(before["session"]["targetDirection"], "已明确的后端岗位")
        self.assertTrue(before["draft"]["markdown"])
        self.send(legacy, "不要改", request_id="raw-control")
        self.assertEqual(self.conversation(legacy)["draft"], before["draft"])
        self.assertEqual(self.count("source_blocks"), 1)
        old_result = self.request("POST", f"/api/sessions/{legacy['sessionId']}/messages", data={"requestId": "old-client", "expectedStateRevision": self.version(legacy["sessionId"]), "content": "旧调用仍能补充", "kind": "supplement"})
        self.assertEqual(old_result["status"], 201)
        self.assertEqual(self.count("source_blocks"), 2)

    def test_mock_history_is_readable_but_new_chat_cannot_write_into_it(self):
        mock = self.request("POST", "/api/demo/sessions", data={"requestId": "mock"})["json"]["data"]["session"]
        restored = self.conversation(mock)
        self.assertEqual(restored["session"]["mode"], "mock")
        before = self.count("messages")
        self.assert_error(self.send(mock, "我的真实资料"), 403, "scope_violation")
        self.assertEqual(self.count("messages"), before)

    def test_model_context_cannot_use_chat_user_write_or_history_read(self):
        session = self.chat()
        repo = Repository(self.path)
        context = RequestContext(session["sessionId"], 1, "run-not-user", 1)
        for call in (lambda: save_chat_message(repo, context, {"content": "伪造", "attachmentIds": []}), lambda: repo.get_conversation(context)):
            with self.assertRaises(RepositoryError) as error:
                call()
            self.assertEqual(error.exception.code, "scope_violation")

    def test_delivery_notice_exposes_only_current_unresolved_conflict_without_internal_checklist(self):
        session = self.chat()
        sid = session["sessionId"]
        repo = Repository(self.path)
        context = RequestContext(sid)
        self.assertFalse(self.conversation(session)["draftReviewRequired"])
        repo.save_issues(context, [{"kind": "capability_gap", "summary": "内部能力缺口", "sourceRefs": [], "affectedBlockIds": []}])
        self.assertFalse(self.conversation(session)["draftReviewRequired"])
        conflict = repo.save_issues(context, [{"kind": "conflict", "summary": "内部事实冲突详情", "sourceRefs": [], "affectedBlockIds": []}])[0]
        before = self.snapshot()
        restored = self.conversation(session)
        self.assertTrue(restored["draftReviewRequired"])
        self.assertNotIn("issues", restored)
        self.assertNotIn("内部事实冲突详情", str(restored))
        self.assertEqual(self.snapshot(), before)
        with db.database(self.path) as connection:
            connection.execute("UPDATE issues SET status = 'resolved' WHERE issue_id = ?", (conflict["issueId"],))
        self.assertFalse(self.conversation(session)["draftReviewRequired"])
        with db.database(self.path) as connection:
            connection.execute("UPDATE issues SET status = 'open', resume_generation = 99 WHERE issue_id = ?", (conflict["issueId"],))
        self.assertFalse(self.conversation(session)["draftReviewRequired"])

    def test_changed_evidence_warns_without_replacing_saved_draft_or_original_reference(self):
        session = self.chat()
        saved = self.material(session)["json"]["data"]
        source = saved["sources"][0]
        repo = Repository(self.path)
        context = RequestContext(session["sessionId"])
        before = self.conversation(session)
        self.assertFalse(before["draftReviewRequired"])
        repo.save_source(context, text="纠正后的构造原文", role=source["role"], origin=source["origin"], use_status="active", source_id=source["sourceId"], expected_source_revision=1)
        restored = self.conversation(session)
        self.assertTrue(restored["draftReviewRequired"])
        self.assertEqual(restored["draft"], before["draft"])
        old = repo.read_source_snapshot(context, source["sourceId"], 1)
        self.assertEqual(old["source"]["text"], source["text"])
        self.assertTrue(old["historical"])

    def test_v4_upgrade_preserves_original_data_and_marks_legacy_direction_provided(self):
        with db.database(":memory:") as connection:
            with patch.object(db, "MIGRATIONS", db.MIGRATIONS[:4]), patch.object(db, "SCHEMA_VERSION", 4):
                db.initialize_database(connection)
                connection.execute("INSERT INTO sessions(session_id, direction) VALUES ('legacy', '原方向')")
                connection.execute("INSERT INTO messages(message_id, session_id, role, content) VALUES ('old', 'legacy', 'user', '旧原话')")
            db.initialize_database(connection)
            row = connection.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(row["direction"], "原方向")
            self.assertEqual(row["direction_status"], "provided")
            self.assertIsNone(row["chat_title"])
            message = connection.execute("SELECT * FROM messages").fetchone()
            self.assertEqual(message["content"], "旧原话")
            self.assertEqual(message["input_channel"], "legacy")
            self.assertEqual(connection.execute("SELECT count(*) FROM chat_message_attachments").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
