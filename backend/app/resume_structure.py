"""Lossless resume boundaries. Never ask a model to regenerate imported text."""
import re
import unicodedata
from typing import Literal

from app.draft_actions import join_markdown
from app.repository import RepositoryError

SECTION = re.compile(r'^(?:教育(?:经历|背景)|项目(?:经历|经验)|个人项目|个人经历|工作(?:经历|经验)|实习(?:经历|经验)|校园经历|实践经历|技能(?:证书|清单)?|专业技能|个人技能|获奖(?:经历|情况)|荣誉奖项|自我评价|个人总结|自我优势|个人优势|核心优势|个人简介|自我介绍|求职意向|基本信息|个人信息|联系方式|Education|Projects?|Experience|Skills|Summary|Profile)\s*[:：]?$', re.I)
SectionKind = Literal['project', 'work', 'profile', 'education', 'skills', 'summary', 'awards']
SECTION_TYPES = {
    **{s: 'profile' for s in ('基本信息', '个人信息', '联系方式', '求职意向', 'profile')},
    **{s: 'education' for s in ('教育背景', '教育经历', 'education')},
    **{s: 'skills' for s in ('技能', '技能证书', '技能清单', '专业技能', '个人技能', 'skills')},
    **{s: 'summary' for s in ('自我评价', '个人总结', '自我优势', '个人优势', '核心优势', '个人简介', '自我介绍', 'summary')},
    **{s: 'awards' for s in ('获奖经历', '获奖情况', '荣誉奖项')},
}
PROJECT = re.compile(r'^(?:项目(?:经历|经验)?\s*[一二三四五六七八九十0-9]+|第[一二三四五六七八九十0-9]+个项目)(?:\s*[:：、.．｜|—-].*|\s+.*)?$')
DATE = re.compile(r'^\d{4}[.年/-]\d{1,2}.{0,20}[—–~～-].{0,20}[｜|]')
# Extracted PDFs often use whitespace instead of "｜": "2025.10—2025.12  项目名  角色".
ENTRY_DATE = re.compile(
    r'^\d{4}\s*[.年/-]\s*\d{1,2}\s*月?\s*(?:[—–~～-]|至)\s*'
    r'(?:\d{4}\s*[.年/-]\s*\d{1,2}\s*月?|\d{1,2}\s*月|至今|现在|今|present)',
    re.I,
)

# Section label -> interview kind. None means frozen: parsed, editable, never interviewed.
_SECTION_KIND = {
    '项目': 'project', '项目经历': 'project', '项目经验': 'project',
    'projects': 'project', 'project': 'project', '个人项目': 'project',
    '个人经历': 'project', '校园经历': 'project', '实践经历': 'project',
    '工作': 'work', '工作经历': 'work', '工作经验': 'work',
    '实习经历': 'work', '实习经验': 'work', 'experience': 'work',
    '教育经历': None, '教育背景': None, 'education': None,
    '技能': None, '技能证书': None, '技能清单': None, '专业技能': None,
    '个人技能': None, 'skills': None,
    '获奖': None, '获奖经历': None, '获奖情况': None, '荣誉奖项': None,
    '自我评价': None, '个人总结': None, 'summary': None,
    '求职意向': None, '个人信息': None, '联系方式': None,
    **{s: None for s in SECTION_TYPES},
}
_EXPERIENCE_PREFIX = re.compile(
    r'^(?:项目经历|项目经验|项目|个人项目|个人经历|校园经历|实践经历|'
    r'工作经历|工作经验|工作|实习经历|实习经验|Projects?|Experience)\s*[:：]',
    re.I,
)
_NAMED_ENTRY = re.compile(r'系统|平台|工具|助手|App|应用')
# Contact fields need a label separator: "姓名模糊查询" is project text, not basic info.
PROFILE_HINT = re.compile(r'基本信息|个人信息|(?:姓名|电话|邮箱|联系方式|求职意向)\s*[:：]|\b\d{11}\b|[\w.+-]+@[\w.-]+')
# PDF extracts often prefix headings with icon-font glyphs from the private use area.
_ICONS = re.compile(r'^[\s\uf000-\uf8ff\u2022\u00b7\u25aa\u25b8\u25cf\u25c6\u25c7\u25a0\u25a1\u25b6\u25ba\u2605\u2606]+|[\s\uf000-\uf8ff\u2022\u00b7\u25aa\u25b8\u25cf\u25c6\u25c7\u25a0\u25a1\u25b6\u25ba\u2605\u2606]+$')


def heading(line):
    label = re.sub(r'^\s{0,3}#{1,6}\s+', '', line.strip()).strip()
    if label.startswith('**') and label.endswith('**'):
        label = label[2:-2].strip()
    # NFKC folds Kangxi radicals (⼈⽬) and full-width punctuation back to normal text.
    return _ICONS.sub('', unicodedata.normalize('NFKC', label)).strip()


def section_kind(label):
    """Return (recognized, kind) for a section heading; kind may be None (frozen)."""
    text = re.sub(r'[:：]\s*$', '', heading(label)).strip()
    key = text.lower()
    inline_label = re.split(r'[:：]', text, maxsplit=1)[0].strip().lower()
    if inline_label in SECTION_TYPES:
        return True, None
    if key in _SECTION_KIND:
        return True, _SECTION_KIND[key]
    return False, None


def inline_kind(label):
    """A one-block section such as '项目经历：整理反馈' is itself an entry."""
    match = _EXPERIENCE_PREFIX.match(heading(label))
    if match is None:
        return None
    key = match.group(0).rstrip(' :：').lower()
    return _SECTION_KIND.get(key)


def block_kinds(markdowns):
    """Classify draft blocks in order: 'project'/'work' or None (frozen).

    When a resume carries no recognizable structure at all, every non-empty
    block stays an experience so pasted project text keeps working.
    """
    kinds, section, seen = [], None, False
    for markdown in markdowns:
        lines = [line for line in markdown.splitlines() if line.strip()]
        if not lines:
            kinds.append(None)
            continue
        first, label = lines[0], heading(lines[0])
        recognized, kind = section_kind(label)
        if recognized:
            seen = True
            # "frozen" keeps later blocks in 教育/技能 out of the interview order.
            section = kind if kind is not None else 'frozen'
            kinds.append(kind if kind is not None and len(lines) > 1 else None)
            continue
        prefix = inline_kind(label)
        if prefix is not None:
            seen = True
            section = prefix
            kinds.append(prefix)
            continue
        if PROJECT.fullmatch(label):
            seen = True
            section = 'project'
            kinds.append('project' if len(lines) > 1 else None)
            continue
        if section is not None:
            kinds.append(None if section == 'frozen' else section)
            continue
        if (ENTRY_DATE.match(label) or (
            re.match(r'^\s{0,3}#{1,6}\s+', first) and _NAMED_ENTRY.search(label)
        )) and len(lines) > 1:
            seen = True
            section = 'project'
            kinds.append('project')
            continue
        kinds.append(None)
    if not seen:
        return ['project' if markdown.strip() else None for markdown in markdowns]
    return kinds


def section_kinds(markdowns):
    """All supported resume sections, without changing the legacy project index."""
    legacy = block_kinds(markdowns)
    result, current = [], None
    for index, (markdown, experience_kind) in enumerate(zip(markdowns, legacy)):
        lines = [heading(line) for line in markdown.splitlines() if line.strip()]
        if not lines:
            result.append(None)
            continue
        if len(lines) == 1 and lines[0].endswith('简历'):
            result.append(None)
            continue
        label = re.split(r'[:：]', lines[0], maxsplit=1)[0].strip().lower()
        if label in SECTION_TYPES:
            current = SECTION_TYPES[label]
            result.append(current)
        elif section_kind(lines[0])[0] or PROJECT.fullmatch(lines[0]) or inline_kind(lines[0]):
            current = None
            result.append(experience_kind)
        elif current is None and index == 0 and not inline_kind(lines[0]) and (experience_kind is None or any(
                PROFILE_HINT.search(line) for line in lines)):
            result.append('profile')
        elif experience_kind in {'project', 'work'} and current is None:
            # An unstructured resume fragment keeps its original experience path.
            result.append(experience_kind)
        elif current is not None:
            result.append(current)
        elif any(PROFILE_HINT.search(line) for line in lines):
            result.append('profile')
        else:
            result.append(experience_kind)
    # Academic metadata is an information section even under an experience
    # heading. Preserve genuine activities/projects with personal actions.
    for index, (kind, markdown) in enumerate(zip(result, markdowns)):
        if (kind == 'project'
                and re.search(r'绩点|学业成绩|\bGPA\b|主修课程', markdown, re.I)
                and re.search(r'竞赛|等奖|获奖|CET[ -]?[46]|成绩|课程', markdown, re.I)
                and not re.search(r'负责|开发|搭建|设计|实现|组织|担任|编写|维护|调研|协作|部署', markdown)):
            result[index] = 'education'
    return result


def split_resume(text):
    """Keep each heading with its content; preserve every byte-equivalent character."""
    starts, offset, fence, in_experience = [0], 0, None, False
    for line in text.splitlines(keepends=True):
        token = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if token:
            mark = token[1]
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = None
        elif fence is None:
            label = heading(line)
            section = bool(SECTION.fullmatch(label)) or bool(inline_kind(label)) or bool(re.match(r'^([^:：]+)[:：].+', label) and re.split(r'[:：]', label, maxsplit=1)[0].strip().lower() in SECTION_TYPES)
            project = bool(PROJECT.fullmatch(label))
            dated_entry = in_experience and bool(ENTRY_DATE.match(label))
            named_entry = in_experience and bool(re.match(r'^\s{0,3}#{1,6}\s+', line)) and bool(_NAMED_ENTRY.search(label))
            if (section or project or dated_entry or named_entry) and offset and text[starts[-1]:offset].strip():
                starts.append(offset)
            if section:
                in_experience = section_kind(label)[1] is not None or inline_kind(label) is not None
        offset += len(line)
    starts.append(len(text))
    return [text[a:b] for a, b in zip(starts, starts[1:]) if text[a:b].strip()]


def structure_blocks(blocks):
    """Map split/grouped draft blocks back to exact per-source substring quotes."""
    whole = join_markdown(blocks)
    parts = split_resume(whole)
    if len(parts) <= 1:
        return blocks
    originals, offset = [], 0
    for block in blocks:
        start = whole.find(block['markdown'], offset)
        if start < 0:
            raise RepositoryError('invalid_input', '无法核对正文分段。')
        originals.append((start, start + len(block['markdown']), block))
        offset = start + len(block['markdown'])
    result, offset = [], 0
    for part in parts:
        start, end = offset, offset + len(part)
        refs = []
        for a, b, original in originals:
            lo, hi = max(start, a), min(end, b)
            if lo >= hi:
                continue
            quote = whole[lo:hi]
            if not quote.strip():
                continue
            for ref in original['sourceRefs']:
                if quote in ref['quote']:
                    same = next((r for r in refs if (r['sourceId'], r['sourceRevision']) == (ref['sourceId'], ref['sourceRevision'])), None)
                    if same is None:
                        refs.append({**ref, 'quote': quote})
                    elif quote not in same['quote']:
                        raise RepositoryError('invalid_input', '同一来源的分段依据不唯一。')
        if not refs:
            raise RepositoryError('invalid_input', '正文分段缺少可核对的原文依据。')
        result.append({'markdown': part, 'sourceRefs': refs})
        offset = end
    if join_markdown(result) != whole:
        raise RepositoryError('invalid_input', '分段未能完整保留原文。')
    return result


def body_span(content, start_line, end_line):
    """Anchors must be unique full nonblank lines; no model offsets or rewrites."""
    if not isinstance(start_line, str) or not isinstance(end_line, str) or not start_line.strip() or not end_line.strip():
        raise RepositoryError('invalid_input', '无法确认简历正文的起止行。')
    lines = content.splitlines(keepends=True)
    first = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == start_line]
    last = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == end_line]
    if len(first) != 1 or len(last) != 1 or first[0] > last[0]:
        raise RepositoryError('invalid_input', '简历起止行重复或不匹配，请将正文与指令分开发送。')
    a = sum(map(len, lines[:first[0]])); b = sum(map(len, lines[:last[0]+1]))
    # Keep a pure resume message byte-for-byte, including outer blank lines.
    if not content[:a].strip():
        a = 0
    if not content[b:].strip():
        b = len(content)
    body = content[a:b]
    if any(re.match(r'^\s*(?:操作指令|修改要求)\s*[:：]|^\s*(?:请|帮我|只|仅).{0,12}(?:优化|修改|改写|润色|精简).{0,12}(?:项目|简历|正文|基本信息|个人信息|自我评价|自我优势|个人优势|技能|教育|获奖|证书)', line) for line in body.splitlines()):
        raise RepositoryError('invalid_input', '正文范围内仍含修改指令，请将正文与指令分开发送。')
    return a, b
