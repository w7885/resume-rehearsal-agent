"""Budgeted recognition of saved current images; no uploads or arbitrary paths."""
import json

from app import vision
from app.repository import Repository, RepositoryError, RequestContext


async def recognize(runtime, user_content):
    if not vision.configured():
        return
    retry = any(word in user_content for word in ('识别', '重试'))
    with runtime.repo._operation(runtime.context) as (connection, session):
        images = [dict(row) for row in connection.execute("SELECT * FROM attachments a WHERE session_id=? AND resume_generation=? AND use_status='pending' AND media_type IN ('image/png','image/jpeg') AND (parse_status='pending' OR (? AND parse_status='failed')) AND NOT EXISTS (SELECT 1 FROM source_blocks b WHERE b.session_id=a.session_id AND b.attachment_id=a.attachment_id) ORDER BY original_order LIMIT 2", (runtime.sid, session['current_resume_generation'], retry))]
    for image in images:
        with runtime.repo._operation(runtime.context):
            pass
        runtime._emit('progress', {'message': '正在识别已保存图片中的文字。'})
        text = await vision.transcribe_image(image['media_type'], image['original_data'], budget=runtime.budget, run_id=runtime.context.run_id)
        with runtime.repo._operation(runtime.context, write=True) as (connection, session):
            current_image = connection.execute('SELECT * FROM attachments WHERE session_id=? AND attachment_id=?', (runtime.sid, image['attachment_id'])).fetchone()
            if current_image is None or current_image['content_hash'] != image['content_hash'] or current_image['use_status'] != 'pending':
                raise RepositoryError('stale_state', '图片已变化，本次识别结果不写入。')
            bound = Repository(runtime.repo.path, _connection=connection)
            status = 'partial' if text and '[无法识别]' in text else 'parsed' if text else 'failed'
            report = {'parser': 'deepseek-flash-vision', 'status': status, 'pageCount': 1,
                      'warnings': [{'code': 'unreadable_text', 'message': '部分文字无法识别，请核对原件。'}] if status == 'partial' else [],
                      'message': '图片转录文字已保存，请核对原件。' if text else '图片识别未完成；可发送“重试识别图片”再次处理，无需重新上传。'}
            connection.execute('UPDATE attachments SET parse_status=?,parse_report_json=? WHERE session_id=? AND attachment_id=?', (status, json.dumps(report, ensure_ascii=False), runtime.sid, image['attachment_id']))
            if text:
                bound.save_source(RequestContext(runtime.sid, session['state_revision']), text=text, role='unknown', use_status='pending', origin={'kind': 'material', 'materialId': image['attachment_id'], 'page': 1, 'lineStart': 1, 'lineEnd': len(text.splitlines())})
            bound._touch(connection, runtime.sid)
            current = bound.get_session(RequestContext(runtime.sid))
            connection.execute('INSERT OR REPLACE INTO agent_turn_effects VALUES (?,?,?,?,?)', (runtime.sid, runtime.user_id, current['stateRevision'], current['draftRevision'], current['currentResumeGeneration']))
        runtime._continue()
