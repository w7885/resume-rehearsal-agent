from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.agent.memory import (
    ExperienceDoc,
    next_unconfirmed,
    normalize_question,
    question_fingerprint,
)
from app.agent.ports import Phase, SessionContext
from app.agent.prompts import build_route_prompt
from app.agent.schemas import CandidateBrief, Clarification, RouteDecision

_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_ORDINAL = re.compile(r"第\s*([一二三四五六七八九十两\d]+)\s*[段个]")
_KIND = re.compile(r"(项目|实习|工作)")
_CONFIRM = {"满意", "可以", "就这样", "保存这段", "用这个", "好", "行", "可以了", "没问题"}
_SKIP = {"跳过", "不知道", "没有", "就这些", "不记得", "先跳过", "跳过这段"}
_GLOBAL = {"都改", "整体", "全部", "整体改", "都改改", "全部改", "都改一下"}
_MODIFY = ("但", "不过", "改成", "改短", "改一下", "第二句", "第一条", "换个说法")
_CANCEL = {"算了", "取消", "不改了"}
_DELEGATE = {"随便", "你决定", "都可以", "你看着办"}
_CONTINUE = {"继续", "下一段", "继续下一段", "下一个", "下一步", "接着来"}
_TITLE_MATCH_LIMIT = 5


def _cn_to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        left, _, right = token.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(token)


@dataclass
class RouteResult:
    action: str
    target_experience_id: str | None = None
    scope: str = "unknown"
    clarification: Clarification | None = None
    pending_clarification: dict | None = None
    clear_pending: bool = False
    echo: str | None = None
    rationale: str = ""
    used_model: bool = False
    drop: bool = False


def _candidate(doc: ExperienceDoc, phase: Phase) -> CandidateBrief:
    return CandidateBrief(
        experience_id=doc.experience_id,
        seq=doc.seq,
        kind=doc.kind,
        title=doc.title,
        role=doc.role,
        phase=phase,
    )


def _candidates(
    docs: list[ExperienceDoc], phases: dict[str, Phase]
) -> list[CandidateBrief]:
    return [_candidate(doc, phases.get(doc.experience_id, "pending")) for doc in docs]


def _echo(doc: ExperienceDoc) -> str:
    return f"已切到第 {doc.seq} 段「{doc.title}」"


def _template(code: str, ref: str, kind: str | None, total: int, title: str = "") -> str:
    kind_text = {"project": "项目", "work": "实习"}.get(kind or "", "经历")
    if code == "out_of_range":
        return f"你提到“{ref}”，目前只有 {total} 段{kind_text}。要改哪一段？"
    if code == "type_empty":
        return f"目前简历里没有{kind_text}经历，只有这些："
    if code == "ambiguous_relative":
        return f"“{ref}”可能指这两段，选哪个？"
    if code == "ambiguous_title":
        return f"有 {total} 段都叫「{title}」，是哪一段？"
    if code == "multiple_match":
        return f"找到 {total} 段相关的，选哪个？"
    return "简历里还没识别到项目/实习，先上传或粘贴一段经历吧。"


def _clarify(
    code: str,
    ref: str,
    docs: list[ExperienceDoc],
    phases: dict[str, Phase],
    *,
    kind: str | None = None,
    total: int | None = None,
    title: str = "",
) -> RouteResult:
    candidates = _candidates(docs[:5], phases)
    question = _template(code, ref, kind, total if total is not None else len(docs), title)
    clarification = Clarification(
        code=code,
        question=question,
        quoted_reference=ref[:12],
        candidates=candidates,
    )
    pending = {
        "code": code,
        "fingerprint": question_fingerprint(question),
        "quoted_reference": ref[:12],
        "candidates": [c.experience_id for c in candidates],
        "attempts": 1,
    }
    return RouteResult(
        action="clarify",
        clarification=clarification,
        pending_clarification=pending,
        rationale="目标段不明确",
    )


def _focus_doc(
    message: str,
    docs: list[ExperienceDoc],
    phases: dict[str, Phase],
    focus_id: str | None,
) -> RouteResult | None:
    ordinal_match = _ORDINAL.search(message)
    if ordinal_match:
        ordinal = _cn_to_int(ordinal_match.group(1))
        kind_match = _KIND.search(message)
        kind = None
        if kind_match:
            kind = "project" if kind_match.group(1) == "项目" else "work"
        reference = ordinal_match.group(0) + (
            kind_match.group(1) if kind_match else ""
        )
        pool = [doc for doc in docs if kind is None or doc.kind == kind]
        if not pool and kind is not None:
            return _clarify(
                "type_empty",
                reference,
                docs,
                phases,
                kind=kind,
                total=0,
            )
        if ordinal is None or ordinal < 1 or ordinal > len(pool):
            return _clarify(
                "out_of_range",
                reference,
                pool or docs,
                phases,
                kind=kind,
                total=len(pool),
            )
        doc = sorted(pool, key=lambda item: item.seq)[ordinal - 1]
        return RouteResult(
            action="focus",
            target_experience_id=doc.experience_id,
            scope="experience",
            echo=_echo(doc),
            rationale="序号定位",
        )

    if message in {"最后一段", "最后那个"} or message.endswith(("最后一段", "最后那个")):
        if not docs:
            return None
        doc = sorted(docs, key=lambda item: item.seq)[-1]
        return RouteResult(
            action="focus",
            target_experience_id=doc.experience_id,
            scope="experience",
            echo=_echo(doc),
            rationale="相对定位",
        )

    if message in {"前一个", "上一个", "刚才那段"} or message.endswith(
        ("前一个", "上一个", "刚才那段")
    ):
        ordered = sorted(docs, key=lambda item: item.seq)
        focus = next(
            (doc for doc in ordered if doc.experience_id == focus_id),
            None,
        )
        if focus is not None:
            index = ordered.index(focus)
            if index > 0:
                pair = [ordered[index - 1], focus]
                return _clarify(
                    "ambiguous_relative",
                    message,
                    pair,
                    phases,
                    kind=focus.kind,
                    total=2,
                )
        if ordered:
            doc = ordered[-1]
            return RouteResult(
                action="focus",
                target_experience_id=doc.experience_id,
                scope="experience",
                echo=_echo(doc),
                rationale="相对定位兜底",
            )

    matched = [doc for doc in docs if len(doc.title) >= 2 and doc.title in message]
    if len(matched) == 1:
        doc = matched[0]
        return RouteResult(
            action="focus",
            target_experience_id=doc.experience_id,
            scope="experience",
            echo=_echo(doc),
            rationale="标题命中",
        )
    if len(matched) > 1:
        return _clarify(
            "ambiguous_title",
            message[:12],
            matched[:_TITLE_MATCH_LIMIT],
            phases,
            total=len(matched),
            title=matched[0].title,
        )

    return None


def _pending_reply(
    message: str,
    pending: dict,
    docs: list[ExperienceDoc],
    phases: dict[str, Phase],
) -> RouteResult | None:
    by_id = {doc.experience_id: doc for doc in docs}
    candidate_ids = [
        experience_id
        for experience_id in pending.get("candidates", [])
        if experience_id in by_id
    ]
    candidates = [by_id[experience_id] for experience_id in candidate_ids]

    index: int | None = None
    if re.fullmatch(r"[1-9]", message):
        index = int(message) - 1
    else:
        match = re.fullmatch(r"第?([一二三四五])个?", message)
        if match:
            parsed = _cn_to_int(match.group(1))
            index = None if parsed is None else parsed - 1
    if index is not None and 0 <= index < len(candidates):
        doc = candidates[index]
        return RouteResult(
            action="focus",
            target_experience_id=doc.experience_id,
            scope="experience",
            clear_pending=True,
            echo=_echo(doc),
            rationale="澄清序号",
        )

    matched = [doc for doc in candidates if len(doc.title) >= 2 and doc.title in message]
    if len(matched) == 1:
        doc = matched[0]
        return RouteResult(
            action="focus",
            target_experience_id=doc.experience_id,
            scope="experience",
            clear_pending=True,
            echo=_echo(doc),
            rationale="澄清标题命中",
        )

    if message in _GLOBAL:
        return RouteResult(
            action="global_edit",
            scope="whole_resume",
            clear_pending=True,
            rationale="用户要求整体修改",
        )
    if message in _CANCEL:
        return RouteResult(action="cancel", clear_pending=True, rationale="用户取消澄清")
    if message in _DELEGATE and candidates:
        doc = min(
            candidates,
            key=lambda item: (
                phases.get(item.experience_id) == "confirmed",
                item.seq,
            ),
        )
        return RouteResult(
            action="focus",
            target_experience_id=doc.experience_id,
            scope="experience",
            clear_pending=True,
            echo=f"先改第 {doc.seq} 段「{doc.title}」，想换随时说",
            rationale="用户委托代选",
        )

    return None


def _model_user_prompt(
    message: str,
    docs: list[ExperienceDoc],
    phases: dict[str, Phase],
    focus_id: str | None,
    has_pending_question: bool,
) -> str:
    lines = [f"用户消息：{message}", f"是否有待答问题：{'是' if has_pending_question else '否'}"]
    if focus_id:
        doc = next((d for d in docs if d.experience_id == focus_id), None)
        if doc:
            lines.append(
                f"当前焦点：{doc.seq}. {doc.kind}｜{doc.title}｜"
                f"{phases.get(doc.experience_id, 'pending')}"
            )
    lines.append("候选经历：")
    for doc in docs:
        lines.append(
            f"- {doc.experience_id}｜{doc.seq}. {doc.kind}｜{doc.title}｜"
            f"{phases.get(doc.experience_id, 'pending')}"
        )
    if not docs:
        lines.append("- （无）")
    return "\n".join(lines)


async def route_message(
    *,
    message: str,
    ctx: SessionContext,
    docs: list[ExperienceDoc],
    phases: dict[str, Phase],
    pending: dict | None,
    has_pending_question: bool,
    llm,
) -> RouteResult:
    text = message.strip()
    if not text:
        return RouteResult(action="drop", drop=True, rationale="空消息")

    if pending:
        reply = _pending_reply(text, pending, docs, phases)
        if reply is not None:
            return reply

    if text in _CONTINUE and not has_pending_question:
        ordered = sorted(docs, key=lambda item: item.seq)
        by_id = {doc.experience_id: doc for doc in ordered}
        nxt = next_unconfirmed(
            [doc.experience_id for doc in ordered],
            by_id,
            phases,
            ctx.session.current_focus_id,
        )
        return RouteResult(
            action="advance",
            target_experience_id=nxt,
            scope="experience",
            echo=_echo(by_id[nxt]) if nxt else None,
            rationale="继续下一段",
        )

    if text in _CONFIRM:
        return RouteResult(
            action="confirm",
            scope="experience",
            target_experience_id=ctx.session.current_focus_id,
            rationale="确认词",
        )
    if any(word in text for word in _MODIFY) and (
        text in _CONFIRM or text.startswith(("满意", "可以", "好"))
    ):
        return RouteResult(
            action="correct",
            scope="experience",
            target_experience_id=ctx.session.current_focus_id,
            rationale="确认同时要求修改",
        )
    if text in _SKIP or text.startswith("跳过"):
        return RouteResult(
            action="skip",
            scope="experience",
            target_experience_id=ctx.session.current_focus_id,
            rationale="跳过词",
        )
    if text in _GLOBAL:
        return RouteResult(action="global_edit", scope="whole_resume", rationale="整体修改")
    if text in _CANCEL:
        return RouteResult(action="cancel", clear_pending=True, rationale="取消")

    focused = _focus_doc(text, docs, phases, ctx.session.current_focus_id)
    if focused is not None:
        if pending:
            focused.clear_pending = True
        return focused

    if has_pending_question:
        return RouteResult(
            action="answer",
            scope="experience",
            target_experience_id=ctx.session.current_focus_id,
            rationale="回答待答问题",
        )

    user_prompt = _model_user_prompt(
        text, docs, phases, ctx.session.current_focus_id, has_pending_question
    )
    try:
        raw = await llm.route(system=build_route_prompt(), user=user_prompt)
        decision = RouteDecision.model_validate(raw)
    except (ValidationError, ValueError):
        if not docs:
            return RouteResult(
                action="clarify",
                clarification=Clarification(
                    code="no_experience",
                    question=_template("no_experience", "", None, 0),
                    quoted_reference=text[:12],
                ),
                pending_clarification={
                    "code": "no_experience",
                    "fingerprint": question_fingerprint(text),
                    "quoted_reference": text[:12],
                    "candidates": [],
                    "attempts": 1,
                },
                rationale="模型路由失败且无经历",
            )
        return RouteResult(
            action="answer",
            scope="experience",
            target_experience_id=ctx.session.current_focus_id,
            rationale="模型路由失败兜底",
        )

    result = RouteResult(
        action=decision.action,
        target_experience_id=decision.target_experience_id,
        scope=decision.scope,
        rationale=decision.rationale,
        used_model=True,
        clarification=decision.clarification,
    )
    if result.action == "focus":
        target = next(
            (doc for doc in docs if doc.experience_id == result.target_experience_id),
            None,
        )
        if target is None:
            return RouteResult(
                action="answer",
                scope="experience",
                target_experience_id=ctx.session.current_focus_id,
                rationale="模型给出了未知段，按回答处理",
                used_model=True,
            )
        result.echo = _echo(target)

    if result.clarification is not None:
        result.clarification = _sanitize(decision.clarification, text, docs, phases)
        result.action = "clarify"
        result.pending_clarification = {
            "code": result.clarification.code,
            "fingerprint": question_fingerprint(result.clarification.question),
            "quoted_reference": result.clarification.quoted_reference,
            "candidates": [
                candidate.experience_id for candidate in result.clarification.candidates
            ],
            "attempts": 1,
        }
    return result


def _sanitize(
    clarification: Clarification,
    message: str,
    docs: list[ExperienceDoc],
    phases: dict[str, Phase],
) -> Clarification:
    valid_ids = {doc.experience_id for doc in docs}
    candidates = [
        candidate
        for candidate in clarification.candidates
        if candidate.experience_id in valid_ids
    ][:5]
    if not candidates and docs:
        candidates = _candidates(docs[:5], phases)

    reference = clarification.quoted_reference
    if not reference or reference not in message:
        reference = message[:12]

    text = normalize_question(clarification.question)
    if (
        len(clarification.question) > 60
        or clarification.question.count("？") + clarification.question.count("?") != 1
        or not text
    ):
        code = clarification.code if candidates or code_is_empty(clarification.code) else "multiple_match"
        question = _template(code, reference, None, len(candidates))
    else:
        question = clarification.question

    return Clarification(
        code=clarification.code,
        question=question,
        quoted_reference=reference,
        candidates=candidates,
    )


def code_is_empty(code: str) -> bool:
    return code == "no_experience"
