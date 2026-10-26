"""Voice boundaries in isolated ASGI apps; no microphone or provider calls."""
from io import BytesIO
import json
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch
import wave

from app import voice
import test_api


def wav(seconds=1, rate=16000, channels=1, width=2):
    output = BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(channels); audio.setsampwidth(width); audio.setframerate(rate)
        audio.writeframes(b'\0' * int(seconds * rate) * channels * width)
    return output.getvalue()


class AudioValidationTests(unittest.TestCase):
    def test_accepts_bounded_pcm_and_rejects_bad_format_or_truncation(self):
        self.assertEqual(len(voice.pcm_audio(wav())), 32000)
        for raw in (b'not a wav', wav(.1), wav(61), wav(rate=44100), wav(channels=2), wav(width=1), wav()[:-20]):
            with self.subTest(length=len(raw)), self.assertRaises(voice.VoiceError):
                voice.pcm_audio(raw)

    def test_worker_has_no_service_credentials_and_returns_only_transcript(self):
        with patch.object(voice, 'status', return_value={'available': True}), patch.object(voice.subprocess, 'run',
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'text':'我 使用 Python 开发 学生 信息 管理 程序'}))) as worker:
            with patch.dict('os.environ', {'DEEPSEEK_API_KEY':'synthetic-never-print'}):
                result = voice.transcribe(wav())
        self.assertEqual(result['text'], '我使用 Python 开发学生信息管理程序')
        self.assertTrue(result['local'])
        self.assertNotIn('DEEPSEEK_API_KEY', worker.call_args.kwargs['env'])
        self.assertNotIn('pcm', ' '.join(worker.call_args.args[0]))
        self.assertEqual(worker.call_args.kwargs['timeout'], 45)

    def test_empty_failed_malformed_and_timed_out_workers_are_recoverable(self):
        for output, code in (({'text':''}, 'voice_no_speech'), ([], 'voice_failed'), ({'text':4}, 'voice_failed')):
            with self.subTest(output=output), patch.object(voice,'status',return_value={'available':True}), patch.object(voice.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(output))):
                with self.assertRaises(voice.VoiceError) as caught:voice.transcribe(wav())
                self.assertEqual(caught.exception.code, code)
        with patch.object(voice,'status',return_value={'available':True}), patch.object(voice.subprocess,'run',side_effect=subprocess.TimeoutExpired('worker',45)):
            with self.assertRaises(voice.VoiceError) as caught:voice.transcribe(wav())
            self.assertEqual(caught.exception.code,'voice_timeout')

    def test_busy_and_unconfigured_do_not_launch_workers(self):
        with patch.object(voice,'status',return_value={'available':False}), patch.object(voice.subprocess,'run') as worker:
            with self.assertRaises(voice.VoiceError) as caught:voice.transcribe(wav())
            self.assertEqual(caught.exception.code,'voice_unavailable');worker.assert_not_called()
        voice._lock.acquire()
        try:
            with patch.object(voice,'status',return_value={'available':True}), patch.object(voice.subprocess,'run') as worker:
                with self.assertRaises(voice.VoiceError) as caught:voice.transcribe(wav())
                self.assertEqual(caught.exception.code,'voice_busy');worker.assert_not_called()
        finally:voice._lock.release()


class VoiceApiTests(unittest.TestCase):
    setUp = test_api.ApiTests.setUp
    request = test_api.ApiTests.request
    count = test_api.ApiTests.count

    def test_status_is_configuration_only_and_never_discloses_paths(self):
        result = self.request('GET','/api/voice/status')
        self.assertEqual(result['status'],200)
        self.assertEqual(set(result['json']['data']),{'available','language','maxSeconds','reason'})
        with patch('app.voice.Path.is_file', side_effect=PermissionError('model unreadable')):
            unavailable = self.request('GET','/api/voice/status')
        self.assertEqual(unavailable['status'],200)
        self.assertFalse(unavailable['json']['data']['available'])
        self.assertEqual(unavailable['json']['data']['reason'],'not_configured')

    def test_transcript_does_not_save_messages_sources_or_drafts(self):
        with patch.object(voice,'status',return_value={'available':True}), patch.object(voice.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'text':'构造语音文字'}))):
            result = self.request('POST','/api/voice/transcribe',body=wav(),headers={'content-type':'audio/wav'})
        self.assertEqual(result['status'],200,result)
        self.assertEqual(result['json']['data']['text'],'构造语音文字')
        for table in ('messages','source_blocks','draft_blocks','attachments','model_budget_reservations'):
            self.assertEqual(self.count(table),0)

    def test_audio_limit_is_independent_from_json_and_cross_origin_is_denied(self):
        with patch.object(voice,'transcribe',return_value={'text':'构造长录音','language':'zh-CN','local':True}) as transcribe:
            result = self.request('POST','/api/voice/transcribe',body=wav(40),headers={'content-type':'audio/wav'})
            self.assertEqual(result['status'],200,result)
            transcribe.assert_called_once()
        oversized = self.request('POST','/api/voice/transcribe',body=b'0'*(voice.MAX_AUDIO_BYTES+1),headers={'content-type':'audio/wav'})
        self.assertEqual(oversized['status'],413)
        foreign = self.request('POST','/api/voice/transcribe',body=wav(),headers={'content-type':'audio/wav','origin':'https://example.invalid'})
        self.assertEqual(foreign['status'],403)
        invalid = self.request('POST','/api/voice/transcribe',body=b'{}',headers={'content-type':'application/json'})
        self.assertEqual(invalid['status'],415)
