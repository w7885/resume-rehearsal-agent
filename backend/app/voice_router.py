"""Local-only voice input endpoints; transcripts are unsent composer text."""
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from app.api import error_response
from app import voice

router = APIRouter(prefix='/api/voice')


@router.get('/status')
async def voice_status():
    return {'ok': True, 'data': voice.status()}


@router.post('/transcribe')
async def voice_transcribe(request: Request):
    if request.headers.get('content-type', '').split(';')[0].lower() not in {'audio/wav', 'audio/x-wav'}:
        return error_response('invalid_audio', '请使用页面中的录音按钮录制语音。', 415)
    try:
        value = await run_in_threadpool(voice.transcribe, await request.body())
        return {'ok': True, 'data': value}
    except voice.VoiceError as error:
        return error_response(error.code, error.message, error.status)
