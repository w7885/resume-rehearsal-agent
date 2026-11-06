"""Bounded ownership classification for active web intake asks."""
import re

from app import web_intake
from app.web_candidates import parse_choice

_URL_OR_GENERIC = re.compile(
    r'https?://|通用修改|按通用|^重试联网[。！!]*$|^重新搜索[。！!]*$')
_NUMERAL = r'[0-9一二三四五六七八九十]+'
_EXPERIENCE_EDIT = re.compile(
    r'(?:改|修改|调整|优化|重写|润色|处理|切换|切到|切回).{0,6}(?:第)?\s*' + _NUMERAL + r'\s*[段个]'
    r'|(?:第)?\s*' + _NUMERAL + r'\s*[段个].{0,6}(?:改|修改|调整|优化|重写|润色|处理)'
    r'|(?:第)?\s*' + _NUMERAL + r'\s*[段个](?:经历|简历)')


def _clean(text):
    return (text or '').strip().rstrip('。！!')


def _selection_or_edit(value, state=None):
    # 先认经历编辑动作：出现“改/切换”时，数字指向简历段落而非候选编号。
    if _EXPERIENCE_EDIT.search(value):
        match = re.search(r'([0-9]+)', value)
        return {'decision': 'edit_experience',
                'index': int(match[1]) if match else None}
    if state and state.get('phase') == 'awaiting_selection':
        if value in {'是', '是的', '就这个', '确认这个', '确认'}:
            return {'decision': 'select_candidate',
                    'index': 1 if len(state.get('candidates') or []) == 1 else None}
        for index, candidate in enumerate(state.get('candidates') or [], 1):
            if value == candidate.get('candidateId'):
                return {'decision': 'select_candidate', 'index': index}
        index = parse_choice(value)
        if index:
            return {'decision': 'select_candidate', 'index': index}
    return None


def classify_text(text, state=None):
    # “通用修改”同时出现在拒绝词表，先判联网请求，由它接管语义。
    value = _clean(text)
    if _URL_OR_GENERIC.search(value):
        return {'decision': 'deterministic_web'}
    if web_intake.is_web_decline(value):
        return {'decision': 'decline'}
    return _selection_or_edit(value, state)
