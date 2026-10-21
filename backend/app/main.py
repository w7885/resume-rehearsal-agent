import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
import sqlite3
from starlette.exceptions import HTTPException

from app.db import DEFAULT_DATABASE_PATH, database, initialize_database
from app.api import LocalRequestGuard, error_response, router, validation_response
from app.business_service import BusinessService
from app.repository import Repository, RepositoryError


@asynccontextmanager
async def lifespan(app: FastAPI):
    path = app.state.database_path or DEFAULT_DATABASE_PATH
    with database(path) as connection:
        initialize_database(connection)
    app.state.business_service = BusinessService(Repository(path))
    yield


def create_app(database_path=None):
    application = FastAPI(lifespan=lifespan)
    application.state.database_path = database_path
    application.add_middleware(LocalRequestGuard)
    application.include_router(router)

    @application.exception_handler(RepositoryError)
    async def business_error(request: Request, error: RepositoryError):
        status = {"session_not_found": 404, "source_not_found": 404, "scope_violation": 403,
                  "stale_state": 409, "request_conflict": 409, "unsupported_format": 415,
                  "storage_failed": 503, "cancelled": 409}.get(error.code, 422)
        return error_response(error.code, error.message, status, recoverable=error.recoverable)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return validation_response(error)

    @application.exception_handler(sqlite3.Error)
    @application.exception_handler(OSError)
    async def storage_error(request: Request, error: Exception):
        return error_response("storage_failed", "本地保存或读取失败，当前操作未确认成功。", 503)

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return error_response("http_error", "接口不存在或不支持此请求方式。", error.status_code)

    @application.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception):
        return error_response("internal_error", "服务处理失败，请保留输入后重试。", 500)

    application.add_api_route("/api/health", get_health, methods=["GET"])
    return application


def get_health():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()

    return {
        "status": "ok",
        "model_configured": bool(api_key),
    }


app = create_app()
