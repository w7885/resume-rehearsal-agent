"""A-T07 integration checks using isolated SQLite/ASGI and synthetic text."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import unittest
from unittest.mock import patch

from app.business_service import BusinessService
from app.db import database, transaction
from app.draft_actions import split_markdown
from app.material_tools import MaterialTools
from app.repository import Repository, RepositoryError, RequestContext
import test_api


class DraftActionsTests(unittest.TestCase):
    setUp = test_api.ApiTests.setUp
    request = test_api.ApiTests.request
    new_session = test_api.ApiTests.new_session
    version = test_api.ApiTests.version
    material = test_api.ApiTests.material
    message = test_api.ApiTests.message
    count = test_api.ApiTests.count
    assert_error = test_api.ApiTests.assert_error

    def setup_case(self, blocks=None):
        session = self.new_session()
        self.sid = session["sessionId"]
        self.base = f"/api/sessions/{self.sid}"
        self.repo = Repository(self.path)
        self.ctx = RequestContext(self.sid)
        uploaded = self.material(session, text="负责需求整理；整理 8 条反馈", role="supplement")["json"]["data"]
        self.ref = {"sourceId": uploaded["sources"][0]["sourceId"], "sourceRevision": 1, "quote": "整理 8 条反馈"}
        blocks = blocks or ["# 构造简历\n\n", "负责需求整理\n\n", "其他经历"]
        self.repo.create_initial_draft(self.ctx, [{"markdown": text, "sourceRefs": [self.ref]} for text in blocks])
        return session

    def draft(self):
        return self.repo.get_draft(self.ctx)

    def proposal(self, operation="replace", index=1, after="整理并归类 8 条反馈\n\n", **extra):
        block = self.draft()["blocks"][index]
        return self.repo.save_suggestion(self.ctx, {"operation": operation,
            "target": {"blockId": block["blockId"], "expectedBlockRevision": block["blockRevision"]},
            "before": block["markdown"], "after": "" if operation == "delete" else after,
            "reason": "根据构造材料补充具体行动", "sourceRefs": [self.ref], "gaps": [], **extra})

    def decision_payload(self, suggestion, decision="accept", request_id="decision-1", **extra):
        return {"decision": decision, "requestId": request_id, "expectedStateRevision": self.version(self.sid),
                "expectedBlockRevision": suggestion["target"]["expectedBlockRevision"], **extra}

    def decide(self, suggestion, payload):
        return self.request("POST", self.base + f"/suggestions/{suggestion['suggestionId']}/decision", data=payload)

    def edit(self, markdown, **extra):
        return self.request("PUT", self.base + "/draft", data={"markdown": markdown,
            "requestId": "edit-1", "expectedStateRevision": self.version(self.sid), **extra})

    def replacement(self, session):
        candidate = self.material(session, text="# 完整构造新版\n只保留新版经历", request_id="candidate")["json"]["data"]
        auth = self.message(session, content="以这份完整新版为准，替换当前稿；同人同岗位，旧改动不迁入", request_id="authorization")["json"]["data"]["message"]
        return {"requestId": "replacement-1", "expectedStateRevision": self.version(self.sid),
                "expectedDraftRevision": self.draft()["draftRevision"], "materialId": candidate["material"]["materialId"],
                "authorizationMessageId": auth["messageId"], "confirmed": True, "completeConfirmed": True}

    def run_record(self):
        state = self.repo.get_session(self.ctx)
        with database(self.path) as connection, transaction(connection):
            connection.execute("INSERT INTO runs(run_id, session_id, generation, input_state_revision, resume_generation, status) VALUES ('run-new', ?, 1, ?, ?, 'running')",
                               (self.sid, state["stateRevision"], state["currentResumeGeneration"]))
        return RequestContext(self.sid, state["stateRevision"], "run-new", 1)

    def snapshot(self):
        with database(self.path, read_only=True) as connection:
            return list(connection.iterdump())

    def test_accept_only_target_and_preserves_exact_evidence_history(self):
        self.setup_case()
        before = self.draft()
        proposal = self.proposal()
        self.assertEqual(self.draft(), before)
        payload = self.decision_payload(proposal)
        run = self.run_record()
        response = self.decide(proposal, payload)
        self.assertEqual(response["status"], 200, response)
        result = response["json"]["data"]
        blocks = result["draft"]["blocks"]
        self.assertEqual(blocks[0], before["blocks"][0])
        self.assertEqual(blocks[2], before["blocks"][2])
        self.assertEqual(blocks[1]["markdown"], proposal["after"])
        self.assertEqual(blocks[1]["sourceRefs"], [self.ref])
        self.assertEqual(blocks[1]["blockRevision"], before["blocks"][1]["blockRevision"] + 1)
        self.assertEqual(result["stateRevision"], payload["expectedStateRevision"] + 1)
        self.assertEqual(result["draftRevision"], before["draftRevision"] + 1)
        self.assertEqual(result["suggestion"]["status"], "accepted")
        with self.assertRaises(RepositoryError):
            self.repo.get_session_context(run)
        with database(self.path, read_only=True) as connection:
            row = connection.execute("SELECT markdown FROM draft_blocks WHERE block_id = ? AND block_revision = 1", (blocks[1]["blockId"],)).fetchone()
            self.assertEqual(row[0], before["blocks"][1]["markdown"])

    def test_insert_replay_and_different_request_cannot_duplicate(self):
        self.setup_case()
        proposal = self.proposal("insert_after")
        sibling = self.proposal("insert_after")
        payload = self.decision_payload(proposal)
        result = self.decide(proposal, payload)
        self.assertEqual(result["status"], 200)
        self.assertEqual(len(self.draft()["blocks"]), 4)
        self.assertEqual(self.draft()["blocks"][2]["markdown"], proposal["after"])
        self.assertEqual(self.decide(proposal, payload)["json"], result["json"])
        self.assert_error(self.decide(proposal, self.decision_payload(proposal, request_id="another")), 409, "stale_state")
        self.assert_error(self.decide(sibling, self.decision_payload(sibling, request_id="sibling")), 409, "stale_state")
        self.assertEqual(len(self.draft()["blocks"]), 4)

    def test_independent_blocks_have_safe_markdown_boundaries(self):
        self.setup_case(["第一段", "第二段", "第三段"])
        self.assertEqual(self.draft()["markdown"], "第一段\n\n第二段\n\n第三段")
        proposal = self.proposal("insert_after", after="插入内容")
        self.assertEqual(self.decide(proposal, self.decision_payload(proposal))["status"], 200)
        self.assertEqual(self.draft()["markdown"], "第一段\n\n第二段\n\n插入内容\n\n第三段")

    def test_delete_keeps_other_blocks_and_history(self):
        self.setup_case()
        before = self.draft()["blocks"]
        proposal = self.proposal("delete")
        self.assertEqual(self.decide(proposal, self.decision_payload(proposal))["status"], 200)
        self.assertEqual(self.draft()["blocks"], [before[0], before[2]])
        self.assertEqual(self.repo.get_suggestion(self.ctx, proposal["suggestionId"])["before"], before[1]["markdown"])

    def test_reject_and_revise_keep_draft_and_expose_feedback_to_host(self):
        self.setup_case()
        before = self.draft()
        first, second = self.proposal(), self.proposal()
        for suggestion, decision, feedback in ((first, "reject", "不采用"), (second, "revise", "请保留协助的职责边界")):
            result = self.decide(suggestion, self.decision_payload(suggestion, decision=decision, request_id=decision, feedback=feedback))
            self.assertEqual(result["status"], 200, result)
            self.assertEqual(self.draft(), before)
        context = self.repo.get_session_context(self.ctx)
        self.assertEqual(len(context["decisionFeedback"]), 2)
        self.assertEqual(context["decisionFeedback"][1]["decision"]["feedback"], "请保留协助的职责边界")
        self.assertEqual(self.count("source_blocks"), 1)
        new = self.proposal(supersedesSuggestionId=second["suggestionId"])
        self.assertEqual(new["supersedesSuggestionId"], second["suggestionId"])
        self.assertEqual(new["status"], "pending")
        self.assertEqual(self.draft(), before)

    def test_missing_feedback_invalid_types_extra_fields_and_stale_block_rejected(self):
        self.setup_case()
        proposal = self.proposal()
        for extra in ({"decision": "revise"}, {"decision": "revise", "feedback": "  "}, {"expectedBlockRevision": True},
                      {"expectedStateRevision": "3"}, {"status": "accepted"}):
            payload = self.decision_payload(proposal, **extra)
            self.assert_error(self.decide(proposal, payload), 422, "invalid_input")
        payload = self.decision_payload(proposal, expectedBlockRevision=2)
        self.assert_error(self.decide(proposal, payload), 409, "stale_state")

    def test_manual_edit_preserves_unique_unchanged_blocks_and_invalidates_changed(self):
        self.setup_case()
        before = self.draft()["blocks"]
        affected, unaffected = self.proposal(), self.proposal(index=2)
        old_payload = self.decision_payload(affected)
        result = self.edit("# 构造简历\n\n协助整理，数量待核实\n\n其他经历")
        self.assertEqual(result["status"], 200)
        blocks = self.draft()["blocks"]
        self.assertEqual(blocks[0], before[0])
        self.assertEqual(blocks[2], before[2])
        self.assertNotEqual(blocks[1]["blockId"], before[1]["blockId"])
        self.assertEqual(blocks[1]["sourceRefs"], [])
        self.assert_error(self.decide(affected, old_payload), 409, "stale_state")
        self.assert_error(self.decide(affected, self.decision_payload(affected, request_id="fresh-version")), 409, "stale_state")
        self.assertEqual(self.decide(unaffected, self.decision_payload(unaffected, request_id="unaffected"))["status"], 200)

    def test_duplicate_paragraphs_do_not_inherit_identity_or_evidence(self):
        self.setup_case(["重复段落\n\n", "重复段落\n\n", "结尾"])
        before = self.draft()["blocks"]
        proposal = self.proposal(index=0)
        self.assertEqual(self.edit("重复段落\n\n重复段落\n\n新结尾")["status"], 200)
        after = self.draft()["blocks"]
        self.assertTrue(all(block["blockId"] not in {old["blockId"] for old in before} for block in after))
        self.assertTrue(all(block["sourceRefs"] == [] for block in after))
        self.assertEqual(self.repo.get_suggestion(self.ctx, proposal["suggestionId"])["status"], "invalidated")

    def test_split_markdown_is_lossless_with_fences_nested_lists_and_crlf(self):
        for text in ("# 标题\r\n\r\n正文\r\n", "- 一级\n\n  - 嵌套\n    继续\n\n下一段", "```python\n\n# 代码标题\n```\n\n正文", "", "\n\n"):
            with self.subTest(text=text):
                parts = split_markdown(text)
                self.assertEqual("".join(parts), text)
        self.assertEqual(len(split_markdown("```\n\n# 代码\n```\n\n正文")), 2)
        self.assertEqual(len(split_markdown("- 一级\n\n  - 嵌套\n\n下一段")), 2)

    def test_empty_and_unchanged_edit_have_correct_version_behavior(self):
        self.setup_case()
        before = self.repo.get_session(self.ctx)
        self.assertEqual(self.edit("".join(b["markdown"] for b in self.draft()["blocks"]))["status"], 200)
        self.assertEqual(self.repo.get_session(self.ctx), before)
        result = self.edit("", requestId="clear")
        self.assertEqual(result["status"], 200)
        self.assertEqual(self.draft()["blocks"], [])

    def test_accept_failure_after_block_write_rolls_back_everything_and_can_retry(self):
        self.setup_case()
        proposal = self.proposal()
        payload = self.decision_payload(proposal)
        self.run_record()
        before = self.snapshot()
        from app import draft_actions
        original = draft_actions._write_block
        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("injected storage failure")
        with patch.object(draft_actions, "_write_block", fail_after_write):
            self.assert_error(self.decide(proposal, payload), 503, "storage_failed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.decide(proposal, payload)["status"], 200)

    def test_edit_request_cache_failure_rolls_back_draft_and_invalidations(self):
        self.setup_case()
        self.proposal()
        self.run_record()
        with database(self.path) as connection:
            connection.execute("CREATE TRIGGER fail_request BEFORE INSERT ON request_log BEGIN SELECT RAISE(ABORT, 'injected'); END")
        before = self.snapshot()
        self.assert_error(self.edit("新正文"), 503, "storage_failed")
        self.assertEqual(self.snapshot(), before)

    def test_evidence_insert_failure_reverts_new_text_and_keeps_pending(self):
        self.setup_case()
        proposal = self.proposal()
        payload = self.decision_payload(proposal)
        with database(self.path) as connection:
            connection.execute("CREATE TRIGGER fail_evidence BEFORE INSERT ON draft_sources BEGIN SELECT RAISE(ABORT, 'injected'); END")
        before = self.snapshot()
        self.assert_error(self.decide(proposal, payload), 503, "storage_failed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.repo.get_suggestion(self.ctx, proposal["suggestionId"])["status"], "pending")

    def test_corrected_source_revision_blocks_old_suggestion_with_fresh_request_version(self):
        self.setup_case()
        proposal = self.proposal()
        source = self.repo.read_material(self.ctx, [self.ref["sourceId"]])[0]
        self.repo.save_source(self.ctx, text="数量尚待核实", role="supplement", origin=source["origin"], use_status="active",
                              source_id=source["sourceId"], expected_source_revision=1)
        before = self.draft()
        self.assert_error(self.decide(proposal, self.decision_payload(proposal)), 409, "stale_state")
        self.assertEqual(self.draft(), before)
        self.assertTrue(self.repo.read_source_snapshot(self.ctx, source["sourceId"], 1)["historical"])

    def test_replacement_switches_scope_preserves_jd_history_and_interrupts_run(self):
        session = self.setup_case()
        accepted = self.proposal()
        self.decide(accepted, self.decision_payload(accepted))
        pending = self.proposal()
        jd = self.material(session, text="岗位要求 Python", role="jd", request_id="jd")["json"]["data"]["sources"][0]
        payload = self.replacement(session)
        self.repo.save_issues(self.ctx, [{"kind": "conflict", "summary": "构造待处理问题", "sourceRefs": [self.ref], "affectedBlockIds": []}])
        run = self.run_record()
        before = self.draft()
        response = self.request("POST", self.base + "/draft/replacement", data=payload)
        self.assertEqual(response["status"], 200, response)
        result = response["json"]["data"]
        self.assertEqual(result["session"]["currentResumeGeneration"], 2)
        self.assertEqual(result["stateRevision"], payload["expectedStateRevision"] + 1)
        self.assertEqual(result["draft"]["blocks"][0]["markdown"], "# 完整构造新版\n只保留新版经历")
        self.assertEqual(self.repo.get_suggestion(self.ctx, pending["suggestionId"])["status"], "invalidated")
        self.assertEqual(self.repo.get_suggestion(self.ctx, accepted["suggestionId"])["status"], "accepted")
        context = self.repo.get_session_context(self.ctx)
        self.assertEqual(context["messages"], [])
        self.assertEqual(context["issues"], [])
        self.assertEqual(context["decisionFeedback"], [])
        self.assertEqual({s["role"] for s in context["sources"]}, {"resume", "jd"})
        self.assertEqual(self.repo.read_material(self.ctx, [jd["sourceId"]])[0]["text"], "岗位要求 Python")
        with self.assertRaises(RepositoryError):
            self.repo.read_material(self.ctx, [self.ref["sourceId"]])
        self.assertEqual(self.repo.search_evidence(self.ctx, "8 条"), [])
        self.assertTrue(self.repo.read_source_snapshot(self.ctx, self.ref["sourceId"], 1)["historical"])
        with self.assertRaises(RepositoryError):
            self.repo.get_session_context(run)
        with database(self.path, read_only=True) as connection:
            record = connection.execute("SELECT * FROM draft_replacements").fetchone()
            self.assertEqual(record["before_markdown"], "".join(b["markdown"] for b in before["blocks"]))
            self.assertIn(payload["authorizationMessageId"], record["authorization_text"])
        self.assertEqual(self.request("POST", self.base + "/draft/replacement", data=payload)["json"], response["json"])
        self.assertEqual(self.count("draft_replacements"), 1)

    def test_replacement_mid_transaction_failure_restores_scope_pending_issues_and_run(self):
        session = self.setup_case()
        self.proposal()
        payload = self.replacement(session)
        self.run_record()
        with database(self.path) as connection:
            connection.execute("CREATE TRIGGER fail_replacement BEFORE INSERT ON draft_replacements BEGIN SELECT RAISE(ABORT, 'injected'); END")
        before = self.snapshot()
        self.assert_error(self.request("POST", self.base + "/draft/replacement", data=payload), 503, "storage_failed")
        self.assertEqual(self.snapshot(), before)

    def test_replacement_rejects_missing_authorization_incomplete_and_foreign_material(self):
        session = self.setup_case()
        payload = self.replacement(session)
        other = self.new_session()
        foreign = self.material(other)["json"]["data"]["material"]["materialId"]
        for extra in ({"confirmed": False}, {"completeConfirmed": False}, {"authorizationMessageId": "missing"}, {"materialId": foreign}):
            result = self.request("POST", self.base + "/draft/replacement", data={**payload, **extra})
            self.assertIn(result["status"], (404, 422), result)
        with database(self.path) as connection:
            connection.execute("UPDATE attachments SET parse_status = 'partial' WHERE attachment_id = ?", (payload["materialId"],))
        before = self.snapshot()
        self.assert_error(self.request("POST", self.base + "/draft/replacement", data=payload), 422, "invalid_input")
        self.assertEqual(self.snapshot(), before)

    def test_cross_session_suggestion_and_model_write_paths_are_denied(self):
        self.setup_case()
        proposal = self.proposal()
        other = self.new_session()
        result = self.request("POST", f"/api/sessions/{other['sessionId']}/suggestions/{proposal['suggestionId']}/decision",
                              data=self.decision_payload(proposal, expectedStateRevision=1))
        self.assert_error(result, 404, "source_not_found")
        run = self.run_record()
        for call in (lambda: self.repo.update_draft(run, "bad"),
                     lambda: self.repo.apply_suggestion_decision(run, proposal["suggestionId"], self.decision_payload(proposal)),
                     lambda: self.repo.replace_draft(run, {})):
            with self.assertRaises(RepositoryError) as error:
                call()
            self.assertEqual(error.exception.code, "scope_violation")
        self.assertFalse(MaterialTools(self.repo, run).dispatch("accept", {})["ok"])

    def test_concurrent_retries_insert_once_and_survive_service_restart(self):
        self.setup_case()
        suggestion = self.proposal("insert_after")
        payload = self.decision_payload(suggestion)
        def execute():
            return BusinessService(Repository(self.path)).apply_suggestion_decision(self.sid, suggestion["suggestionId"], payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: execute(), range(2)))
        self.assertEqual(first, second)
        self.assertEqual(execute(), first)
        self.assertEqual(len(self.draft()["blocks"]), 4)

    def test_deleted_session_cannot_be_recreated_by_old_edit_or_decision_retry(self):
        self.setup_case()
        proposal = self.proposal()
        payload = self.decision_payload(proposal)
        self.decide(proposal, payload)
        self.assertEqual(self.request("DELETE", self.base, data={"requestId": "delete", "expectedStateRevision": self.version(self.sid), "confirmed": True})["status"], 200)
        self.assert_error(self.decide(proposal, payload), 404, "session_not_found")
        self.assert_error(self.request("PUT", self.base + "/draft", data={"requestId": "late", "expectedStateRevision": 1, "markdown": "bad"}), 404, "session_not_found")
        self.assertEqual(self.count("sessions"), 0)
        self.assertEqual(self.count("suggestion_decisions"), 0)

    def test_changed_retry_payload_conflicts(self):
        self.setup_case()
        suggestion = self.proposal()
        payload = self.decision_payload(suggestion, decision="reject")
        self.assertEqual(self.decide(suggestion, payload)["status"], 200)
        self.assert_error(self.decide(suggestion, {**payload, "decision": "accept"}), 409, "request_conflict")

    def test_correcting_accepted_fact_never_silently_overwrites_draft(self):
        session = self.setup_case()
        proposal = self.proposal()
        self.decide(proposal, self.decision_payload(proposal))
        before = self.draft()
        correction = self.message(session, content="我仅协助，8 条数量待核实", kind="correction")
        self.assertEqual(correction["status"], 201)
        self.assertEqual(self.draft(), before)
        # B can record a conflict and propose a correction; A does not generate it.
        self.repo.save_issues(self.ctx, [{"kind": "conflict", "summary": "已接受数量待核实", "sourceRefs": [self.ref], "affectedBlockIds": [before["blocks"][1]["blockId"]]}])
        correction_proposal = self.proposal(after="协助整理反馈，数量待核实\n\n")
        self.assertEqual(self.draft(), before)
        self.assertEqual(self.decide(correction_proposal, self.decision_payload(correction_proposal, request_id="correction-accept"))["status"], 200)
        self.assertEqual(self.repo.get_suggestion(self.ctx, proposal["suggestionId"])["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
