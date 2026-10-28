"""Trusted state transitions around semantic intent classification."""
import re

from app.agent_answer_sources import factual_answer


def factual_quote(message, quote):
    """Accept a complete original sentence span, never a model paraphrase.

    Commas are not boundaries: dropping '如果' or '不' before a comma can
    turn a hypothesis/denial into a false personal claim.
    """
    if not quote or not quote.strip() or not factual_answer(quote):
        return None
    for match in re.finditer(re.escape(quote), message):
        left, right = message[:match.start()].rstrip(' \t\r'), message[match.end():].lstrip(' \t\r')
        if re.search(r'如果|假如|假设|他说|她说|他人|示例|虚构', left):
            continue
        if left and left[-1] not in '。；;！!？?\n':
            continue
        if right and quote.rstrip()[-1] not in '。；;！!' and right[0] not in '。；;！!？?\n':
            continue
        return quote
    return None


def current_question(doc):
    return next((q for q in reversed(doc.questions) if q.active and q.outcome == 'asked'), None) if doc else None


def context_reply(doc, reply=None):
    if doc is None:
        return '请先明确要处理的简历段落；这条消息尚未作为经历回答。'
    lines = [line.strip() for line in doc.raw_text.splitlines() if line.strip()]
    # A mixed campus block may put the actual activity after grades/awards.
    # Do not hide it by showing only the first metadata lines.
    excerpt = '\n'.join(lines)[:600]
    text = f'当前正在处理第{doc.seq}段，原文片段是：\n{excerpt}\n'
    if reply:
        text += reply.strip() + '\n'
    question = current_question(doc)
    if question:
        text += f'当前待核对的问题是：{question.text}\n这次先回应你的疑问，原问题和追问次数保持。'
    return text.strip()
