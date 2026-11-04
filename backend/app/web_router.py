"""User-only session web settings; no public fetch proxy or model endpoint."""
from fastapi import APIRouter, Request
from pydantic import Field, StrictBool

from app.api import RequestId
from app.repository import RequestContext

router = APIRouter(prefix='/api/sessions')


class WebSettingsInput(RequestId):
    enabled: StrictBool
    expectedWebSettingsRevision: int = Field(ge=1, le=2**63-1)


@router.get('/{session_id}/web-settings')
def get_web_settings(session_id: str, request: Request):
    repo = request.app.state.business_service.repository
    return {'ok': True, 'data': repo.get_web_settings(RequestContext(session_id))}


@router.patch('/{session_id}/web-settings')
def update_web_settings(session_id: str, payload: WebSettingsInput, request: Request):
    return request.app.state.business_service.update_web_settings(session_id, payload.model_dump())


@router.get('/{session_id}/external-references')
def get_external_references(session_id: str, request: Request):
    repo = request.app.state.business_service.repository
    return {'ok': True, 'data': repo.list_external_references(RequestContext(session_id))}
