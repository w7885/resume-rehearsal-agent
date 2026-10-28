"""A-T05 integration checks: real SQLite and Python tools, synthetic data only."""

from copy import deepcopy
import json
import unittest

from app.db import database, initialize_database, transaction
from app.material_tools import MaterialTools
from app.repository import Repository, RepositoryError, RequestContext
from app.agent.adapters.a_repo import ARepoAdapter
from app.agent.ports import capture_proposal_snapshot
from test_db import FIXTURE, WORKSPACE_ROOT, seed_fixture, temporary_directory


EXCHANGES = json.loads((WORKSPACE_ROOT / "tests/fixtures/minimal-tool-exchanges.json").read_text(encoding="utf-8"))


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory()
        self.root = self.directory.__enter__()
        self.addCleanup(self.directory.__exit__, None, None, None)
        self.path = self.root / "repository.sqlite"
        with database(self.path) as connection:
            initialize_database(connection)
            self.session_id = seed_fixture(connection)
        self.repo = Repository(self.path)
        self.context = RequestContext(self.session_id)
        self.tools = MaterialTools(self.repo, self.context)

    def proposal(self):
        result = deepcopy(FIXTURE["suggestion"])
        for key in ("suggestionId", "sessionId", "status"):
            result.pop(key)
        return result

    def sql(self, statement, parameters=()):
        # Only test-authored fixed statements are executed through this helper.
        with database(self.path) as connection, transaction(connection):
            return connection.execute(statement, parameters).fetchall()

    def count(self, table):
        with database(self.path, read_only=True) as connection:
            return connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

    def assert_error(self, response, code):
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error"]["code"], code)
        self.assertNotIn("data", response)

    def new_personal_source(self, context, text):
        message = self.repo.save_message(context, text, kind="supplement")
        return self.repo.save_source(context, text=text, role="supplement", use_status="active",
                                     origin={"kind": "message", "messageId": message["messageId"]})

    def begin_run(self):
        session = self.repo.get_session(self.context)
        self.sql("UPDATE runs SET status='running', input_state_revision=?, resume_generation=? WHERE run_id='run-test-001'",
                 (session["stateRevision"], session["currentResumeGeneration"]))
        return RequestContext(self.session_id, session["stateRevision"], "run-test-001", 1)

    def test_five_existing_exchange_cases_match_exactly(self):
        for case in EXCHANGES["cases"]:
            with self.subTest(case=case["caseId"]):
                response = self.tools.dispatch(case["modelToolName"], case["input"])
                self.assertEqual(response, case["expectedResponse"])

    def test_user_data_roundtrip_and_initial_draft(self):
        session = self.repo.create_session("  后端开发实习  ")
        context = RequestContext(session["sessionId"])
        self.assertEqual(session["direction"], "后端开发实习")
        self.assertNotEqual(session["sessionId"], self.session_id)
        raw = "负责需求整理。".encode("utf-8")
        material = self.repo.save_material(context, name="简历.md", media_type="text/markdown", original_data=raw,
                                           role="resume", parse_status="parsed", use_status="active")
        source = self.repo.save_source(context, text=raw.decode(), role="resume", use_status="active",
                                      origin={"kind": "material", "materialId": material["materialId"], "lineStart": 1, "lineEnd": 1})
        ref = {"sourceId": source["sourceId"], "sourceRevision": 1, "quote": source["text"]}
        before = self.repo.get_session(context)
        draft = self.repo.create_initial_draft(context, [{"markdown": source["text"], "sourceRefs": [ref]}])
        self.assertEqual(draft["draftRevision"], before["draftRevision"] + 1)
        self.assertEqual(self.repo.get_session(context)["stateRevision"], before["stateRevision"] + 1)
        self.assertEqual(self.repo.get_draft(context), draft)
        self.assertEqual(self.repo.read_material_file(context, material["materialId"])["originalData"], raw)
        self.assertEqual(self.repo.read_material(context, [source["sourceId"]]), [source])
        self.assertTrue(any(item["sessionId"] == session["sessionId"] for item in self.repo.list_sessions()))
        with self.assertRaises(RepositoryError):
            self.repo.create_initial_draft(context, [{"markdown": "overwrite", "sourceRefs": [ref]}])

    def test_user_input_advances_context_assistant_output_does_not(self):
        before = self.repo.get_session(self.context)
        message = self.repo.save_message(self.context, "新增的用户反馈")
        after = self.repo.get_session(self.context)
        self.assertEqual(after["stateRevision"], before["stateRevision"] + 1)
        self.assertEqual(after["draftRevision"], before["draftRevision"])
        self.repo.save_message(self.context, "Agent 的进度说明", role="assistant")
        self.assertEqual(self.repo.get_session(self.context)["stateRevision"], after["stateRevision"])
        self.assertIn(message, self.repo.list_messages(self.context))

    def test_equal_message_timestamps_keep_insertion_order(self):
        first = self.repo.save_message(self.context, "先说负责统筹")
        second = self.repo.save_message(self.context, "后纠正为协助")
        self.sql("UPDATE messages SET created_at='2099-01-01T00:00:00Z' WHERE message_id IN (?, ?)", (first["messageId"], second["messageId"]))
        self.assertEqual([item["content"] for item in self.repo.list_messages(self.context)][-2:], [first["content"], second["content"]])

    def test_same_keyword_in_other_session_never_crosses_scope(self):
        other = RequestContext(self.repo.create_session("另一个岗位")["sessionId"])
        foreign = self.new_personal_source(other, "8 条同学反馈，另一个人的经历")
        results = self.tools.dispatch("search_evidence", {"query": "8 条同学反馈"})
        self.assertEqual([row["sourceId"] for row in results["data"]], ["source-supplement-001"])
        self.assert_error(self.tools.dispatch("read_material", {"sourceIds": [foreign["sourceId"]]}), "scope_violation")

    def test_read_batch_missing_item_returns_error_not_partial_data(self):
        response = self.tools.dispatch("read_material", {"sourceIds": ["source-resume-001", "missing"]})
        self.assert_error(response, "source_not_found")

    def test_search_excludes_jd_unknown_pending_and_parent_history(self):
        jd_text = FIXTURE["sources"][1]["text"]
        self.assertEqual(self.tools.dispatch("search_evidence", {"query": jd_text})["data"], [])
        material = self.repo.save_material(self.context, name="用途未明.md", media_type="text/markdown", original_data=b"unknown")
        unknown = self.repo.save_source(self.context, text="待澄清唯一关键词", role="unknown",
                                       origin={"kind": "material", "materialId": material["materialId"]})
        self.assertEqual(self.tools.dispatch("search_evidence", {"query": "待澄清唯一关键词"})["data"], [])
        self.assert_error(self.tools.dispatch("read_material", {"sourceIds": [unknown["sourceId"]]}), "stale_state")
        self.assertNotIn("待澄清唯一关键词", json.dumps(self.tools.get_session_context(), ensure_ascii=False))
        self.sql("UPDATE messages SET use_status='history' WHERE message_id='message-user-001'")
        self.assertEqual(self.tools.dispatch("search_evidence", {"query": "8 条同学反馈"})["data"], [])

    def test_jd_cannot_be_relabelled_as_personal_evidence_by_source_alone(self):
        # Malformed lower-level data must not bypass the parent's JD classification.
        self.sql("INSERT INTO source_blocks(source_id,source_revision,session_id,attachment_id,role,text,origin_json,use_status) "
                 "VALUES ('mislabelled',1,?,'material-jd-001','resume','虚构个人集成能力','{}','active')", (self.session_id,))
        self.assertEqual(self.tools.dispatch("search_evidence", {"query": "虚构个人集成能力"})["data"], [])

    def test_search_query_is_literal_and_results_are_bounded_and_sorted(self):
        self.new_personal_source(self.context, "百分号 % 和下划线 _ 只是原文，需求整理")
        self.new_personal_source(self.context, "另外一条需求整理")
        self.assertEqual(len(self.repo.search_evidence(self.context, "%")), 1)
        self.assertEqual(len(self.repo.search_evidence(self.context, "_")), 1)
        self.assertEqual(self.repo.search_evidence(self.context, "' OR 1=1 --"), [])
        results = self.repo.search_evidence(self.context, "需求整理", 2)
        self.assertEqual(len(results), 2)
        self.assertEqual([row["sourceId"] for row in results], sorted(row["sourceId"] for row in results))

    def test_invalid_queries_limits_and_model_scope_parameters_rejected(self):
        for args in ({"query": None}, {"query": []}, {"query": "x", "limit": True}, {"query": "x", "limit": 0},
                     {"query": "x", "limit": 11}, {"query": "x", "limit": 1.5}, {"query": "x", "sessionId": "other"},
                     {"query": "x", "sql": "SELECT * FROM messages"}, {"query": "x", "path": "elsewhere"}):
            with self.subTest(arguments=args):
                self.assert_error(self.tools.dispatch("search_evidence", args), "invalid_input")
        for args in ({"sourceIds": []}, {"sourceIds": "source-resume-001"}, {"sourceIds": [None]}):
            self.assert_error(self.tools.dispatch("read_material", args), "invalid_input")

    def test_tool_allowlist_has_no_accept_delete_or_global_database_access(self):
        before = self.repo.get_draft(self.context)
        for name in ("accept_suggestion", "delete_session", "create_session", "list_sessions", "read_material_file", "execute_sql"):
            self.assert_error(self.tools.dispatch(name, {}), "invalid_tool")
        self.assertEqual(self.repo.get_draft(self.context), before)

    def test_suggestion_is_pending_without_draft_or_context_change(self):
        run_context = self.begin_run()
        tools = MaterialTools(self.repo, run_context)
        draft, state = self.repo.get_draft(self.context), self.repo.get_session(self.context)
        self.assert_error(tools.dispatch("propose_change", self.proposal()), 'missing_snapshot')
        adapter = ARepoAdapter(tools=tools, context=run_context, session_id=self.session_id)
        snapshot = capture_proposal_snapshot(adapter.get_session_context(self.session_id),
                                             self.proposal()['target']['blockId'], run_id=run_context.run_id)
        result = tools.propose_with_snapshot(self.proposal(), snapshot)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["status"], "pending")
        self.assertNotEqual(result["data"]["suggestionId"], FIXTURE["suggestion"]["suggestionId"])
        self.assertEqual(self.repo.get_draft(self.context), draft)
        self.assertEqual(self.repo.get_session(self.context), state)
        self.assertTrue(tools.dispatch("search_evidence", {"query": "需求整理"})["ok"])

    def test_suggestion_validates_all_references_before_saving(self):
        original_count = self.count("suggestions")
        missing = self.proposal()
        missing["sourceRefs"][0]["sourceId"] = "missing"
        self.assert_error(self.tools.dispatch("propose_change", missing), "source_not_found")
        wrong_quote = self.proposal()
        wrong_quote["sourceRefs"][0]["quote"] = "原文没有这句话"
        self.assert_error(self.tools.dispatch("propose_change", wrong_quote), "invalid_input")
        jd_only = self.proposal()
        jd_only["sourceRefs"] = [{"sourceId": "source-jd-001", "sourceRevision": 1, "quote": "API 集成"}]
        self.assert_error(self.tools.dispatch("propose_change", jd_only), "invalid_input")
        empty_refs = self.proposal()
        empty_refs["sourceRefs"] = []
        self.assert_error(self.tools.dispatch("propose_change", empty_refs), "invalid_input")
        self.assertEqual(self.count("suggestions"), original_count)

    def test_duplicate_reference_handling_does_not_silently_lose_distinct_quotes(self):
        same = self.proposal()
        same["sourceRefs"].append(deepcopy(same["sourceRefs"][0]))
        saved = self.tools.dispatch("propose_change", same)
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(len(saved["data"]["sourceRefs"]), 2)
        different = self.proposal()
        extra = deepcopy(different["sourceRefs"][0])
        extra["quote"] = "协助页面开发"
        different["sourceRefs"].append(extra)
        self.assert_error(self.tools.dispatch("propose_change", different), "invalid_input")

    def test_stale_target_or_before_and_extra_accepted_status_rejected(self):
        for change in ("version", "before", "status"):
            proposal = self.proposal()
            if change == "version":
                proposal["target"]["expectedBlockRevision"] = 99
            elif change == "before":
                proposal["before"] = "不是当前原文"
            else:
                proposal["status"] = "accepted"
            self.assert_error(self.tools.dispatch("propose_change", proposal), "invalid_input" if change == "status" else "stale_state")
        for revision in (None, True, 0, 2**63):
            proposal = self.proposal()
            proposal["target"]["expectedBlockRevision"] = revision
            self.assert_error(self.tools.dispatch("propose_change", proposal), "invalid_input")

    def test_reference_save_failure_rolls_back_suggestion_and_hides_debug_detail(self):
        before = self.count("suggestions")
        self.sql("CREATE TRIGGER fail_reference BEFORE INSERT ON suggestion_sources BEGIN SELECT RAISE(ABORT, 'internal-debug-only'); END")
        response = self.tools.dispatch("propose_change", self.proposal())
        self.assert_error(response, "storage_failed")
        self.assertNotIn("internal-debug-only", json.dumps(response))
        self.assertEqual(self.count("suggestions"), before)

    def test_sources_corrected_as_new_snapshot_invalidate_old_pending(self):
        source = FIXTURE["sources"][2]
        corrected = self.repo.save_source(self.context, source_id=source["sourceId"], expected_source_revision=1,
                                          text="更正：整理 7 条反馈，没有上线数据。", role="supplement", origin=source["origin"], use_status="active")
        self.assertEqual(corrected["sourceRevision"], 2)
        self.assertEqual(self.repo.read_material(self.context, [source["sourceId"]]), [corrected])
        old = self.repo.read_source_snapshot(self.context, source["sourceId"], 1)
        self.assertTrue(old["historical"])
        self.assertEqual(old["source"]["text"], source["text"])
        self.assertEqual(self.repo.get_suggestion(self.context, FIXTURE["suggestion"]["suggestionId"])["status"], "invalidated")
        self.assert_error(self.tools.dispatch("propose_change", self.proposal()), "stale_state")
        context = self.tools.get_session_context()["data"]
        self.assertEqual(context["suggestions"], [])
        self.assertEqual(context["issues"], [])
        self.assertEqual(self.repo.get_draft(self.context)["blocks"][0]["markdown"], FIXTURE["suggestion"]["before"])

    def test_old_resume_messages_and_sources_not_in_new_generation_context(self):
        self.sql("UPDATE sessions SET current_resume_generation=2, state_revision=2 WHERE session_id=?", (self.session_id,))
        context = self.tools.get_session_context()["data"]
        self.assertEqual(context["messages"], [])
        self.assertEqual(context["draft"]["blocks"], [])
        self.assertEqual(context["suggestions"], [])
        self.assertEqual(context["issues"], [])
        self.assertEqual([source["role"] for source in context["sources"]], ["jd"])
        self.assert_error(self.tools.dispatch("read_material", {"sourceIds": ["source-resume-001"]}), "stale_state")
        self.assertTrue(self.tools.dispatch("read_material", {"sourceIds": ["source-jd-001"]})["ok"])
        self.assertEqual(self.tools.dispatch("search_evidence", {"query": "需求整理"})["data"], [])

    def test_user_correction_interrupts_run_and_old_context_cannot_write(self):
        context = self.begin_run()
        tools = MaterialTools(self.repo, context)
        self.repo.save_message(self.context, "我只是协助，没有负责统筹", kind="correction")
        before = self.count("suggestions")
        self.assert_error(tools.dispatch("propose_change", self.proposal()), "stale_state")
        self.assertEqual(self.count("suggestions"), before)
        current = self.repo.get_session(self.context)
        cancelled = RequestContext(self.session_id, current["stateRevision"], context.run_id, context.generation)
        self.assert_error(MaterialTools(self.repo, cancelled).dispatch("search_evidence", {"query": "需求整理"}), "cancelled")

    def test_wrong_run_generation_or_resume_generation_rejected(self):
        context = self.begin_run()
        wrong = RequestContext(self.session_id, context.expected_state_revision, context.run_id, 99)
        self.assert_error(MaterialTools(self.repo, wrong).get_session_context(), "cancelled")
        self.sql("UPDATE runs SET resume_generation=99 WHERE run_id=?", (context.run_id,))
        self.assert_error(MaterialTools(self.repo, context).get_session_context(), "stale_state")

    def test_model_context_cannot_create_user_facts_or_import_draft(self):
        context = self.begin_run()
        for operation in (
            lambda: self.repo.save_message(context, "冒充用户补充"),
            lambda: self.repo.save_material(context, name="x", media_type="text/plain", original_data=b"x"),
            lambda: self.repo.read_material_file(context, "material-resume-001"),
            lambda: self.repo.create_initial_draft(context, []),
            lambda: self.repo.read_source_snapshot(context, "source-resume-001", 1),
        ):
            with self.assertRaises(RepositoryError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "scope_violation")

    def test_assistant_message_cannot_become_personal_source(self):
        message = self.repo.save_message(self.context, "模型自己写的经历", role="assistant")
        with self.assertRaises(RepositoryError):
            self.repo.save_source(self.context, text=message["content"], role="supplement", use_status="active",
                                  origin={"kind": "message", "messageId": message["messageId"]})

    def test_issues_are_atomic_and_do_not_change_formal_draft_or_context(self):
        issue = deepcopy(FIXTURE["issues"][0])
        issue.pop("issueId")
        issue.pop("sessionId", None)
        issue.pop("status")
        before = self.repo.get_draft(self.context), self.repo.get_session(self.context)
        result = self.tools.dispatch("record_issues", {"issues": [issue]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"][0]["status"], "open")
        self.assertEqual((self.repo.get_draft(self.context), self.repo.get_session(self.context)), before)
        count = self.count("issues")
        invalid = deepcopy(issue)
        invalid["sourceRefs"][0]["sourceId"] = "missing"
        self.assert_error(self.tools.dispatch("record_issues", {"issues": [issue, invalid]}), "source_not_found")
        self.assertEqual(self.count("issues"), count)

    def test_storage_failure_is_not_a_successful_empty_result(self):
        missing = self.root / "missing.sqlite"
        tools = MaterialTools(Repository(missing), self.context)
        self.assert_error(tools.dispatch("search_evidence", {"query": "需求整理"}), "storage_failed")
        self.assertFalse(missing.exists())

    def test_missing_session_and_partial_run_context_rejected(self):
        tools = MaterialTools(self.repo, RequestContext("missing-session"))
        self.assert_error(tools.dispatch("search_evidence", {"query": "需求整理"}), "session_not_found")
        for parameters in ({"run_id": "run"}, {"generation": 1}, {"expected_state_revision": True}):
            with self.assertRaises(RepositoryError):
                RequestContext(self.session_id, **parameters)


if __name__ == "__main__":
    unittest.main()
