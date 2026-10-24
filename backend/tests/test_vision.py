"""DeepSeek-Flash transcription checks against a fake provider; no network."""
import os
import unittest
from unittest.mock import AsyncMock, patch

from app import vision


class FakeResponse:
    def __init__(self, status, content=''):
        self.status_code = status
        self._content = content

    def json(self):
        return {'choices': [{'message': {'content': self._content}}]}


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
        if isinstance(item, Exception):
            raise item
        return item


class VisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'VISION_ENABLED': '1',
            'DEEPSEEK_API_KEY': 'test-key',
            'DEEPSEEK_BASE_URL': 'https://example.test/',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        sleep = patch('app.vision.asyncio.sleep', new=AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)
        FakeClient.calls.clear()

    def client(self, responses):
        return patch('app.vision.httpx.AsyncClient', lambda **kwargs: FakeClient(responses))

    def render(self, value=None):
        return patch('app.vision.render_page', return_value=value or {'status': 'ready', 'image': 'cG5n'})

    async def test_sends_low_effort_image_and_returns_text(self):
        transcript = '张小明\n教育背景：某大学 计算机科学与技术\n项目经历：负责需求整理与接口联调'
        with self.render(), self.client([FakeResponse(200, transcript)]):
            text = await vision.transcribe_image('image/png', b'raw')
        self.assertEqual(text, transcript)
        url, kwargs = FakeClient.calls[0]
        self.assertEqual(url, 'https://example.test/chat/completions')
        self.assertEqual(kwargs['json']['model'], 'deepseek-flash')
        self.assertEqual(kwargs['json']['effort'], 'low')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-key')
        self.assertTrue(kwargs['json']['messages'][0]['content'][1]['image_url']['url']
                        .startswith('data:image/png;base64,'))

    async def test_retries_a_retryable_status_once(self):
        transcript = '重试后返回的识别文字，包含姓名、教育背景和项目经历等信息，内容完整可读'
        with self.render(), self.client([FakeResponse(503), FakeResponse(200, transcript)]):
            text = await vision.transcribe_image('image/png', b'raw')
        self.assertEqual(text, transcript)
        self.assertEqual(len(FakeClient.calls), 2)

    async def test_gives_up_after_two_failures_and_never_retries_client_errors(self):
        with self.render(), self.client([FakeResponse(503), FakeResponse(503)]):
            self.assertIsNone(await vision.transcribe_image('image/png', b'raw'))
        self.assertEqual(len(FakeClient.calls), 2)
        FakeClient.calls.clear()
        with self.render(), self.client([FakeResponse(400)]):
            self.assertIsNone(await vision.transcribe_image('image/png', b'raw'))
        self.assertEqual(len(FakeClient.calls), 1)

    async def test_short_transcript_is_rejected(self):
        with self.render(), self.client([FakeResponse(200, '[无法识别]')]):
            self.assertIsNone(await vision.transcribe_image('image/png', b'raw'))

    async def test_disabled_or_missing_key_skips_the_provider(self):
        with self.render(), self.client([]):
            with patch.dict(os.environ, {'VISION_ENABLED': '0'}):
                self.assertIsNone(await vision.transcribe_image('image/png', b'raw'))
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
                self.assertIsNone(await vision.transcribe_image('image/png', b'raw'))
        self.assertEqual(FakeClient.calls, [])

    async def test_unreadable_image_is_rejected_before_the_provider(self):
        with self.render({'status': 'failed'}), self.client([]):
            self.assertIsNone(await vision.transcribe_image('image/png', b'raw'))
        self.assertEqual(FakeClient.calls, [])
