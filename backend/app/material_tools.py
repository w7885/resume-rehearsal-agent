"""Model-facing allowlist adapter; context and Repository are supplied by Host."""

import sqlite3

from app.repository import Repository, RepositoryError, RequestContext, _keys


class MaterialTools:
    def __init__(self, repository: Repository, context: RequestContext):
        self.repository, self.context = repository, context

    @staticmethod
    def _reply(operation):
        try:
            return {"ok": True, "data": operation()}
        except RepositoryError as error:
            return {"ok": False, "error": {"code": error.code, "message": error.message, "recoverable": error.recoverable}}
        except (sqlite3.Error, OSError):
            # Never expose SQL, filesystem paths, raw records, or stack traces.
            return {"ok": False, "error": {"code": "storage_failed", "message": "本地数据读取或保存失败，请检查后端后重试。", "recoverable": True}}

    def get_session_context(self):
        """Host calls this to assemble context; not a model-selectable tool."""
        return self._reply(lambda: self.repository.get_session_context(self.context))

    def dispatch(self, name: str, arguments: dict):
        def execute():
            if name == "read_material":
                _keys(arguments, {"sourceIds"})
                return self.repository.read_material(self.context, arguments["sourceIds"])
            if name == "search_evidence":
                _keys(arguments, {"query"}, {"limit"})
                return self.repository.search_evidence(self.context, arguments["query"], arguments.get("limit", 5))
            if name == "propose_change":
                return self.repository.save_suggestion(self.context, arguments)
            if name == "record_issues":
                _keys(arguments, {"issues"})
                return self.repository.save_issues(self.context, arguments["issues"])
            raise RepositoryError("invalid_tool", "未开放此工具。", False)
        return self._reply(execute)
