"""HTTP/ASGI integration tests without external network or a model provider."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from urllib.parse import urlencode, urlsplit
import unittest
from unittest.mock import patch
import zipfile

from app.api import MAX_JSON_BYTES, MAX_UPLOAD_BYTES
from app.business_service import BusinessService
from app.db import database, transaction
from app.main import create_app
from app.material_tools import MaterialTools
from app.repository import Repository, RequestContext
from test_db import temporary_directory


async def request_asgi(app, method, url, *, data=None, body=None, headers=None, chunks=None):
    parsed = urlsplit(url)
    values = {"host": "127.0.0.1:3001"}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        values["content-type"] = "application/json"
    body = b"" if body is None else body
    values["content-length"] = str(len(body))
    values.update(headers or {})
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "scheme": "http",
             "method": method, "path": parsed.path, "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(),
             "root_path": "", "server": ("127.0.0.1", 3001), "client": ("127.0.0.1", 12345),
             "headers": [(key.lower().encode(), value.encode("latin-1")) for key, value in values.items()]}
    parts = list(chunks) if chunks is not None else [body]
    responses = []

    async def receive():
        if parts:
            return {"type": "http.request", "body": parts.pop(0), "more_body": bool(parts)}
        return {"type": "http.disconnect"}

    async def send(message):
        responses.append(message)

    try:
        await app(scope, receive, send)
    except Exception:
        # Equivalent to observing an HTTP 500 response after server middleware
        # has handled/logged an internal failure; never fabricate a response.
        if not any(message["type"] == "http.response.start" for message in responses):
            raise
    start = next(message for message in responses if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in responses if message["type"] == "http.response.body")
    result = {"status": start["status"], "headers": dict(start["headers"]), "body": raw}
    if result["headers"].get(b"content-type", b"").startswith(b"application/json"):
        result["json"] = json.loads(raw)
    return result


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.path = self.root / "api.sqlite"
        self.runner = asyncio.Runner()
        self.addCleanup(self.runner.close)
        self.app = create_app(self.path)
        self.lifecycle = self.app.router.lifespan_context(self.app)
        self.runner.run(self.lifecycle.__aenter__())
        self.addCleanup(lambda: self.runner.run(self.lifecycle.__aexit__(None, None, None)))
        self.sequence = 0

    def request(self, method, path, **kwargs):
        return self.runner.run(request_asgi(self.app, method, path, **kwargs))

    def new_session(self, direction="后端实习"):
        self.sequence += 1
        result = self.request("POST", "/api/sessions", data={"direction": direction, "requestId": f"create-{self.sequence}"})
        self.assertEqual(result["status"], 201, result)
        return result["json"]["data"]

    def version(self, session_id):
        return self.request("GET", f"/api/sessions/{session_id}")["json"]["data"]["session"]["stateRevision"]

    def message(self, session, content="用户补充", request_id="message-1", revision=None, **extra):
        revision = self.version(session["sessionId"]) if revision is None else revision
        return self.request("POST", f"/api/sessions/{session['sessionId']}/messages", data={
            "content": content, "requestId": request_id, "expectedStateRevision": revision, **extra})

    def material(self, session, text="# 构造简历\n负责需求整理", request_id="material-1", role="resume", **extra):
        return self.request("POST", f"/api/sessions/{session['sessionId']}/materials", data={
            "text": text, "requestId": request_id, "expectedStateRevision": self.version(session["sessionId"]), "role": role, **extra})

    def count(self, table):
        with database(self.path, read_only=True) as connection:
            return connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

    def assert_error(self, result, status, code):
        self.assertEqual(result["status"], status, result)
        self.assertFalse(result["json"]["ok"])
        self.assertEqual(result["json"]["error"]["code"], code)
        self.assertNotIn("data", result["json"])

    def test_health_has_no_secret_and_returns_configuration_only(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-secret-not-for-output"}):
            result = self.request("GET", "/api/health")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"], {"status": "ok", "model_configured": True})
        self.assertNotIn(b"test-secret", result["body"])

    def test_create_list_restore_and_missing_session(self):
        created = self.new_session()
        listing = self.request("GET", "/api/sessions")
        self.assertEqual(listing["json"]["data"], [created])
        detail = self.request("GET", f"/api/sessions/{created['sessionId']}")["json"]["data"]
        self.assertEqual(detail["session"], created)
        self.assertEqual(detail["messages"], [])
        self.assertEqual(detail["draft"]["blocks"], [])
        self.assert_error(self.request("GET", "/api/sessions/missing"), 404, "session_not_found")

    def test_create_request_is_idempotent_even_after_application_restart(self):
        payload = {"direction": "开发实习", "requestId": "create-stable"}
        first = self.request("POST", "/api/sessions", data=payload)
        restarted = create_app(self.path)
        lifecycle = restarted.router.lifespan_context(restarted)
        self.runner.run(lifecycle.__aenter__())
        try:
            second = self.runner.run(request_asgi(restarted, "POST", "/api/sessions", data=payload))
        finally:
            self.runner.run(lifecycle.__aexit__(None, None, None))
        self.assertEqual(second["json"], first["json"])
        self.assertEqual(self.count("sessions"), 1)

    def test_saved_message_is_user_role_and_has_addressable_user_source(self):
        session = self.new_session()
        result = self.message(session, "我整理了 8 条反馈")
        self.assertEqual(result["status"], 201, result)
        data = result["json"]["data"]
        self.assertEqual(data["message"]["role"], "user")
        self.assertEqual(data["source"]["origin"]["messageId"], data["message"]["messageId"])
        self.assertEqual(data["stateRevision"], session["stateRevision"] + 1)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("source_blocks"), 1)
        self.assertEqual(self.request("GET", f"/api/sessions/{session['sessionId']}/messages")["json"]["data"], [data["message"]])

    def test_skip_is_preserved_as_skip_without_personal_evidence(self):
        session = self.new_session()
        result = self.message(session, "暂时想不起来", kind="skip")
        self.assertEqual(result["status"], 201)
        self.assertEqual(result["json"]["data"]["message"]["kind"], "skip")
        self.assertIsNone(result["json"]["data"]["source"])
        self.assertEqual(self.count("source_blocks"), 0)

    def test_retry_returns_original_result_before_old_version_check(self):
        session = self.new_session()
        first = self.message(session, "同一次输入", revision=1)
        self.message(session, "后来新的输入", request_id="message-2")
        state = self.version(session["sessionId"])
        replay = self.message(session, "同一次输入", revision=1)
        self.assertEqual(replay["json"], first["json"])
        self.assertEqual(self.count("messages"), 2)
        self.assertEqual(self.version(session["sessionId"]), state)

    def test_same_text_new_request_is_two_messages(self):
        session = self.new_session()
        self.message(session, "相同文字", request_id="one")
        self.message(session, "相同文字", request_id="two")
        self.assertEqual(self.count("messages"), 2)

    def test_changed_payload_same_request_and_new_stale_request_conflict(self):
        session = self.new_session()
        self.message(session, "第一次", revision=1)
        self.assert_error(self.message(session, "另一内容", revision=1), 409, "request_conflict")
        self.assert_error(self.message(session, "过期新请求", request_id="new", revision=1), 409, "stale_state")
        self.assertEqual(self.count("messages"), 1)

    def test_invalid_input_types_extra_role_and_bad_json_do_not_write(self):
        session = self.new_session()
        for extra in ({"role": "system-secret-value"}, {"runId": "fake"}, {"sql": "SELECT secret"}, {"sessionId": "other"}):
            result = self.message(session, "有效文字", **extra)
            self.assert_error(result, 422, "invalid_input")
            self.assertNotIn(b"system-secret-value", result["body"])
        for revision in (True, "1", 0, 2**63):
            self.assert_error(self.message(session, revision=revision), 422, "invalid_input")
        self.assert_error(self.message(session, "  "), 422, "invalid_input")
        self.assert_error(self.request("POST", f"/api/sessions/{session['sessionId']}/messages", body=b"{", headers={"content-type": "application/json"}), 422, "invalid_input")
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.version(session["sessionId"]), 1)

    def test_request_log_failure_rolls_back_message_source_and_revision(self):
        session = self.new_session()
        with database(self.path) as connection:
            connection.execute("CREATE TRIGGER fail_request_log BEFORE INSERT ON request_log BEGIN SELECT RAISE(ABORT, 'do-not-expose-private-sql'); END")
        result = self.message(session, "不应部分保存")
        self.assert_error(result, 503, "storage_failed")
        self.assertNotIn(b"do-not-expose", result["body"])
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("source_blocks"), 0)
        self.assertEqual(self.version(session["sessionId"]), 1)

    def test_pasted_resume_builds_initial_draft_atomically(self):
        session = self.new_session()
        result = self.material(session)
        self.assertEqual(result["status"], 201, result)
        data = result["json"]["data"]
        self.assertTrue(data["draftCreated"])
        self.assertFalse(data["needsClarification"])
        self.assertEqual(data["stateRevision"], 2)
        self.assertEqual(data["draftRevision"], 2)
        draft = self.request("GET", f"/api/sessions/{session['sessionId']}/draft")["json"]["data"]
        self.assertEqual(draft["blocks"][0]["markdown"], "# 构造简历\n负责需求整理")
        material_id = data["material"]["materialId"]
        detail = self.request("GET", f"/api/sessions/{session['sessionId']}/materials/{material_id}")["json"]["data"]
        self.assertEqual(detail["sources"], data["sources"])

    def test_ambiguous_and_second_resume_do_not_overwrite_or_join_evidence(self):
        session = self.new_session()
        first = self.material(session)
        before = self.request("GET", f"/api/sessions/{session['sessionId']}/draft")["json"]
        second = self.material(session, "另一版简历：负责全部统筹", request_id="second")
        self.assertTrue(second["json"]["data"]["needsClarification"])
        self.assertFalse(second["json"]["data"]["draftCreated"])
        self.assertEqual(self.request("GET", f"/api/sessions/{session['sessionId']}/draft")["json"], before)
        tools = MaterialTools(Repository(self.path), RequestContext(session["sessionId"]))
        self.assertEqual(tools.dispatch("search_evidence", {"query": "全部统筹"})["data"], [])
        self.assertTrue(first["json"]["data"]["draftCreated"])

    def test_unknown_role_material_is_saved_pending_without_forced_classification(self):
        session = self.new_session()
        result = self.request("POST", f"/api/sessions/{session['sessionId']}/materials", data={
            "requestId": "unknown", "expectedStateRevision": 1, "text": "未明确用途的文字"})
        self.assertEqual(result["status"], 201)
        self.assertEqual(result["json"]["data"]["material"]["useStatus"], "pending")
        self.assertFalse(result["json"]["data"]["draftCreated"])
        self.assertTrue(result["json"]["data"]["needsClarification"])

    def test_material_retry_does_not_duplicate_bytes_sources_or_draft(self):
        session = self.new_session()
        payload = {"requestId": "upload", "expectedStateRevision": 1, "role": "resume", "text": "简历原文"}
        first = self.request("POST", f"/api/sessions/{session['sessionId']}/materials", data=payload)
        second = self.request("POST", f"/api/sessions/{session['sessionId']}/materials", data=payload)
        self.assertEqual(first["json"], second["json"])
        self.assertEqual(self.count("attachments"), 1)
        self.assertEqual(self.count("source_blocks"), 1)
        self.assertEqual(self.count("draft_blocks"), 1)
        self.assertEqual(self.version(session["sessionId"]), 2)

    def test_originals_survive_even_when_documents_fail_parsing_and_images_remain_pending(self):
        session = self.new_session()
        docx = BytesIO()
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        samples = [("截图.png", "image/png", b"\x89PNG\r\n\x1a\nsynthetic"),
                   ("简历.pdf", "application/pdf", b"%PDF-synthetic"),
                   ("简历.jpg", "image/jpeg", b"\xff\xd8\xffsynthetic"),
                   ("简历.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx.getvalue())]
        for index, (name, media_type, raw) in enumerate(samples):
            params = urlencode({"name": name, "requestId": f"file-{index}", "expectedStateRevision": self.version(session["sessionId"])})
            result = self.request("POST", f"/api/sessions/{session['sessionId']}/materials?{params}", body=raw, headers={"content-type": media_type})
            self.assertEqual(result["status"], 201, result)
            material = result["json"]["data"]["material"]
            self.assertEqual(material["parseStatus"], "failed" if name.endswith((".docx", ".pdf")) else "pending")
            self.assertEqual(result["json"]["data"]["sources"], [])
            download = self.request("GET", f"/api/sessions/{session['sessionId']}/materials/{material['materialId']}/file")
            self.assertEqual(download["body"], raw)
            self.assertEqual(download["headers"][b"x-content-type-options"], b"nosniff")
            self.assertIn(b"attachment;", download["headers"][b"content-disposition"])

    def test_bad_format_mime_and_filename_are_rejected_without_saved_rows(self):
        session = self.new_session()
        for index, (name, media_type, raw, status) in enumerate([
            ("old.doc", "application/octet-stream", b"old", 415),
            ("fake.png", "image/png", b"not png", 422),
            ("wrong.md", "application/pdf", b"markdown", 422),
            ("../../escape.md", "text/markdown", b"text", 422),
            ("bad.docx", "application/octet-stream", b"not zip", 422),
        ]):
            query = urlencode({"name": name, "requestId": f"bad-{index}", "expectedStateRevision": 1})
            result = self.request("POST", f"/api/sessions/{session['sessionId']}/materials?{query}", body=raw, headers={"content-type": media_type})
            self.assertEqual(result["status"], status, result)
        self.assertEqual(self.count("attachments"), 0)
        self.assertEqual(self.version(session["sessionId"]), 1)

    def test_delete_scrubs_history_and_replays_without_recreating_session(self):
        session = self.new_session("私密岗位名称")
        other = self.new_session("保留另一个会话")
        self.message(session, "私密履历正文", request_id="sensitive-message")
        material = self.material(session, "私密简历正文", request_id="sensitive-file")
        old_source = material["json"]["data"]["sources"][0]
        original = self.root / "original.md"
        budget = self.root / "independent-budget.json"
        original.write_text("用户原文件", encoding="utf-8")
        budget.write_text('{"spent": 100}', encoding="utf-8")
        version = self.version(session["sessionId"])
        with database(self.path) as connection:
            connection.execute("INSERT INTO runs(run_id,session_id,generation,input_state_revision,resume_generation,status) VALUES ('old-run',?,1,?,1,'running')", (session["sessionId"], version))
        context = RequestContext(session["sessionId"], version, "old-run", 1)
        payload = {"requestId": "delete-once", "expectedStateRevision": version, "confirmed": True}
        deleted = self.request("DELETE", f"/api/sessions/{session['sessionId']}", data=payload)
        self.assertEqual(deleted["status"], 200, deleted)
        self.assertEqual(self.request("DELETE", f"/api/sessions/{session['sessionId']}", data=payload)["json"], deleted["json"])
        self.assert_error(self.request("GET", f"/api/sessions/{session['sessionId']}"), 404, "session_not_found")
        self.assertEqual(self.request("GET", f"/api/sessions/{other['sessionId']}")["status"], 200)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("attachments"), 0)
        self.assertEqual(self.count("source_blocks"), 0)
        self.assertEqual(self.count("runs"), 0)
        self.assertEqual(original.read_text(encoding="utf-8"), "用户原文件")
        self.assertEqual(budget.read_text(encoding="utf-8"), '{"spent": 100}')
        self.assertFalse(MaterialTools(Repository(self.path), context).dispatch("read_material", {"sourceIds": [old_source["sourceId"]]})["ok"])
        old_create = self.request("POST", "/api/sessions", data={"direction": "私密岗位名称", "requestId": "create-1"})
        self.assert_error(old_create, 404, "session_not_found")
        with database(self.path, read_only=True) as connection:
            requests = [dict(row) for row in connection.execute("SELECT * FROM request_log")]
        self.assertNotIn("私密", json.dumps(requests, ensure_ascii=False))
        for row in requests:
            if row["state"] == "deleted":
                self.assertIsNone(row["response_json"])
                self.assertIsNone(row["request_hash"])

    def test_delete_needs_confirmation_and_current_version(self):
        session = self.new_session()
        self.assert_error(self.request("DELETE", f"/api/sessions/{session['sessionId']}", data={"requestId": "no", "expectedStateRevision": 1, "confirmed": False}), 422, "invalid_input")
        self.assert_error(self.request("DELETE", f"/api/sessions/{session['sessionId']}", data={"requestId": "stale", "expectedStateRevision": 99, "confirmed": True}), 409, "stale_state")
        self.assertEqual(self.count("sessions"), 1)

    def test_other_session_cannot_read_material_or_snapshot(self):
        session = self.new_session()
        other = self.new_session()
        data = self.material(session)["json"]["data"]
        mid = data["material"]["materialId"]
        source = data["sources"][0]
        self.assert_error(self.request("GET", f"/api/sessions/{other['sessionId']}/materials/{mid}/file"), 404, "source_not_found")
        self.assert_error(self.request("GET", f"/api/sessions/{other['sessionId']}/sources/{source['sourceId']}/revisions/1"), 403, "scope_violation")

    def test_unknown_routes_and_empty_mutations_do_not_fake_success(self):
        session = self.new_session()
        self.assert_error(self.request("PUT", f"/api/sessions/{session['sessionId']}/draft", data={}), 422, "invalid_input")
        self.assert_error(self.request("POST", f"/api/sessions/{session['sessionId']}/suggestions/x/decision", data={}), 422, "invalid_input")
        self.assert_error(self.request("GET", "/api/unknown"), 404, "http_error")
        self.assertEqual(self.count("draft_blocks"), 0)

    def test_cross_site_and_rebinding_host_requests_rejected(self):
        for headers in ({"origin": "https://untrusted.example"}, {"host": "untrusted.example"}, {"sec-fetch-site": "cross-site"}):
            self.assert_error(self.request("POST", "/api/sessions", data={"requestId": "blocked", "direction": "x"}, headers=headers), 403, "scope_violation")
        good = self.request("POST", "/api/sessions", data={"requestId": "local", "direction": "x"}, headers={"origin": "http://127.0.0.1:5173"})
        self.assertEqual(good["status"], 201)

    def test_body_limits_enforced_for_declared_and_streamed_lengths(self):
        self.assert_error(self.request("POST", "/api/sessions", body=b"{}", headers={"content-type": "application/json", "content-length": "9" * 5000}), 413, "invalid_input")
        result = self.request("POST", "/api/sessions", body=b"{}", headers={"content-type": "application/json", "content-length": str(MAX_JSON_BYTES + 1)})
        self.assert_error(result, 413, "invalid_input")
        result = self.request("POST", "/api/sessions", body=b"{}", chunks=[b"x" * MAX_JSON_BYTES, b"x"], headers={"content-type": "application/json", "content-length": "2"})
        self.assert_error(result, 413, "invalid_input")
        result = self.request("POST", "/api/sessions/x/materials", body=b"x", headers={"content-type": "application/pdf", "content-length": str(MAX_UPLOAD_BYTES + 1)})
        self.assert_error(result, 413, "invalid_input")
        self.assertEqual(self.count("sessions"), 0)

    def test_concurrent_identical_operations_commit_once(self):
        session = self.new_session()
        payload = {"requestId": "concurrent", "expectedStateRevision": 1, "content": "并发重试", "kind": "message"}
        service = BusinessService(Repository(self.path))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: service.save_message(session["sessionId"], payload), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.version(session["sessionId"]), 2)


if __name__ == "__main__":
    unittest.main()
