"""DeepSeek-Flash transcription checks against a fake provider; no network."""
import os
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import vision


class FakeResponse:
    def __init__(self, status, content='', finish_reason='stop'):
        self.status_code = status
        self._content = content
        self._finish_reason = finish_reason

    def json(self):
        return {'choices': [{'message': {'content': self._content}, 'finish_reason': self._finish_reason}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}


class FakeClient:
    calls = []

    def __init__(self, responses, **kwargs):
        self.responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        FakeClient.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeBudget:
    def __init__(self):
        self.allow = True
        self.reserved = []
        self.charged = []

    async def reserve(self, amount):
        if not self.allow:
            return False
        self.reserved.append(amount)
        return True

    async def settle(self, run_id, actual, reserved):
        self.charged.append(actual)

    async def charge_unknown(self, run_id, reserved):
        self.charged.append(reserved)


class VisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'VISION_ENABLED': '1',
            'AGENT_BUDGET_CONFIRMED': '1',
            'DEEPSEEK_API_KEY': 'test-key',
            'DEEPSEEK_BASE_URL': 'https://example.test/',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        sleep = patch('app.vision.asyncio.sleep', new=AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)
        FakeClient.calls.clear()
        self.budget = FakeBudget()

    async def transcribe(self, media_type, raw):
        return await vision.transcribe_image(media_type, raw, budget=self.budget, run_id='test-vision')

    def client(self, responses):
        return patch('app.vision.httpx.AsyncClient', lambda **kwargs: FakeClient(responses))

    def render(self, value=None):
        return patch('app.vision.render_page', return_value=value or {'status': 'ready', 'image': 'cG5n'})

    async def test_sends_low_effort_image_and_returns_text(self):
        transcript = '张小明\n教育背景：某大学 计算机科学与技术\n项目经历：负责需求整理与接口联调'
        with self.render(), self.client([FakeResponse(200, transcript)]):
            text = await self.transcribe('image/png', b'raw')
        self.assertEqual(text, transcript)
        url, kwargs = FakeClient.calls[0]
        self.assertEqual(url, 'https://example.test/chat/completions')
        self.assertEqual(kwargs['json']['model'], 'deepseek-flash')
        self.assertEqual(kwargs['json']['effort'], 'low')
        self.assertEqual(kwargs['json']['max_tokens'], 16000)
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-key')
        self.assertTrue(kwargs['json']['messages'][0]['content'][1]['image_url']['url']
                        .startswith('data:image/png;base64,'))

    async def test_retries_a_retryable_status_once(self):
        transcript = '重试后返回的识别文字，包含姓名、教育背景和项目经历等信息，内容完整可读'
        with self.render(), self.client([FakeResponse(503), FakeResponse(200, transcript)]):
            text = await self.transcribe('image/png', b'raw')
        self.assertEqual(text, transcript)
        self.assertEqual(len(FakeClient.calls), 2)

    async def test_gives_up_after_two_failures_and_never_retries_client_errors(self):
        with self.render(), self.client([FakeResponse(503), FakeResponse(503)]):
            self.assertIsNone(await self.transcribe('image/png', b'raw'))
        self.assertEqual(len(FakeClient.calls), 2)
        FakeClient.calls.clear()
        with self.render(), self.client([FakeResponse(400)]):
            self.assertIsNone(await self.transcribe('image/png', b'raw'))
        self.assertEqual(len(FakeClient.calls), 1)

    async def test_short_transcript_is_rejected(self):
        with self.render(), self.client([FakeResponse(200, '[无法识别]')]):
            self.assertIsNone(await self.transcribe('image/png', b'raw'))

    async def test_disabled_or_missing_key_skips_the_provider(self):
        with self.render(), self.client([]):
            with patch.dict(os.environ, {'VISION_ENABLED': '0'}):
                self.assertIsNone(await self.transcribe('image/png', b'raw'))
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
                self.assertIsNone(await self.transcribe('image/png', b'raw'))
        self.assertEqual(FakeClient.calls, [])

    async def test_unreadable_image_is_rejected_before_the_provider(self):
        with self.render({'status': 'failed'}), self.client([]):
            self.assertIsNone(await self.transcribe('image/png', b'raw'))
        self.assertEqual(FakeClient.calls, [])

    async def test_budget_exhaustion_prevents_the_provider_call(self):
        self.budget.allow = False
        with self.render(), self.client([]):
            self.assertIsNone(await self.transcribe('image/png', b'raw'))
        self.assertEqual(FakeClient.calls, [])

    async def test_missing_budget_or_confirmation_never_calls_provider(self):
        with self.render(), self.client([]):
            self.assertIsNone(await vision.transcribe_image('image/png', b'raw'))
            with patch.dict(os.environ, {'AGENT_BUDGET_CONFIRMED': '0'}):
                self.assertIsNone(await self.transcribe('image/png', b'raw'))
        self.assertEqual(FakeClient.calls, [])

    async def test_success_settles_usage_and_each_retry_is_reserved(self):
        text = '构造图片转录结果，包含教育经历、项目职责与技能清单等原始信息。'
        with self.render(), self.client([FakeResponse(503), FakeResponse(200, text)]):
            self.assertEqual(await self.transcribe('image/png', b'raw'), text)
        self.assertEqual(len(self.budget.reserved), 2)
        self.assertEqual(self.budget.charged, [14, 1])

    async def test_cancellation_keeps_unknown_charge(self):
        with self.render(), self.client([asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await self.transcribe('image/png', b'raw')
        self.assertEqual(self.budget.charged, [14])

    async def test_missing_usage_is_conservatively_charged(self):
        response = FakeResponse(200, '长' * 40)
        response.json = lambda: {'choices': [{'message': {'content': '长' * 40}, 'finish_reason': 'stop'}]}
        with self.render(), self.client([response]):
            self.assertEqual(await self.transcribe('image/png', b'raw'), '长' * 40)
        self.assertEqual(self.budget.charged, [14])

    async def test_truncated_transcript_keeps_its_text_and_marks_the_rest_unreadable(self):
        with self.render(), self.client([FakeResponse(200, '长' * 40, finish_reason='length')]):
            text = await self.transcribe('image/png', b'raw')
        self.assertEqual(text, '长' * 40 + '\n[无法识别]')

    async def test_truncated_transcript_does_not_repeat_the_unreadable_marker(self):
        body = '长' * 40 + '\n[无法识别]'
        with self.render(), self.client([FakeResponse(200, body, finish_reason='length')]):
            self.assertEqual(await self.transcribe('image/png', b'raw'), body)

    async def test_failure_reason_is_logged(self):
        with self.render(), self.client([FakeResponse(400)]):
            with self.assertLogs('app.vision', level='WARNING') as logs:
                self.assertIsNone(await self.transcribe('image/png', b'raw'))
        self.assertIn('http_400', '\n'.join(logs.output))
