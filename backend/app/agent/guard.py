from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.agent.memory import ExperienceDoc, effect_state, question_similarity
from app.agent.ports import SessionContext
from app.agent.schemas import (
    AskStep,
    FinishStep,
    ProposeChangeArgs,
    StepDecision,
    ToolStep,
)
from app.agent.tools import TOOL_SPECS, parse_args
from app.agent.questions import MAX_QUESTION_LENGTH

MAX_STEPS = 8
MAX_ROUNDS = 6
EFFECT_EXHAUSTED_TEXT = "效果已经问过两次，请基于职责、动作和方法照实改写。"
_EFFECT_CLAIMS = (
    "提升", "提高", "降低", "减少", "节省", "加快", "缩短",
    "改善", "避免", "替代", "方便", "效率",
)

_QUESTION_WORDS = re.compile(
    r"(什么|怎么|怎样|如何|为什么|哪|谁|多少|几点|几个|是否|有没有|能不能)"
)
_CONJUNCTIONS = ("并且", "以及", "同时", "还有", "而且")
_LEADING = ("是不是", "对吧", "你应该", "肯定是", "一定是", "难道")
_PRIVACY = (
    "身份证",
    "银行卡",
    "住址",
    "家庭地址",
    "薪资",
    "工资",
    "密码",
    "账号",
    "家庭成员",
    "照片",
    "手机号",
)
_JUDGMENTAL = ("太差", "很差", "不行", "你怎么连", "写得不好", "糟糕", "垃圾")
_UNANSWERABLE = ("DAU", "日活", "代码行数", "后台数据", "数据库密码", "内部数据")
_ROLE_WORDS = ("负责人", "主管", "主导", "架构师", "leader", "牵头", "经理")
_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
_CN_NUMBER = re.compile(r"[一二三四五六七八九十百千万两]+(?=[个项条次人页年月天%])")


def _cn_numbers(text: str):
    for match in _CN_NUMBER.finditer(text):
        tail = text[match.end() : match.end() + 1]
        if match.group() == "一" and tail in ("个", "次", "人"):
            continue
        yield match.group()


_PROPER_NOUN = re.compile(r"[A-Za-z][A-Za-z0-9+.#-]{2,}")


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    retryable: bool = True


@dataclass
class GuardResult:
    violation: Violation | None = None
    gaps: list[str] = field(default_factory=list)


@dataclass
class GuardState:
    session: SessionContext
    focus_doc: ExperienceDoc | None = None
    step_count: int = 0
    anchor_text: str = ""
    other_titles: list[str] | None = None
    source_texts: dict[str, str] = field(default_factory=dict)
    source_kinds: dict[str, str] = field(default_factory=dict)
    focus_block_markdown: str = ""
    state_revision_changed: bool = False
    recent_calls: list[str] = field(default_factory=list)
    direct_rewrite: bool = False


def call_signature(tool: str, args: dict) -> str:
    return f"{tool}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


def _evidence_text(state: GuardState) -> str:
    parts = [state.anchor_text]
    if state.focus_doc is not None:
        parts.extend(f.statement for f in state.focus_doc.facts if f.status == "active")
        parts.extend([state.focus_doc.role or "", state.focus_doc.period or ""])
    parts.extend(state.source_texts.values())
    parts.extend(state.session.session.jd_keywords)
    return "\n".join(parts)


def _personal_change_evidence(args: ProposeChangeArgs, state: GuardState) -> str:
    """Keep original draft facts, but require personal quotes for additions.

    JD describes requirements, raw chat may be a question or hypothetical, and
    model-maintained memory is not itself a new primary source.
    """
    original = state.focus_block_markdown or (state.focus_doc.raw_text if state.focus_doc else '')
    quotes = [ref.quote for ref in args.source_refs if state.source_kinds.get(ref.source_id) in {'resume', 'supplement'}]
    return '\n'.join([original, *quotes])


def _question_violation(step: AskStep, state: GuardState) -> Violation | None:
    text = step.message.strip()
    marks = text.count("？") + text.count("?")
    if marks != 1:
        return Violation("question_shape", "问题必须恰好一个问号")
    if len(text) > MAX_QUESTION_LENGTH:
        return Violation("question_too_long", f"问题与回答提示合计不能超过 {MAX_QUESTION_LENGTH} 字")
    if any(word in text for word in _CONJUNCTIONS):
        return Violation("question_multi", "一次只能问一个点")
    if len(_QUESTION_WORDS.findall(text)) > 1:
        return Violation("question_multi", "一次只能问一个点")
    if re.match(r'^(?:「[^」\n]*」|【[^】\n]*】|\[[^\]\n]*\]|“[^”\n]*”)\s*', text):
        return Violation("question_prefix", "直接自然提问，不在开头添加人名、标题或原文引用标签")
    if any(word in text for word in _LEADING):
        return Violation("question_leading", "问题不能诱导或预设答案")
    if any(word in text for word in _PRIVACY):
        return Violation("question_private", "不能索取隐私信息")
    if any(word in text for word in _JUDGMENTAL):
        return Violation("question_judgmental", "不能评判或施压")
    if any(word.lower() in text.lower() for word in _UNANSWERABLE):
        return Violation("question_unanswerable", "不能要求用户查后台数据")

    if state.focus_doc is not None:
        recent = state.focus_doc.questions[-3:]
        if any(question_similarity(q.text, text) >= 0.6 for q in recent):
            return Violation("question_repeat", "最近已经问过类似问题")
        if step.slot is not None:
            exempt_effect_followup = (
                step.slot == "effect"
                and effect_state(state.focus_doc) == "followup"
            )
            if step.slot in state.focus_doc.declined and not exempt_effect_followup:
                return Violation("question_declined", "该维度已被用户拒绝")
            if step.slot == "confirm":
                return None

    for title in state.other_titles or []:
        if len(title) >= 2 and title in text:
            return Violation("question_cross_segment", "不能提及其他经历")

    compact = re.sub(r"\s+", "", _evidence_text(state))
    for number in _NUMBER.findall(text):
        if number not in compact:
            return Violation("question_new_number", "问题里不能出现事实中没有的数字")
    return None


def _evidence_gate(
    args: ProposeChangeArgs, state: GuardState
) -> GuardResult:
    for ref in args.source_refs:
        source = state.source_texts.get(ref.source_id)
        if source is None or (ref.quote and ref.quote not in source):
            return GuardResult(
                violation=Violation(
                    "invalid_source_ref",
                    f"来源 {ref.source_id} 无法定位引用“{ref.quote[:20]}”",
                    True,
                )
            )

    block_text = state.focus_block_markdown
    if not block_text and state.focus_doc is not None:
        block_text = state.focus_doc.raw_text
    if args.before is not None and block_text and args.before.strip() != block_text.strip():
        return GuardResult(
            violation=Violation("stale_anchor", "before 必须逐字复制当前整段原文", True)
        )
    if not args.after.strip():
        return GuardResult(violation=Violation("empty_after", "改文不能为空", True))
    anchor = args.before if args.before is not None else block_text
    if args.after.strip() == anchor.strip():
        return GuardResult(violation=Violation("no_change", "改文与原文相同", True))
    if not args.reason.strip():
        return GuardResult(violation=Violation("missing_reason", "请填写修改理由", True))
    if not args.source_refs:
        return GuardResult(violation=Violation("missing_source", "改写必须引用来源", True))
    if state.source_kinds and not any(
        state.source_kinds.get(ref.source_id) in ("resume", "supplement")
        for ref in args.source_refs
    ):
        return GuardResult(
            violation=Violation("source_role", "必须引用简历或用户补充", True)
        )

    evidence = _personal_change_evidence(args, state)
    compact = re.sub(r"\s+", "", evidence)
    for number in _NUMBER.findall(args.after) + list(_cn_numbers(args.after)):
        if number not in compact:
            return GuardResult(
                violation=Violation(
                    "unsupported_number", f"数字 {number} 没有依据", True
                )
            )

    lowered = evidence.lower()
    for word in _ROLE_WORDS:
        if word in args.after and word.lower() not in lowered:
            return GuardResult(
                violation=Violation(
                    "role_inflation", f"职级「{word}」没有依据", False
                )
            )

    gaps = list(args.gaps)
    # ponytail: proper nouns only warn; a glossary check can replace this later.
    for noun in _PROPER_NOUN.findall(args.after):
        if noun not in evidence and f"缺少依据：{noun}" not in gaps:
            gaps.append(f"缺少依据：{noun}")
    return GuardResult(gaps=gaps)


def check(decision: StepDecision, state: GuardState) -> GuardResult:
    if state.state_revision_changed:
        return GuardResult(
            violation=Violation("stale_state", "会话已变化，本次结果作废", False)
        )
    if state.step_count >= MAX_STEPS:
        return GuardResult(
            violation=Violation("step_limit", "已达到步数上限", False)
        )

    if isinstance(decision, FinishStep):
        return GuardResult()

    if isinstance(decision, AskStep):
        if state.focus_doc is not None and state.focus_doc.no_fact_streak >= 2:
            return GuardResult(
                violation=Violation(
                    "ask_blocked",
                    "连续两轮无新增事实，本轮必须改写或结束",
                    True,
                )
            )
        if state.focus_doc is not None and state.focus_doc.round >= MAX_ROUNDS:
            return GuardResult(
                violation=Violation("round_limit", "已达到轮次上限", False)
            )
        if state.focus_doc is not None and effect_state(state.focus_doc) != "confirmed":
            effect_questions = sum(
                1
                for question in state.focus_doc.questions
                if question.slot == "effect"
            )
            if effect_questions >= 2:
                return GuardResult(
                    violation=Violation(
                        "effect_exhausted", EFFECT_EXHAUSTED_TEXT, True
                    )
                )
        violation = _question_violation(decision, state)
        return GuardResult(violation=violation)

    if not isinstance(decision, ToolStep):
        return GuardResult(
            violation=Violation("invalid_decision", "未知动作", False)
        )

    if decision.tool not in TOOL_SPECS:
        return GuardResult(
            violation=Violation(
                "tool_not_allowed", f"工具 {decision.tool} 不在白名单", True
            )
        )
    if call_signature(decision.tool, decision.args) in state.recent_calls:
        return GuardResult(
            violation=Violation("duplicate_tool", "该工具已用同样参数调用过", True)
        )

    try:
        args = parse_args(decision)
    except ValidationError as exc:
        return GuardResult(
            violation=Violation("invalid_tool_args", str(exc)[:200], True)
        )

    experience_id = getattr(args, "experience_id", None) or getattr(args, "target", None)
    if experience_id is not None and experience_id not in state.session.session.experience_order:
        return GuardResult(
            violation=Violation(
                "scope_violation", f"{experience_id} 不属于本会话", False
            )
        )

    if (
        state.focus_doc is not None
        and state.focus_doc.round >= MAX_ROUNDS
        and decision.tool != "propose_change"
    ):
        return GuardResult(
            violation=Violation("round_limit", "轮次已满，只能改写", False)
        )

    if decision.tool == "propose_change":
        if state.focus_doc is not None:
            if getattr(args, "target", None) != state.focus_doc.experience_id:
                return GuardResult(
                    violation=Violation(
                        "wrong_target", "只能改写当前这一段，不能改其他段落。", True
                    )
                )
            answered = any(
                question.outcome in ("answered", "declined")
                and question.slot != "confirm"
                for question in state.focus_doc.questions
            )
            if not answered and not state.direct_rewrite:
                return GuardResult(
                    violation=Violation(
                        "ask_first", "当前段还没有问过用户，先问一个具体的职责问题。", True
                    )
                )
            effect = effect_state(state.focus_doc)
            if effect == "followup":
                return GuardResult(
                    violation=Violation(
                        "ask_effect_followup",
                        "用户只说明了使用情况，请再追问一次具体变化；仍没有就照实改写。",
                        True,
                    )
                )
            if effect == "missing":
                return GuardResult(
                    violation=Violation(
                        "ask_effect",
                        "改写前必须先问这段经历带来的实际效果，不能把“有人使用/上线”当成效果。",
                        True,
                    )
                )
            if effect == "absent":
                anchor = (
                    args.before
                    if args.before is not None
                    else state.focus_block_markdown
                )
                claims = [
                    word
                    for word in _EFFECT_CLAIMS
                    if word in args.after and word not in anchor
                ]
                if claims:
                    return GuardResult(
                        violation=Violation(
                            "effect_claim_without_evidence",
                            "用户已说明没有可确认的实际效果，改写只写职责、动作和方法，不要写效果结论。",
                            True,
                        )
                    )
        return _evidence_gate(args, state)

    return GuardResult()
