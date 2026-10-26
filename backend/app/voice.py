"""Bounded offline speech transcription. No provider, session or database writes."""
import base64
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import wave

BACKEND = Path(__file__).resolve().parents[1]
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 60
RATE = 16000
_lock = threading.Lock()


class VoiceError(ValueError):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def model_path():
    path = Path(os.getenv('VOICE_MODEL_PATH', '.local/voice-models/vosk-model-small-cn-0.22'))
    return path.resolve() if path.is_absolute() else (BACKEND / path).resolve()


def status():
    path = model_path()
    try:
        configured = (path / 'am/final.mdl').is_file() and (path / 'conf/model.conf').is_file() and (path / 'graph').is_dir()
    except OSError:
        configured = False
    enabled = os.getenv('VOICE_INPUT_ENABLED', '1') == '1'
    available = enabled and configured and importlib.util.find_spec('vosk') is not None
    return {'available': available, 'language': 'zh-CN', 'maxSeconds': MAX_SECONDS,
            'reason': None if available else 'disabled' if not enabled else 'not_configured'}


def pcm_audio(raw):
    if not raw or len(raw) > MAX_AUDIO_BYTES:
        raise VoiceError('invalid_audio', '请选择不超过 60 秒的录音。')
    try:
        with wave.open(BytesIO(raw), 'rb') as audio:
            frames = audio.getnframes()
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != RATE
                    or audio.getcomptype() != 'NONE' or not RATE // 4 <= frames <= RATE * MAX_SECONDS):
                raise VoiceError('invalid_audio', '录音格式无效，请重新录制 0.25～60 秒的语音。')
            pcm = audio.readframes(frames)
            if len(pcm) != frames * 2:
                raise VoiceError('invalid_audio', '录音数据不完整，请重新录制。')
            return pcm
    except (wave.Error, EOFError, OSError) as error:
        raise VoiceError('invalid_audio', '无法读取录音，请重新录制。') from error


def transcribe(raw):
    pcm = pcm_audio(raw)
    if not status()['available']:
        raise VoiceError('voice_unavailable', '本地语音识别尚未就绪，请检查语音配置；文字输入仍可使用。', 503)
    if not _lock.acquire(blocking=False):
        raise VoiceError('voice_busy', '上一段语音还在转写，请稍候再试。', 429)
    try:
        # The worker receives only PCM and a trusted configuration path. Do not
        # pass model-service credentials into this offline recognition process.
        env = {k: v for k, v in os.environ.items() if not re.search(r'API_KEY|TOKEN|SECRET|PASSWORD', k, re.I)}
        path = model_path()
        # Vosk's Windows native file API cannot open some absolute Unicode
        # paths. Project-local model components are ASCII and resolve from cwd.
        worker_path = str(path.relative_to(BACKEND)) if path.is_relative_to(BACKEND) else str(path)
        try:
            result = subprocess.run([sys.executable, '-m', 'app.voice_worker'],
                input=json.dumps({'modelPath': worker_path, 'pcm': base64.b64encode(pcm).decode('ascii')}),
                cwd=BACKEND, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode or len(result.stdout) > 200000:
                raise ValueError('worker failed')
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError('invalid worker output')
            text = value.get('text')
            if not isinstance(text, str) or len(text) > 50000:
                raise ValueError('invalid worker output')
        except subprocess.TimeoutExpired as error:
            raise VoiceError('voice_timeout', '这段语音转写超时了，录音可重试，已有文字不会丢失。', 503) from error
        except (OSError, ValueError) as error:
            raise VoiceError('voice_failed', '语音暂时没有转写成功，可以重试或重新录制。', 503) from error
        text = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', text.strip())
        if not text:
            raise VoiceError('voice_no_speech', '没有识别到清晰语音，可以重试或重新录制。')
        return {'text': text, 'language': 'zh-CN', 'local': True}
    finally:
        _lock.release()
