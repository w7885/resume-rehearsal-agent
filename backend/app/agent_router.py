"""Outer intent classification: one model call per turn, two orthogonal axes."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from app.agent.llm import LLMError
from app.agent.prompts import build_router_prompt
from app.agent.schemas import RouterOutput

FALLBACK_SEMANTIC = "router_semantic_fail"
FALLBACK_BUDGET = "router_budget_exhausted"
_RETRIES = 1


@dataclass(frozen=True)
class RouterResult:
    output: RouterOutput | None
    fallback_reason: str | None
    attempts: int


async def classify(llm, *, user: str, run_id: str = "unknown") -> RouterResult:
    """One classification, retried once on a semantic failure, then降级."""
    attempts = 0
    for attempt in range(_RETRIES + 1):
        attempts += 1
        try:
            raw = await llm.route(system=build_router_prompt(), user=user, run_id=run_id)
        except LLMError as error:
            if error.code == "budget_exhausted":
                return RouterResult(None, FALLBACK_BUDGET, attempts)
            if attempt == _RETRIES:
                return RouterResult(None, FALLBACK_SEMANTIC, attempts)
            continue
        try:
            return RouterResult(RouterOutput.model_validate(raw), None, attempts)
        except (ValidationError, ValueError):
            if attempt == _RETRIES:
                return RouterResult(None, FALLBACK_SEMANTIC, attempts)
    return RouterResult(None, FALLBACK_SEMANTIC, attempts)


def build_user_prompt(
    *,
    message: str,
    blocks: list[dict],
    focus_id: str | None,
    question: str | None,
    suggestions: list[dict],
    recent: list[str],
) -> str:
    lines = [f"用户消息：{message}", f"待答问题：{question or '无'}"]
    if focus_id:
        lines.append(f"当前焦点：{focus_id}")
    lines.append("简历段落：")
    for block in blocks:
        lines.append(f"- {block['id']}｜第{block['seq']}段｜{block.get('kind') or '未知'}｜{block['title']}")
    if not blocks:
        lines.append("- （无）")
    lines.append("待确认改写：")
    for item in suggestions:
        lines.append(f"- {item['id']}｜{item['title']}｜拟改为：{item['after']}")
    if not suggestions:
        lines.append("- （无）")
    if recent:
        lines.append("最近对话：")
        lines.extend(f"- {line}" for line in recent[-6:])
    return "\n".join(lines)
