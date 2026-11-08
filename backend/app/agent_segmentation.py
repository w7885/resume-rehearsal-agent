"""Rule-first resume segmentation; ask the model only for line numbers, never text."""
import re

from app.resume_structure import entry_dated, heading, split_resume

MAX_BOUNDARIES = 50
_BULLET = re.compile(r'^\s*[-+*－•▸●▪▶]')

_INSTRUCTION = (
    '只做简历分段，不改写任何文字。输入是带行号的简历文本，行号从 0 开始。'
    '找出每段经历/项目的起始行（例如以日期和项目名开头的标题行），'
    '返回 JSON：{"boundaries":[行号,...]}；不确定或没有可分的条目时返回空数组。'
    '行号必须来自输入；不要输出正文或解释。'
)


def _entry_candidate(line):
    return bool(line.strip()) and not _BULLET.match(line) and entry_dated(heading(line))


def suspicious(text):
    """同一规则分段里出现多个日期条目，说明规则边界大概率漏切。"""
    for part in split_resume(text):
        if sum(1 for line in part.splitlines() if _entry_candidate(line)) > 1:
            return True
    return False


def validate(text, values):
    lines = text.splitlines()
    result = set()
    for value in values if isinstance(values, list) else ():
        if type(value) is not int or not 0 < value < len(lines):
            continue
        if not lines[value].strip() or _BULLET.match(lines[value]):
            continue
        result.add(value)
        if len(result) >= MAX_BOUNDARIES:
            break
    return result or None


async def refine(runtime, text):
    """返回额外的分段行号；规则已够用或模型结果不可用时返回 None。"""
    if not text.strip() or not suspicious(text):
        return None
    lines = text.splitlines()
    numbered = '\n'.join(f'{index}: {line}' for index, line in enumerate(lines) if line.strip())
    try:
        data = await runtime._json(_INSTRUCTION, {'lines': numbered})
    except Exception:
        # 分段兜底不能阻断导入；失败就退回规则结果。
        return None
    return validate(text, (data or {}).get('boundaries'))
