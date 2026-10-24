"""Conservative, server-resolved project limits for chat suggestions and acceptance."""
import re

from app.repository import RepositoryError
from app.resume_structure import DATE, PROJECT, block_kinds, heading

NUMBERS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
ORDINAL = r'(?:第\s*([0-9一二两三四五六七八九十]+)\s*(?:个|项)?\s*项目|项目\s*([0-9一二两三四五六七八九十]+))'
REQUEST = re.compile(r'(?:(?:只|仅)(?:对|把)?(?:优化|修改|改写|调整|润色|精简|改)?\s*|(?:优化|修改|改写|调整|润色|精简|改)\s*)' + ORDINAL)


def project_blocks(repo, context):
    draft = repo.get_draft(context)
    texts = []
    with repo._operation(context) as (connection, _):
        for block in draft['blocks']:
            original = connection.execute('SELECT markdown FROM draft_blocks WHERE session_id=? AND block_id=? ORDER BY block_revision LIMIT 1', (context.session_id, block['blockId'])).fetchone()
            texts.append(original[0] if original else block['markdown'])
    return [block for block, kind in zip(draft['blocks'], block_kinds(texts)) if kind == 'project']


def resolve_scope(repo, context):
    """None means unrestricted; an empty set means the requested project is ambiguous."""
    with repo._operation(context) as (connection, session):
        rows = connection.execute("SELECT content FROM messages WHERE session_id=? AND role='user' AND use_status='active' AND resume_generation=? ORDER BY rowid DESC", (context.session_id, session['current_resume_generation'])).fetchall()
    projects = None
    for row in rows:
        # Only line-level requests count. A resume sentence mentioning a project is not authority.
        lines = []
        for line in row['content'].splitlines():
            if re.search(r'如果|假如|是否|例如|比如|他说|她说|[“”「」"?？]', line):
                continue
            lines.extend(re.split(r'[，,。；;！!]', line))
        for line in reversed(lines):
            text = line.strip()
            if re.match(r'^(?:现在|接下来|请|帮我|我想|可以|\s)*(?:改|修改|优化|调整|重写|改写)?(?:整份简历|全部项目|所有项目|全文)', text):
                return None
            if re.search(r'不要|别', text):
                continue
            match = REQUEST.search(text)
            if not re.match(r'^(?:请|帮我|我想|这次|本次|现在|接下来|只|仅|要求|修改要求|操作指令|优化|修改|改写|调整|润色|精简|改)', text):
                continue
            if match is None:
                named = re.search(r'(?:只|仅)(?:对|把)?(?:优化|修改|改写|调整|润色|精简|改)\s*(.+)', text)
                if named is None:
                    continue
                projects = projects if projects is not None else project_blocks(repo, context)
                hits = []
                with repo._operation(context) as (connection, _):
                    for block in projects:
                        original = connection.execute('SELECT markdown FROM draft_blocks WHERE session_id=? AND block_id=? ORDER BY block_revision LIMIT 1', (context.session_id, block['blockId'])).fetchone()[0]
                        lines = [heading(line) for line in original.splitlines() if line.strip()]
                        names = []
                        for line in lines[:2]:
                            if DATE.match(line):
                                names.extend(re.split(r'[｜|]', line)[1:2])
                            elif PROJECT.fullmatch(line):
                                names.extend(re.split(r'[:：]', line, maxsplit=1)[1:])
                        if any(len(name.strip()) >= 2 and name.strip() in named[1] for name in names):
                            hits.append(block['blockId'])
                if hits:
                    return {hits[0]} if len(hits) == 1 else set()
                if re.search(r'项目|系统|平台|应用|助手', named[1]):
                    return set()
                continue
            values = [value for value in match.groups() if value]
            raw = values[0]
            index = int(raw) if raw.isdigit() else NUMBERS.get(raw)
            projects = projects if projects is not None else project_blocks(repo, context)
            if index is None or index < 1 or index > len(projects):
                return set()
            # A legacy block containing two project headers must never masquerade as project two.
            for block in projects:
                if sum(bool(PROJECT.fullmatch(heading(line))) for line in block['markdown'].splitlines()) > 1:
                    return set()
            return {projects[index - 1]['blockId']}
    return None


def validate_project_refs(repo, context, block_id, refs):
    """A whole-file source ID does not authorize citing another project's slice."""
    with repo._operation(context) as (connection, _):
        permitted = connection.execute('SELECT source_id,source_revision,quote FROM draft_sources WHERE session_id=? AND block_id=?', (context.session_id, block_id)).fetchall()
        for ref in refs:
            sid = ref.source_id if hasattr(ref, 'source_id') else ref['sourceId']
            revision = ref.source_revision if hasattr(ref, 'source_revision') else ref['sourceRevision']
            quote = ref.quote if hasattr(ref, 'quote') else ref['quote']
            source = repo._find_source(connection, context.session_id, sid)
            if source['role'] == 'resume' and not any(row['source_id'] == sid and revision in (0, row['source_revision']) and quote and quote in row['quote'] for row in permitted):
                raise RepositoryError('scope_violation', '引用不属于指定项目的原文范围。', False)
