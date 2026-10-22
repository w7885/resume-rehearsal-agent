from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.agent.store import AgentStateStore

_INPUT_CENTS_PER_MTOK = 200
_OUTPUT_CENTS_PER_MTOK = 800
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    def __init__(self, code: str, message: str, recoverable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    max_tokens: int = 2000
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> "LLMSettings":
        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "2000")),
        )


@dataclass(frozen=True)
class Completion:
    data: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    cost_cents: int


def cost_cents(prompt_tokens: int, completion_tokens: int) -> int:
    cents = (
        prompt_tokens * _INPUT_CENTS_PER_MTOK
        + completion_tokens * _OUTPUT_CENTS_PER_MTOK
    ) / 1_000_000
    return max(1, math.ceil(cents)) if (prompt_tokens or completion_tokens) else 0


def estimate_cents(prompt_chars: int, max_tokens: int) -> int:
    input_tokens = max(1, prompt_chars // 2)
    return cost_cents(input_tokens, max_tokens)


class Budget:
    def __init__(
        self,
        store: AgentStateStore,
        session_id: str,
        *,
        limit_cents: int | None = None,
        day: str | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.limit_cents = (
            limit_cents
            if limit_cents is not None
            else int(os.getenv("BUDGET_DAILY_CENTS", "500"))
        )
        self.day = day or date.today().isoformat()
        self._lock = asyncio.Lock()

    def _load(self) -> tuple[dict[str, Any], int | None]:
        stored = self.store.get(self.session_id, "budget", self.day)
        if stored is None:
            return (
                {
                    "day": self.day,
                    "spent_cents": 0,
                    "reserved_cents": 0,
                    "entries": [],
                },
                None,
            )
        return dict(stored.doc), stored.revision

    async def reserve(self, cents: int) -> bool:
        async with self._lock:
            doc, revision = self._load()
            if doc["spent_cents"] + doc["reserved_cents"] + cents > self.limit_cents:
                return False
            doc["reserved_cents"] += cents
            self.store.put(self.session_id, "budget", self.day, doc, revision)
            return True

    async def settle(
        self, run_id: str, actual_cents: int, reserved_cents: int
    ) -> None:
        async with self._lock:
            doc, revision = self._load()
            doc["reserved_cents"] = max(0, doc["reserved_cents"] - reserved_cents)
            doc["spent_cents"] += actual_cents
            doc["entries"].append({"run_id": run_id, "cents": actual_cents})
            doc["entries"] = doc["entries"][-200:]
            self.store.put(self.session_id, "budget", self.day, doc, revision)

    async def charge_unknown(self, run_id: str, reserved_cents: int) -> None:
        async with self._lock:
            doc, revision = self._load()
            doc["reserved_cents"] = max(0, doc["reserved_cents"] - reserved_cents)
            doc["spent_cents"] += reserved_cents
            doc["entries"].append({"run_id": run_id, "cents": reserved_cents})
            doc["entries"] = doc["entries"][-200:]
            self.store.put(self.session_id, "budget", self.day, doc, revision)

    def snapshot(self) -> tuple[int, int]:
        doc, _ = self._load()
        return doc["spent_cents"], doc["reserved_cents"]


class DeepSeekClient:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        budget: Budget | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.budget = budget
        self._client = client

    def _payload(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        if self._client is not None:
            return await self._client.post(
                f"{self.settings.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        timeout = httpx.Timeout(
            connect=self.settings.connect_timeout,
            read=self.settings.read_timeout,
            write=10.0,
            pool=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"{self.settings.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        run_id: str = "unknown",
    ) -> Completion:
        if not self.settings.api_key:
            raise LLMError("config_error", "DEEPSEEK_API_KEY is empty", False)

        payload = self._payload(system, user)
        estimate = estimate_cents(len(system) + len(user), self.settings.max_tokens)

        for attempt in range(2):
            if self.budget is not None and not await self.budget.reserve(estimate):
                raise LLMError("budget_exhausted", "daily budget exhausted", False)
            try:
                response = await self._post(payload)
            except asyncio.CancelledError:
                if self.budget is not None:
                    await self.budget.charge_unknown(run_id, estimate)
                raise
            except httpx.RequestError as exc:
                if self.budget is not None:
                    await self.budget.charge_unknown(run_id, estimate)
                if attempt == 0:
                    await asyncio.sleep(0.8)
                    continue
                raise LLMError("llm_unavailable", str(exc), True) from exc

            if response.status_code >= 400:
                retryable = response.status_code in _RETRYABLE_STATUS
                if self.budget is not None:
                    if retryable:
                        await self.budget.charge_unknown(run_id, estimate)
                    else:
                        await self.budget.settle(run_id, 0, estimate)
                if retryable and attempt == 0:
                    await asyncio.sleep(0.8)
                    continue
                raise LLMError(
                    "llm_http_error",
                    f"HTTP {response.status_code}: {response.text[:200]}",
                    retryable,
                )

            try:
                body = response.json()
            except ValueError:
                if self.budget is not None:
                    await self.budget.charge_unknown(run_id, estimate)
                raise LLMError('invalid_json', 'provider returned invalid JSON', True) from None
            usage = body.get("usage") or {}
            if not isinstance(usage,dict) or any(type(usage.get(key)) is not int or usage[key]<0 for key in ('prompt_tokens','completion_tokens')):
                if self.budget is not None:
                    await self.budget.charge_unknown(run_id, estimate)
                raise LLMError('usage_unknown', 'provider usage is unavailable; reserved cost retained', False)
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            actual = cost_cents(prompt_tokens, completion_tokens)
            if self.budget is not None:
                await self.budget.settle(run_id, actual, estimate)

            content = body["choices"][0]["message"].get("content") or ""
            try:
                data = json.loads(_extract_json(content))
            except (ValueError, KeyError, IndexError) as exc:
                raise LLMError(
                    "invalid_json",
                    f"model did not return JSON ({exc}): len={len(content)} tail={content[-40:]}",
                    True,
                ) from exc
            return Completion(
                data=data,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_cents=actual,
            )

        raise LLMError("llm_unavailable", "retry exhausted", True)

    async def decide(
        self, *, system: str, user: str, run_id: str = "unknown"
    ) -> Completion:
        return await self.complete_json(system=system, user=user, run_id=run_id)

    async def route(
        self, *, system: str, user: str, run_id: str = "unknown"
    ) -> dict[str, Any]:
        completion = await self.complete_json(
            system=system, user=user, run_id=run_id
        )
        return completion.data


def _extract_json(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    if start == -1:
        return text
    try:
        _, end = json.JSONDecoder().raw_decode(text[start:])
        return text[start : start + end]
    except ValueError:
        end = text.rfind("}")
        if end > start:
            return text[start : end + 1]
        return text[start:]
