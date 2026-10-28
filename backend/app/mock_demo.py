"""Fixed synthetic A-T02 scenario. Never generates content for user resumes."""

import json
from pathlib import Path

from app.agent.adapters.a_repo import ARepoAdapter
from app.agent.ports import capture_proposal_snapshot
from app.material_tools import MaterialTools
from app.repository import RepositoryError, RequestContext, _id


FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "minimal-resume-case.json"


def create_demo(repo):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if fixture.get("synthetic") is not True:
        raise RepositoryError("invalid_input", "演示材料必须明确标为构造材料。")
    session = repo.create_session("模拟示例：" + fixture["session"]["direction"])
    session_id = session["sessionId"]
    context = RequestContext(session_id)
    connection = repo._connection
    connection.execute("UPDATE sessions SET mode = 'mock' WHERE session_id = ?", (session_id,))
    source_map = {}
    for material in fixture["materials"]:
        saved = repo.save_material(context, name=material["name"], media_type="text/markdown", original_data=material["text"].encode(),
                                   role=material["role"], parse_status="parsed", use_status="active")
        source = repo.save_source(context, text=material["text"], role=material["role"], use_status="active",
                                  origin={"kind": "material", "materialId": saved["materialId"]})
        source_map[material["role"]] = source
    text = fixture["messages"][0]["content"]
    message = repo.save_message(context, text, kind="supplement")
    repo.save_source(context, text=text, role="supplement", use_status="active", origin={"kind": "message", "messageId": message["messageId"]})
    source = source_map["resume"]
    repo.create_initial_draft(context, [{"markdown": source["text"], "sourceRefs": [{"sourceId": source["sourceId"], "sourceRevision": 1, "quote": source["text"]}]}])
    return {"sessionId": session_id, **repo.get_session_detail(context)}


def analyze_demo(repo, context):
    repo._user_path(context)
    with repo._operation(context, write=True) as (connection, session):
        if session["mode"] != "mock":
            raise RepositoryError("scope_violation", "预设模拟流程只可用于独立构造示例，不能分析个人材料。", False)
        if connection.execute("SELECT 1 FROM runs WHERE session_id = ?", (context.session_id,)).fetchone():
            raise RepositoryError("stale_state", "本构造示例已运行；可新开模拟示例重新体验，不会忽略你的调整反馈。")
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        current = repo.get_session_context(context)
        draft = current["draft"]
        if session["current_resume_generation"] != 1 or draft["markdown"] != fixture["suggestion"]["before"]:
            raise RepositoryError("stale_state", "构造稿已改变，请新开模拟示例；预设建议不会套用到新正文。")
        sources = {source["role"]: source for source in current["sources"]}
        for role, expected in (("resume", fixture["materials"][0]["text"]), ("jd", fixture["materials"][1]["text"]), ("supplement", fixture["messages"][0]["content"])):
            if role not in sources or sources[role]["text"] != expected:
                raise RepositoryError("stale_state", "构造来源已改变，不能运行固定模拟。")
        repo._touch(connection, context.session_id)
        revision = repo.get_session(RequestContext(context.session_id))["stateRevision"]
        run_id = _id("mock-run")
        connection.execute("INSERT INTO runs(run_id, session_id, generation, input_state_revision, resume_generation, status) VALUES (?, ?, 1, ?, 1, 'running')",
                           (run_id, context.session_id, revision))
        tools = MaterialTools(repo, RequestContext(context.session_id, revision, run_id, 1))
        evidence = tools.dispatch("search_evidence", {"query": fixture["expectations"]["searchQuery"]})
        if not evidence["ok"] or not evidence["data"]:
            raise RepositoryError("stale_state", "构造依据查询失败，未生成建议。")
        block = draft["blocks"][0]
        adapter = ARepoAdapter(tools=tools, context=tools.context, session_id=context.session_id)
        snapshot = capture_proposal_snapshot(adapter.get_session_context(context.session_id),
                                             block['blockId'], run_id=run_id)
        def ref(role, quote):
            return {"sourceId": sources[role]["sourceId"], "sourceRevision": sources[role]["sourceRevision"], "quote": quote}
        proposal = {key: fixture["suggestion"][key] for key in ("operation", "before", "after", "reason", "gaps")}
        proposal.update(target={"blockId": block["blockId"], "expectedBlockRevision": block["blockRevision"]},
                        sourceRefs=[ref("resume", fixture["suggestion"]["before"]), ref("supplement", "我整理了 8 条同学反馈，把重复意见合并成 5 项需求")])
        suggestion = tools.propose_with_snapshot(proposal, snapshot)
        issues = tools.dispatch("record_issues", {"issues": [{"kind": "capability_gap", "summary": fixture["issues"][0]["summary"],
            "sourceRefs": [ref("jd", "API 集成"), ref("supplement", "未做 API 集成，没有上线数据。")], "affectedBlockIds": [block["blockId"]]}]})
        if not suggestion["ok"] or not issues["ok"]:
            raise RepositoryError("stale_state", "构造建议或问题保存失败，模拟事务未完成。")
        events = [
            ("progress", {"message": "模拟流程：读取构造简历与补充；未调用模型。"}),
            ("diagnosis", {"message": "原描述未体现已提供的反馈整理行动；API 集成仍是能力缺口。"}),
            ("tool_progress", {"message": "已真实查询构造依据并保存待确认建议；行动顺序为预设，无模型自主决策。"}),
            ("question", {"message": "模拟追问：这些反馈是如何收集的？可补充、纠正或跳过。此处不会自动生成模型回复。"}),
            ("suggestion", {"suggestionId": suggestion["data"]["suggestionId"]}),
            ("done", {"message": "模拟建议已保存，接受前当前稿保持原文。"}),
        ]
        for sequence, (event_type, payload) in enumerate(events, 1):
            connection.execute("INSERT INTO run_events(session_id, run_id, sequence, type, mode, payload_json) VALUES (?, ?, ?, ?, 'mock', ?)",
                               (context.session_id, run_id, sequence, event_type, json.dumps(payload, ensure_ascii=False)))
        connection.execute("UPDATE runs SET status = 'waiting_user' WHERE session_id = ? AND run_id = ?", (context.session_id, run_id))
        connection.execute("UPDATE sessions SET status = 'waiting_user' WHERE session_id = ?", (context.session_id,))
        return repo.get_session_detail(RequestContext(context.session_id))
