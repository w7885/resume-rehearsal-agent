from __future__ import annotations

from app.agent.memory import ExperienceDoc, Slot, effect_state, supported_experience_slots
from app.agent.ports import Phase, SessionContext
from app.agent.section_policy import EXPERIENCE_KINDS, RULES

_SLOT_ORDER: list[Slot] = [
    "responsibility",
    "context",
    "action",
    "result",
    "effect",
    "evidence",
    "tech",
    "details", "strength", "preference",
]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _focus_block(doc: ExperienceDoc) -> str:
    lines = [
        f"类型：{doc.kind}",
        f"标题：{doc.title}",
        f"段落编号（blockId）：{doc.block_id or doc.experience_id}",
        f"段落版本（blockRevision）：{doc.block_revision}",
        f"原文（整段，before 必须逐字复制；最多 4000 字）：\n{_clip(doc.raw_text, 4000)}",
    ]
    if doc.kind in EXPERIENCE_KINDS:
        lines[2:2] = [f"角色：{doc.role or '未填写'}", f"时间：{doc.period or '未填写'}"]
    if doc.source_ids:
        lines.append(f"来源编号：{', '.join(doc.source_ids)}")
    return "\n".join(lines)


def _facts_block(doc: ExperienceDoc) -> str:
    active = [fact for fact in doc.facts if fact.status == "active"]
    if not active:
        return "（暂无）"
    lines: list[str] = []
    for slot in _SLOT_ORDER:
        slot_facts = [fact for fact in active if fact.slot == slot]
        slot_facts.sort(key=lambda fact: fact.created_at)
        for fact in slot_facts[-8:]:
            lines.append(f"- [{slot}] fact_id={fact.id}：{fact.statement}")
            if fact.source_refs:
                lines.append('  原始依据：' + '、'.join(f'[{r.source_id}，版本 {r.source_revision}]' for r in fact.source_refs[:2]))
    return "\n".join(lines) if lines else "（暂无）"


def _questions_block(doc: ExperienceDoc) -> str:
    lines: list[str] = []
    pending = [q for q in doc.questions if q.outcome == "asked"]
    for question in pending[-3:]:
        lines.append(f"- 未答：{question.text}")
    recent = [q for q in doc.questions if q.outcome != "asked"][-3:]
    for question in recent:
        lines.append(f"- 已问（{question.outcome}）：{question.text}")
    if doc.declined:
        lines.append(f"- 已拒绝的维度：{', '.join(doc.declined)}")
    return "\n".join(lines) if lines else "（暂无）"


def _effect_block(doc: ExperienceDoc) -> str:
    if doc.kind not in EXPERIENCE_KINDS:
        return '本节不适用经历效果采访；按当前段类型的处理规则整理。'
    state = effect_state(doc)
    if state == "confirmed":
        statement = next((fact.statement for fact in reversed(doc.facts)
                          if fact.slot == "effect" and fact.status == "active"), doc.raw_text)
        return f"已确认：{_clip(statement, 200)}"
    if state == "followup":
        return "已追问 1 次，仍待具体变化（最多再问 1 次）"
    if state == "absent":
        return "用户已说明没有可确认的实际效果：照实改写，不写效果类结论"
    return "尚未询问"


def _sources_block(ctx: SessionContext, doc: ExperienceDoc) -> str:
    ids = set(doc.source_ids)
    rows = [
        source
        for source in ctx.sources
        if (source.source_id in ids or doc.kind in {'summary', 'skills'}) and source.kind in ("resume", "supplement")
    ]
    if not rows:
        return "（暂无）"
    needed = {r.source_id for f in doc.facts if f.status == 'active' for r in f.source_refs}
    important = [s for s in rows if s.source_id in needed]
    originals = [s for s in rows if s.kind == 'resume' and s.source_id not in needed]
    if doc.kind in {'summary', 'skills'}:
        import re
        originals.sort(key=lambda s: (s.source_id not in ids, not bool(re.search(r'负责|实现|开发|测试|整理|协调|分析|组织', s.text))))
    supplements = [s for s in rows if s.kind == 'supplement' and s.source_id not in needed]
    selected = [*important, *originals[:6], *supplements[-6:]][:12]
    return "\n\n".join(
        f"[{source.source_id}]（{source.kind}，版本 {source.source_revision}）\n{_clip(source.text, 800)}"
        for source in selected
    )


def _messages_block(ctx: SessionContext) -> str:
    if not ctx.messages:
        return "（暂无）"
    lines = []
    for message in ctx.messages[-3:]:
        role = "用户" if message.role == "user" else "助手"
        lines.append(f"{role}：{_clip(message.text, 400)}")
    return "\n".join(lines)


def assemble_context(
    ctx: SessionContext,
    experiences: list[ExperienceDoc],
    phases: dict[str, Phase],
    focus_experience_id: str | None,
    session_goal: str = "",
) -> str:
    by_id = {doc.experience_id: doc for doc in experiences}
    focus = by_id.get(focus_experience_id or "")

    experience_lines = []
    for experience_id in ctx.session.experience_order:
        doc = by_id.get(experience_id)
        if doc is None:
            continue
        phase = phases.get(experience_id, "pending")
        experience_lines.append(
            f"- {doc.seq}. {doc.kind}｜{doc.title}｜"
            f"experience_id={doc.experience_id}｜{phase}｜round={doc.round}"
        )
    experience_block = "\n".join(experience_lines) or "（暂无）"

    brake = ""
    if focus is not None and focus.no_fact_streak >= 2:
        brake = (
            "\n\n## 追问刹车\n"
            "连续 2 轮没有新增事实。本轮禁止 ask_user，只能 propose_change 或 finish。"
        )

    draft_block = "（暂无）"
    if focus is not None:
        for block in ctx.draft_blocks:
            if block.experience_id == focus.experience_id:
                draft_block = _clip(block.markdown, 4000)
                break

    sections = [
        f"## 会话\n方向：{ctx.session.direction or '未填写'}\n"
        f"目标：{session_goal or (ctx.session.direction if ctx.session.direction_confirmed else '未确认')}\n"
        f"JD 关键词：{', '.join(ctx.session.jd_keywords) or '未填写'}\n"
        f"摘要：{_clip(ctx.session.summary, 300) or '（暂无）'}",
        f"## 经历清单\n{experience_block}",
    ]
    jd = [f'[{source.source_id}，版本 {source.source_revision}]\n{source.text}' for source in ctx.sources if source.kind == 'jd']
    if jd:
        sections.append('## 目标岗位描述（仅用于匹配要求，不能当作用户经历）\n' + _clip('\n\n'.join(jd), 6000))
    if focus is None:
        sections.append("## 当前焦点\n（未指定）")
    else:
        available = supported_experience_slots(
            focus, source_texts={s.source_id: s.text for s in ctx.sources},
            source_kinds={s.source_id: s.kind for s in ctx.sources})
        if focus.kind in EXPERIENCE_KINDS:
            sections.append('## 已有材料诊断\n'
                            + '原文或已核实个人引用中可辨认的维度：'
                            + '、'.join(slot for slot in ('responsibility', 'action', 'effect') if slot in available)
                            + '。只补影响改写的缺口；已有维度不重复采访。明确没有效果或拒答时按现有事实处理；记不清最多换角度追问一次。')
        if focus.kind not in EXPERIENCE_KINDS:
            sections.append('## 当前段处理规则（本节优先，不套用经历采访）\n' + RULES[focus.kind]
                            + '\n可以直接整理清楚的原文，不必先问职责、动作或效果。只修改当前目标块，其他个人材料仅作依据。')
        feedback = [r for r in ctx.revision_feedback if r.get('experience_id') == focus.experience_id
                    and r.get('block_revision') == focus.block_revision]
        if feedback:
            latest = feedback[-1]
            sections.append('## 当前段修改反馈（表达要求，不是新增事实）\n'
                            + '用户原话：' + _clip(latest['feedback'], 1000)
                            + '\n待调整的旧建议（不作为事实依据）：\n' + _clip(latest['after'], 4000)
                            + '\n请直接生成符合反馈的完整新版建议；before 仍取当前稿，采用前不写入正文。')
        sections.append(
            f"## 当前焦点\n{_focus_block(focus)}\n\n"
            f"## 可用来源（quote 必须逐字来自这里）\n{_sources_block(ctx, focus)}\n"
            '未完整展示的依据可按 source_id 调用 read_material，优先每次一个来源；'
            '按返回的 next_offset 继续补读，offset 按原文字符位置计，不按字节或token；'
            '来源版本变化时重新核对，不跨版本拼接；不能用事实记忆拼造引用。\n\n'
            f"## 已确认事实\n{_facts_block(focus)}\n\n"
            f"## 实际效果\n{_effect_block(focus)}\n\n"
            f"## 问题与缺口\n{_questions_block(focus)}\n\n"
            f"## 最近对话\n{_messages_block(ctx)}\n\n"
            f"## 当前稿\n{draft_block}{brake}"
        )
    return "\n\n".join(sections)
