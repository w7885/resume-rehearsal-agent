"""DeepSeek-V4.1-Flash image transcription. No database and no persistence."""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from app.page_renderer import render_page
from app.agent.llm import cost_cents

LOGGER = logging.getLogger(__name__)

PROMPT = (
    "只转录图中可见文字，保留换行与栏目顺序。"
    "先判断区块和栏目边界：通栏标题及其正文按原图从上到下读取；"
    "同一区块存在左右独立栏目时，先完整读取左栏标题及全部正文，再读取右栏标题及全部正文，"
    "多栏依次从左到右；每栏内部从上到下，续行紧跟所属条目。"
    "不得把左右栏处在同一横排的文字拼成一句，不得将一栏的正文放到另一栏标题下。"
    "表格保留行列对应关系，不按独立栏目拆读；日期、数字与其对应条目保持关联。"
    "看不清文字或无法确定栏目归属时在相应位置写 [无法识别]，不得猜测归属；"
    "不得推断、补全或润色；不得描述人脸。"
)
MIN_CHARS = 30
# Reasoning tokens share this budget and have overrun 8000 on their own, which
# truncated the transcript; the visible text itself stays around 1500 tokens.
MAX_TOKENS = 16000
_RETRYABLE = {429, 500, 502, 503, 504}
_RETRY_DELAY = 0.5


def _failed(reason, run_id):
    LOGGER.warning('image transcription failed: %s (run %s)', reason, run_id)
    return None


def configured() -> bool:
    return (os.getenv("VISION_ENABLED") == "1" and os.getenv('AGENT_BUDGET_CONFIRMED') == '1'
            and os.getenv('VISION_MODEL', 'deepseek-flash') == 'deepseek-flash'
            and bool(os.getenv("DEEPSEEK_API_KEY", "").strip()))


async def transcribe_image(media_type: str, raw: bytes, *, budget=None, run_id='vision') -> str | None:
    """Return visible text, or None when disabled, unavailable or unreadable."""
    if not configured():
        return _failed('not_configured', run_id)
    if budget is None:
        return _failed('no_budget', run_id)
    page = await asyncio.to_thread(render_page, media_type, raw, 1)
    if page.get("status") != "ready":
        return _failed('render_failed', run_id)
    image = page["image"]
    payload = {
        "model": os.getenv("VISION_MODEL", "deepseek-flash"),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
        ]}],
        # Transcription needs visible text, not a long reasoning pass. Use the
        # documented switch: the old `effort` field did not select this mode.
        "thinking": {"type": "disabled"},
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
                return _failed('budget_exhausted', run_id)
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
                return _transcript(response, run_id)
            if attempt or (response is not None and response.status_code not in _RETRYABLE):
                return _failed('transport_error' if response is None else f'http_{response.status_code}', run_id)
            await asyncio.sleep(_RETRY_DELAY)
    return _failed('retries_exhausted', run_id)


def _transcript(response: httpx.Response, run_id) -> str | None:
    try:
        choice = response.json()['choices'][0]
        truncated = choice.get('finish_reason') == 'length'
        text = (choice["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return _failed('unreadable_response', run_id)
    if len(text) < MIN_CHARS:
        return _failed('empty_transcript', run_id)
    if len(text) > 50000:
        return _failed('transcript_too_long', run_id)
    if truncated:
        # ponytail: reuse the prompt's own unreadable marker instead of adding a
        # reason channel, so the intake already stores this as a partial result.
        LOGGER.warning('image transcript truncated at %s tokens (run %s)', MAX_TOKENS, run_id)
        if '[无法识别]' not in text:
            text += '\n[无法识别]'
    return text
