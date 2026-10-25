"""Durable review progress, separate from revision-bound Agent factual memory."""
import hashlib
import json
import re

from app.agent_edit_scope import resolve_scope
from app.repository import RepositoryError
from app.resume_structure import block_kinds, heading, section_kind

CONTINUE = {'继续', '继续修改', '下一步', '下一段', '接着改', '继续优化', '继续改写'}
STOP = {'停止', '暂停', '先暂停', '先停一下', '先到这里', '暂时不改了', '不继续了'}
KEEP_NEXT = {'保留原文，继续下一段', '保留原文继续下一段', '保留原文，继续', '跳过当前段', '跳过这段'}


def plain(text):
    return text.strip().rstrip('。！!').strip()


def label(block):
    lines = [heading(line) for line in block['markdown'].splitlines() if line.strip()]
    if re.search(r'姓名|求职意向|电话|邮箱', '\n'.join(lines[:2])):
        return '基本信息与求职意向'
    content = [line for line in lines if not section_kind(line)[0]]
    first = (content or lines or ['当前段落'])[0]
    return first[:32]


def _key(session):
    return f"host-progress:g{session['current_resume_generation']}"


def _load(connection, session):
    row = connection.execute("SELECT doc_json FROM agent_state WHERE session_id=? AND scope='session' AND key=?", (session['session_id'], _key(session))).fetchone()
    return json.loads(row[0]) if row else {'reviewed': {}, 'waiting': None}


def _signature(block, sources):
    value = {'block': block, 'sources': [(s['sourceId'], s['sourceRevision']) for s in sources]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plan(runtime):
    context = runtime.repo.get_session_context(runtime.context)
    allowed = resolve_scope(runtime.repo, runtime.context)
    if allowed == set():
        raise RepositoryError('edit_scope_ambiguous', '无法唯一定位指定项目，请明确要修改的项目。')
    blocks = [b for b in context['draft']['blocks'] if allowed is None or b['blockId'] in allowed]
    kinds = block_kinds([b['markdown'] for b in blocks])
    blocks = [b for b, kind in zip(blocks, kinds) if kind is not None]
    with runtime.repo._operation(runtime.context) as (connection, session):
        progress = _load(connection, session)
        done, remaining, review = [], [], []
        for block in blocks:
            row = connection.execute('SELECT write_kind FROM draft_blocks WHERE session_id=? AND block_id=? AND block_revision=?',
                                     (runtime.sid, block['blockId'], block['blockRevision'])).fetchone()
            valid = True
            try:
                runtime.repo._refs(connection, runtime.sid, block['sourceRefs'])
            except RepositoryError:
                valid = False
            recorded = progress['reviewed'].get(block['blockId'], {})
            accepted = row and row['write_kind'] == 'accepted_suggestion'
            reviewed = recorded.get('signature') == _signature(block, context['sources'])
            if valid and (accepted or reviewed):
                done.append(block)
            elif not valid:
                review.append(block)
            else:
                remaining.append(block)
        pending = [p for p in context['suggestions'] if any(b['blockId'] == p['target']['blockId'] for b in blocks)]
        waiting = progress.get('waiting')
        if waiting:
            remaining.sort(key=lambda b: b['blockId'] != waiting)
        return {'blocks': blocks, 'done': done, 'remaining': remaining, 'review': review,
                'waiting': waiting, 'blocked': progress.get('blocked'),
                'pending': pending, 'limitedScope': allowed is not None, 'sources': context['sources']}


def record(runtime, block_id, *, completed=False, blocked=False):
    context = runtime.repo.get_session_context(runtime.context)
    block = next((b for b in context['draft']['blocks'] if b['blockId'] == block_id), None)
    if block is None:
        raise RepositoryError('stale_state', '当前段落已改变，接续进度未写入。')
    with runtime.repo._operation(runtime.context, write=True) as (connection, session):
        progress = _load(connection, session)
        progress['waiting'] = None if completed else block_id
        progress['blocked'] = block_id if blocked else None
        if completed:
            progress['reviewed'][block_id] = {'signature': _signature(block, context['sources'])}
        connection.execute("INSERT INTO agent_state(session_id,scope,key,doc_json,revision) VALUES (?,'session',?,?,1) ON CONFLICT(session_id,scope,key) DO UPDATE SET doc_json=excluded.doc_json,revision=agent_state.revision+1",
                           (runtime.sid, _key(session), json.dumps(progress, ensure_ascii=False)))


def completion(plan):
    prefix = '本次指定范围已处理完。' if plan['limitedScope'] else '本轮简历检查已完成。'
    return prefix + f"已处理 {len(plan['done'])} 段；当前稿已保存，可以预览或下载。交付前仍请核对经历、数字和资质。"
