"""User HTTP operations. Agent tools remain Python calls with a Host context."""

from io import BytesIO
import re
from typing import Literal
from urllib.parse import quote
import zipfile

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from app.repository import RepositoryError, RequestContext


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 50_000
router = APIRouter(prefix="/api")


def error_response(code, message, status_code, *, recoverable=True, fields=None):
    error = {"code": code, "message": message, "recoverable": recoverable}
    if fields:
        error["fields"] = fields
    return JSONResponse({"ok": False, "error": error}, status_code=status_code)


class LocalRequestGuard:
    """Local host/origin checks and bounded bodies, before JSON parsing."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value.decode("latin-1") for key, value in scope["headers"]}
        host = headers.get(b"host", "")
        origin = headers.get(b"origin")
        allowed_origins = {f"http://{hostname}:{port}" for hostname in ("127.0.0.1", "localhost") for port in (5173, 3001)}
        if not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", host) or (origin and origin not in allowed_origins) or headers.get(b"sec-fetch-site") == "cross-site":
            return await error_response("scope_violation", "只接受本机工作台的请求。", 403, recoverable=False)(scope, receive, send)
        method = scope["method"]
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await self.app(scope, receive, send)
        is_binary = scope["path"].endswith("/materials") and headers.get(b"content-type", "").split(";")[0].lower() != "application/json"
        maximum = MAX_UPLOAD_BYTES if is_binary else MAX_JSON_BYTES
        length = headers.get(b"content-length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,20}", length) or int(length) > maximum):
            return await error_response("invalid_input", "请求内容过大或长度无效。", 413)(scope, receive, send)
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > maximum:
                return await error_response("invalid_input", "请求内容超过当前大小限制。", 413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay_body():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, replay_body, send)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RequestId(InputModel):
    requestId: str = Field(min_length=1, max_length=128)

    @field_validator("requestId")
    @classmethod
    def valid_request_id(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise ValueError("Invalid request identifier")
        return value


class Mutation(RequestId):
    expectedStateRevision: int = Field(ge=1, le=2**63 - 1)


class CreateSession(RequestId):
    direction: str = Field(min_length=1, max_length=120)

    @field_validator("direction")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Direction is required")
        return value.strip()


class SendMessage(Mutation):
    content: str = Field(min_length=1, max_length=MAX_TEXT_CHARACTERS)
    kind: Literal["message", "supplement", "correction", "skip"] = "message"

    @field_validator("content")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Message is empty")
        return value


class DeleteSession(Mutation):
    confirmed: StrictBool

    @field_validator("confirmed")
    @classmethod
    def must_confirm(cls, value):
        if value is not True:
            raise ValueError("Deletion requires confirmation")
        return value


class SuggestionDecision(Mutation):
    decision: Literal["accept", "reject", "revise"]
    expectedBlockRevision: int = Field(ge=1, le=2**63 - 1)
    feedback: str = Field(default="", max_length=MAX_TEXT_CHARACTERS)

    @model_validator(mode="after")
    def require_revision_feedback(self):
        if self.decision == "revise" and not self.feedback.strip():
            raise ValueError("Revision requires feedback")
        return self


class DraftEdit(Mutation):
    markdown: str = Field(max_length=MAX_TEXT_CHARACTERS)


class DraftReplacement(DeleteSession):
    expectedDraftRevision: int = Field(ge=1, le=2**63 - 1)
    materialId: str = Field(min_length=1, max_length=128)
    authorizationMessageId: str = Field(min_length=1, max_length=128)
    completeConfirmed: StrictBool

    @field_validator("completeConfirmed")
    @classmethod
    def require_complete_candidate(cls, value):
        if value is not True:
            raise ValueError("Complete candidate confirmation is required")
        return value


class UploadMetadata(Mutation):
    name: str = Field(min_length=1, max_length=240)
    role: Literal["unknown", "resume", "jd", "supplement"] = "unknown"

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if not value.strip() or any(char in value for char in ('/', '\\')) or any(ord(char) < 32 for char in value):
            raise ValueError("Use a filename, not a path")
        return value


class PastedMaterial(UploadMetadata):
    name: str = Field(default="粘贴材料.md", min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARACTERS)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Material text is empty")
        return value


def inspect_upload(name, raw, declared_type):
    """Recognize supported file containers; parsing binary formats is deferred."""
    if not raw:
        raise RepositoryError("invalid_input", "文件内容为空。")
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    types = {"md": "text/markdown", "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    if extension not in types:
        raise RepositoryError("unsupported_format", "请使用 .md、.docx、PDF 或 PNG/JPEG；旧 .doc 请先转换。")
    media_type = types[extension]
    if declared_type not in {"application/octet-stream", media_type} and not (extension == "md" and declared_type == "text/plain"):
        raise RepositoryError("invalid_input", "声明的文件类型与扩展名不一致。")
    text = None
    if extension == "md":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise RepositoryError("parse_failed", "Markdown 无法按 UTF-8 读取，请重新保存或粘贴文字。") from error
        if not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
            raise RepositoryError("invalid_input", "Markdown 为空或超过当前 50000 字符限制。")
    elif extension == "pdf" and not raw.startswith(b"%PDF-"):
        raise RepositoryError("invalid_input", "文件内容不是可识别的 PDF。")
    elif extension == "png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RepositoryError("invalid_input", "文件内容不是可识别的 PNG。")
    elif extension in {"jpg", "jpeg"} and not raw.startswith(b"\xff\xd8\xff"):
        raise RepositoryError("invalid_input", "文件内容不是可识别的 JPEG。")
    elif extension == "docx":
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                names = archive.namelist()
                if len(names) > 10000 or not {"[Content_Types].xml", "word/document.xml"} <= set(names):
                    raise ValueError("Not a supported Word container")
        except (zipfile.BadZipFile, ValueError) as error:
            raise RepositoryError("invalid_input", "文件内容不是可识别的 DOCX。") from error
    return media_type, text


def _service(request):
    return request.app.state.business_service


def _ok(data):
    return {"ok": True, "data": data}


@router.get("/sessions")
def list_sessions(request: Request):
    return _ok(_service(request).repository.list_sessions())


@router.post("/sessions", status_code=201)
def create_session(payload: CreateSession, request: Request):
    return _service(request).create_session(payload.model_dump())


@router.post("/demo/sessions", status_code=201)
def create_demo_session(payload: RequestId, request: Request):
    return _service(request).create_mock_session(payload.model_dump())


@router.post("/sessions/{session_id}/mock-analysis")
def analyze_mock_session(session_id: str, payload: Mutation, request: Request):
    return _service(request).analyze_mock_session(session_id, payload.model_dump())


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request):
    return _ok(_service(request).repository.get_session_detail(RequestContext(session_id)))


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, payload: DeleteSession, request: Request):
    return _service(request).delete_session(session_id, payload.model_dump())


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, request: Request):
    return _ok(_service(request).repository.list_messages(RequestContext(session_id)))


@router.post("/sessions/{session_id}/messages", status_code=201)
def send_message(session_id: str, payload: SendMessage, request: Request):
    return _service(request).save_message(session_id, payload.model_dump())


@router.get("/sessions/{session_id}/draft")
def get_draft(session_id: str, request: Request):
    return _ok(_service(request).repository.get_draft(RequestContext(session_id)))


@router.put("/sessions/{session_id}/draft")
def update_draft(session_id: str, payload: DraftEdit, request: Request):
    return _service(request).update_draft(session_id, payload.model_dump())


@router.post("/sessions/{session_id}/suggestions/{suggestion_id}/decision")
def decide_suggestion(session_id: str, suggestion_id: str, payload: SuggestionDecision, request: Request):
    return _service(request).apply_suggestion_decision(session_id, suggestion_id, payload.model_dump())


@router.post("/sessions/{session_id}/draft/replacement")
def replace_draft(session_id: str, payload: DraftReplacement, request: Request):
    return _service(request).replace_draft(session_id, payload.model_dump())


@router.get("/sessions/{session_id}/materials")
def list_materials(session_id: str, request: Request):
    detail = _service(request).repository.get_session_detail(RequestContext(session_id))
    return _ok(detail["materials"])


@router.post("/sessions/{session_id}/materials", status_code=201)
async def upload_material(session_id: str, request: Request):
    declared = request.headers.get("content-type", "").split(";")[0].lower()
    raw = await request.body()
    if declared == "application/json":
        try:
            payload = PastedMaterial.model_validate_json(raw)
        except ValidationError as error:
            return validation_response(error)
        metadata = payload.model_dump(exclude={"text"})
        raw = payload.text.encode("utf-8")
        media_type, text = inspect_upload(payload.name, raw, "text/markdown")
    else:
        query = dict(request.query_params)
        if any(len(request.query_params.getlist(key)) != 1 for key in query):
            raise RepositoryError("invalid_input", "上传参数不能重复。")
        revision = query.get("expectedStateRevision", "")
        if not re.fullmatch(r"[1-9][0-9]{0,18}", revision):
            raise RepositoryError("invalid_input", "请提供有效的预期上下文版本。")
        query["expectedStateRevision"] = int(revision)
        try:
            metadata = UploadMetadata.model_validate(query).model_dump()
        except ValidationError as error:
            return validation_response(error)
        media_type, text = inspect_upload(metadata["name"], raw, declared)
    return await run_in_threadpool(_service(request).save_material, session_id, metadata, raw, media_type, text)


@router.get("/sessions/{session_id}/materials/{material_id}")
def get_material(session_id: str, material_id: str, request: Request):
    return _ok(_service(request).repository.get_material_detail(RequestContext(session_id), material_id))


@router.get("/sessions/{session_id}/materials/{material_id}/file")
def download_material(session_id: str, material_id: str, request: Request):
    material = _service(request).repository.read_material_file(RequestContext(session_id), material_id)
    return Response(material["originalData"], media_type=material["mediaType"], headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(material["name"], safe=""),
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/sessions/{session_id}/sources/{source_id}/revisions/{revision}")
def source_snapshot(session_id: str, source_id: str, revision: int, request: Request):
    return _ok(_service(request).repository.read_source_snapshot(RequestContext(session_id), source_id, revision))


@router.get("/sessions/{session_id}/suggestions/{suggestion_id}")
def get_suggestion(session_id: str, suggestion_id: str, request: Request):
    return _ok(_service(request).repository.get_suggestion(RequestContext(session_id), suggestion_id))


def validation_response(error):
    fields = [{"field": ".".join(str(part) for part in item["loc"]), "type": item["type"]} for item in error.errors()]
    return error_response("invalid_input", "请求字段或数据格式不正确。", 422, fields=fields)
