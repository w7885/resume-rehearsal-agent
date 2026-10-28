"""One focused question plus optional, non-presupposing answer guidance."""
import re

MAX_QUESTION_LENGTH = 120


def _anchor(doc):
    for line in doc.raw_text.splitlines():
        if re.search(r'负责|承担|开发|搭建|实现|组织|担任|编写', line) and re.search(r'[:：]', line):
            label = re.split(r'[:：]', line, maxsplit=1)[0].strip(' ▸▹►•●▪◆◇#*')
            if label and len(label) <= 28 and label not in {'项目经历', '工作经历', '实习经历', '校园经历'}:
                return f'「{label}」'
    text = doc.raw_text + '\n' + '\n'.join(f.statement for f in doc.facts if f.status == 'active')
    for word in ('姓名模糊查询', '模糊查询', '接口测试', '文章发布', '学生信息', '个人博客', '反馈整理'):
        if word in text:
            return word
    if doc.title.strip() and doc.title.strip() in doc.raw_text and re.search(r'项目|系统|平台|网站|活动|建模', doc.title):
        return f'「{doc.title.strip()[:28]}」'
    return '这段经历'


def guided_question(doc, slot, *, effect_followup=False):
    anchor = _anchor(doc)
    if effect_followup:
        return '除了使用情况，实际带来了什么变化？可说少做的一步或一条实际反馈；没有就说没有。'
    return {
        'responsibility': f'关于{anchor}，你本人负责哪部分？可从设计、实现或测试中选一项说起；没参与的不用补充。',
        'action': f'在{anchor}中，你亲手处理了哪个小任务？可按“遇到的情况→采取的做法”讲，先说最熟悉的一件。',
        'context': f'做{anchor}之前，最想解决哪个具体问题？可说原来的做法或最麻烦的一步，选一个就好。',
        'effect': '这段经历上线或交付后，带来了哪些实际变化？可从操作步骤、查找方式或实际反馈中选一项；没有观察到也可以说没有。',
        'evidence': f'关于{anchor}，你保留了什么可供核对的材料？可说代码、文档或测试记录中的一项；没有也可以。',
        'tech': f'完成{anchor}时，你实际用到了什么工具或方法？先说最关键的一种，没用过的不用补充。',
    }[slot]


def with_answer_guidance(text, doc, slot):
    if doc.kind not in {'project', 'work'}:
        from app.agent.section_policy import question
        if slot not in {'details', 'strength', 'preference', 'context', 'evidence'}:
            return text
        if any(word in text for word in ('可从', '可以从', '可按', '例如', '比如', '先说', '可选', '可先', '选一项', '没有也可以')):
            return text
        guide = question(doc)[0].split('？', 1)[1]
        return text + guide if len(text + guide) <= MAX_QUESTION_LENGTH else text
    if slot not in {'responsibility', 'action', 'context', 'effect', 'tech', 'evidence'}:
        return text
    if any(word in text for word in ('可从', '可以从', '可按', '例如', '比如', '先说', '选一项', '没有也可以')):
        return text
    guide = guided_question(doc, slot).split('？', 1)[1]
    expanded = text + guide
    return expanded if len(expanded) <= MAX_QUESTION_LENGTH else text
