"""DeepSeek-V4.1-Flash image transcription. No database and no persistence."""
from __future__ import annotations

import asyncio
import os

import httpx

from app.page_renderer import render_page
from app.agent.llm import cost_cents

PROMPT = (
    "只转录图中可见文字，保留换行与栏目顺序；看不清写 [无法识别]；"
    "不得推断、补全或润色；不得描述人脸。"
)
MIN_CHARS = 30
MAX_TOKENS = 8000
_RETRYABLE = {429, 500, 502, 503, 504}
_RETRY_DELAY = 0.5


def configured() -> bool:
    return (os.getenv("VISION_ENABLED") == "1" and os.getenv('AGENT_BUDGET_CONFIRMED') == '1'
            and os.getenv('VISION_MODEL', 'deepseek-flash') == 'deepseek-flash'
            and bool(os.getenv("DEEPSEEK_API_KEY", "").strip()))


async def transcribe_image(media_type: str, raw: bytes, *, budget=None, run_id='vision') -> str | None:
    """Return visible text, or None when disabled, unavailable or unreadable."""
    if not configured() or budget is None:
        return None
    page = await asyncio.to_thread(render_page, media_type, raw, 1)
    if page.get("status") != "ready":
        return None
    image = page["image"]
    payload = {
        "model": os.getenv("VISION_MODEL", "deepseek-flash"),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
        ]}],
        # The default high effort spends the whole token budget on reasoning
        # and returns empty content, so transcription always asks for low.
        "effort": "low",
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
    }
    url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + os.getenv("DEEPSEEK_API_KEY", "").strip()}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        for attempt in (0, 1):
            # Official Flash image cap: 1024 input tokens/image. Include a text margin,
            # and conservatively use peak uncached rates (2/8 CNY per million tokens).
            reserved = cost_cents(2048, MAX_TOKENS)
            if not await budget.reserve(reserved):
                return None
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError:
                response = None
            except BaseException:
                await budget.charge_unknown(run_id, reserved)
                raise
            try:
                usage = response.json().get('usage') if response is not None and response.status_code == 200 else None
                valid = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens'))
                if valid:
                    await budget.settle(run_id, cost_cents(usage['prompt_tokens'], usage['completion_tokens']), reserved)
                else:
                    await budget.charge_unknown(run_id, reserved)
            except (ValueError, TypeError, AttributeError):
                await budget.charge_unknown(run_id, reserved)
            if response is not None and response.status_code == 200:
                return _transcript(response)
            if attempt or (response is not None and response.status_code not in _RETRYABLE):
                return None
            await asyncio.sleep(_RETRY_DELAY)
    return None


def _transcript(response: httpx.Response) -> str | None:
    try:
        choice = response.json()['choices'][0]
        if choice.get('finish_reason') == 'length':
            return None
        text = (choice["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    return text if MIN_CHARS <= len(text) <= 50000 else None
