"""Run real local tools against synthetic temporary data, without a model."""

import json
from pathlib import Path
import sys
import tempfile

from app.db import BACKEND_ROOT, database, initialize_database
from app.material_tools import MaterialTools
from app.repository import Repository, RequestContext


def main():
    demo_root = (BACKEND_ROOT / ".local" / "tool-demo").resolve()
    demo_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="synthetic-", dir=demo_root) as directory:
        location = Path(directory).resolve()
        if not location.is_relative_to(demo_root):
            raise RuntimeError("Unexpected temporary cleanup location")
        path = location / "demo.sqlite"
        with database(path) as connection:
            initialize_database(connection)
        repository = Repository(path)
        session = repository.create_session("构造案例：开发实习")
        context = RequestContext(session["sessionId"])
        before = "参与校园报名工具开发，负责需求整理，协助页面开发。"
        material = repository.save_material(context, name="构造简历.md", media_type="text/markdown",
                                            original_data=before.encode("utf-8"), role="resume", parse_status="parsed", use_status="active")
        original = repository.save_source(context, text=before, role="resume", use_status="active",
                                          origin={"kind": "material", "materialId": material["materialId"]})
        supplement = "我整理了 8 条同学反馈，合并成 5 项需求，没有上线数据。"
        message = repository.save_message(context, supplement, kind="supplement")
        source = repository.save_source(context, text=supplement, role="supplement", use_status="active",
                                        origin={"kind": "message", "messageId": message["messageId"]})
        original_ref = {"sourceId": original["sourceId"], "sourceRevision": 1, "quote": before}
        draft = repository.create_initial_draft(context, [{"markdown": before, "sourceRefs": [original_ref]}])
        # This is a trusted local demo context, not a persisted live Agent run.
        # The live Host must also bind run_id/generation/expected_state_revision.
        tools = MaterialTools(repository, context)
        search = tools.dispatch("search_evidence", {"query": "8 条同学反馈", "limit": 5})
        proposal = {
            "operation": "replace",
            "target": {"blockId": draft["blocks"][0]["blockId"], "expectedBlockRevision": 1},
            "before": before,
            "after": "参与校园报名工具开发，整理 8 条同学反馈并归纳为 5 项需求，协助页面开发。",
            "reason": "依据用户补充，具体说明行动，保留协助开发的职责范围。",
            "sourceRefs": [original_ref, {"sourceId": source["sourceId"], "sourceRevision": 1, "quote": supplement}],
            "gaps": ["没有上线数据，不补造业务收益。"],
        }
        suggestion = tools.dispatch("propose_change", proposal)
        unchanged = repository.get_draft(context) == draft
        print(json.dumps({"synthetic": True, "modelCalled": False, "search": search,
                          "suggestion": suggestion, "draftUnchanged": unchanged}, ensure_ascii=False, indent=2))
        return 0 if search["ok"] and suggestion["ok"] and unchanged else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
