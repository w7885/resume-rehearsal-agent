"""Fixed mock workflow: real local persistence, no model or external network."""

import unittest
from unittest.mock import patch

from app import db
import test_api


class MockDemoTests(unittest.TestCase):
    setUp = test_api.ApiTests.setUp
    request = test_api.ApiTests.request
    new_session = test_api.ApiTests.new_session
    count = test_api.ApiTests.count
    assert_error = test_api.ApiTests.assert_error

    def demo(self):
        result = self.request("POST", "/api/demo/sessions", data={"requestId": "demo-create"})
        self.assertEqual(result["status"], 201, result)
        return result["json"]["data"]

    def analyze(self, detail, request_id="demo-analyze"):
        return self.request("POST", f"/api/sessions/{detail['session']['sessionId']}/mock-analysis", data={
            "requestId": request_id, "expectedStateRevision": detail["session"]["stateRevision"]})

    def test_mock_creation_is_atomic_labeled_and_replayable_without_duplicate_materials(self):
        first = self.demo()
        second = self.demo()
        self.assertEqual(first, second)
        self.assertEqual(first["session"]["mode"], "mock")
        self.assertEqual(self.count("sessions"), 1)
        self.assertEqual(self.count("attachments"), 2)
        self.assertEqual(first["suggestions"], [])
        self.assertEqual(first["events"], [])
        self.assertIn("协助页面开发", first["draft"]["markdown"])
        sessions = self.request("GET", "/api/sessions")["json"]["data"]
        self.assertEqual(sessions[0]["mode"], "mock")

    def test_fixed_tools_persist_pending_suggestion_and_events_but_do_not_change_draft(self):
        before = self.demo()
        result = self.analyze(before)
        self.assertEqual(result["status"], 200, result)
        after = result["json"]["data"]
        self.assertEqual(after["draft"], before["draft"])
        self.assertEqual(len(after["suggestions"]), 1)
        self.assertEqual(after["suggestions"][0]["status"], "pending")
        self.assertEqual({e["mode"] for e in after["events"]}, {"mock"})
        self.assertIn("question", {e["type"] for e in after["events"]})
        self.assertTrue(after["issues"][0]["isCurrent"])
        restored = self.request("GET", f"/api/sessions/{after['session']['sessionId']}")["json"]["data"]
        self.assertEqual(restored, after)
        self.assertEqual(self.analyze(before)["json"], result["json"])
        self.assertEqual(self.count("suggestions"), 1)
        self.assert_error(self.analyze(after, "another-analysis"), 409, "stale_state")

    def test_mock_suggestion_accept_uses_real_decision_transaction_and_evidence_survives(self):
        demo = self.demo()
        analyzed = self.analyze(demo)["json"]["data"]
        sid = analyzed["session"]["sessionId"]
        suggestion = analyzed["suggestions"][0]
        payload = {"requestId": "accept", "expectedStateRevision": analyzed["session"]["stateRevision"],
                   "expectedBlockRevision": suggestion["target"]["expectedBlockRevision"], "decision": "accept"}
        result = self.request("POST", f"/api/sessions/{sid}/suggestions/{suggestion['suggestionId']}/decision", data=payload)
        self.assertEqual(result["status"], 200, result)
        self.assertEqual(result["json"]["data"]["draft"]["markdown"], suggestion["after"])
        ref = suggestion["sourceRefs"][0]
        source = self.request("GET", f"/api/sessions/{sid}/sources/{ref['sourceId']}/revisions/{ref['sourceRevision']}")
        self.assertEqual(source["status"], 200)
        self.assertIn(ref["quote"], source["json"]["data"]["source"]["text"])

    def test_mock_endpoint_refuses_real_session_and_user_cannot_choose_mock_mode(self):
        real = self.new_session()
        before = self.count("draft_blocks")
        self.assert_error(self.analyze({"session": real}), 403, "scope_violation")
        self.assertEqual(self.count("draft_blocks"), before)
        self.assertEqual(self.count("runs"), 0)
        self.assert_error(self.request("POST", "/api/sessions", data={"requestId": "spoof", "direction": "真实", "mode": "mock"}), 422, "invalid_input")
        self.assertEqual(self.count("sessions"), 1)

    def test_modified_demo_draft_cannot_receive_fixed_rewrite(self):
        detail = self.demo()
        sid = detail["session"]["sessionId"]
        self.request("PUT", f"/api/sessions/{sid}/draft", data={"requestId": "edit", "expectedStateRevision": detail["session"]["stateRevision"], "markdown": "我的新内容"})
        current = self.request("GET", f"/api/sessions/{sid}")["json"]["data"]
        self.assert_error(self.analyze(current), 409, "stale_state")
        self.assertEqual(self.count("suggestions"), 0)

    def test_failed_mock_analysis_rolls_back_version_tools_runs_and_events(self):
        detail = self.demo()
        with db.database(self.path) as connection:
            connection.execute("CREATE TRIGGER fail_events BEFORE INSERT ON run_events BEGIN SELECT RAISE(ABORT, 'injected'); END")
            before = list(connection.iterdump())
        self.assert_error(self.analyze(detail), 503, "storage_failed")
        with db.database(self.path, read_only=True) as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_deleted_demo_cannot_be_recreated_by_creation_or_analysis_replay(self):
        detail = self.demo()
        analyzed = self.analyze(detail)["json"]["data"]
        sid = detail["session"]["sessionId"]
        deleted = self.request("DELETE", f"/api/sessions/{sid}", data={"requestId": "delete", "expectedStateRevision": analyzed["session"]["stateRevision"], "confirmed": True})
        self.assertEqual(deleted["status"], 200)
        self.assert_error(self.request("POST", "/api/demo/sessions", data={"requestId": "demo-create"}), 404, "session_not_found")
        self.assert_error(self.analyze(detail), 404, "session_not_found")
        self.assertEqual(self.count("sessions"), 0)

    def test_v3_migration_preserves_session_and_defaults_to_local_mode(self):
        with db.database(":memory:") as connection:
            with patch.object(db, "MIGRATIONS", db.MIGRATIONS[:3]), patch.object(db, "SCHEMA_VERSION", 3):
                db.initialize_database(connection)
                connection.execute("INSERT INTO sessions(session_id, direction) VALUES ('old-session', '原来的方向')")
            report = db.initialize_database(connection)
            self.assertEqual(report["schemaVersion"], 4)
            row = connection.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(row["direction"], "原来的方向")
            self.assertEqual(row["mode"], "local")

    def test_cleared_draft_does_not_make_new_resume_upload_an_implicit_first_import(self):
        detail = self.demo()
        sid = detail["session"]["sessionId"]
        edited = self.request("PUT", f"/api/sessions/{sid}/draft", data={"requestId": "clear", "expectedStateRevision": detail["session"]["stateRevision"], "markdown": ""})["json"]["data"]
        upload = self.request("POST", f"/api/sessions/{sid}/materials", data={"requestId": "candidate", "expectedStateRevision": edited["stateRevision"], "name": "新版.md", "role": "resume", "text": "完整新版，仍须明确替换"})
        self.assertEqual(upload["status"], 201, upload)
        self.assertFalse(upload["json"]["data"]["draftCreated"])
        self.assertTrue(upload["json"]["data"]["needsClarification"])
        restored = self.request("GET", f"/api/sessions/{sid}")["json"]["data"]
        self.assertEqual(restored["draft"]["markdown"], "")
        self.assertEqual(restored["materials"][-1]["useStatus"], "pending")


if __name__ == "__main__":
    unittest.main()
