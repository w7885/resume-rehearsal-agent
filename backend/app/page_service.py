"""Session-bound local page cache. Images never count as recognized text."""
import json
from app.page_renderer import MAX_PAGES, RENDER_VERSION, prepare_page
from app.repository import RepositoryError, RequestContext, _integer, _text

SUPPORTED = {'application/pdf', 'image/png', 'image/jpeg'}


def material_row(connection, context, material_id):
    _text(material_id, '材料编号')
    row = connection.execute('SELECT attachment_id,session_id,media_type,content_hash,use_status,resume_generation,page_report_json FROM attachments WHERE session_id = ? AND attachment_id = ?', (context.session_id, material_id)).fetchone()
    if row is None:
        raise RepositoryError('source_not_found', '附件不存在于当前会话。')
    if row['media_type'] not in SUPPORTED:
        raise RepositoryError('unsupported_format', '原图／图页预览仅支持 PDF、PNG 和 JPEG。')
    return row


def page_view(row, number):
    return {'page': number, 'status': row['status'] if row else 'pending',
            'width': row['width'] if row else None, 'height': row['height'] if row else None,
            'errorCode': row['error_code'] if row else None, 'errorMessage': row['error_message'] if row else None}


def list_pages(repo, context, material_id):
    repo._user_path(context)
    with repo._operation(context) as (connection, session):
        material = material_row(connection, context, material_id)
        report = json.loads(material['page_report_json']) if material['page_report_json'] else None
        if report and (report.get('renderVersion') != RENDER_VERSION or report.get('originalHash') != material['content_hash']):
            report = None
        count = report.get('pageCount') if report else None
        rows = {row['page_number']: row for row in connection.execute(
            'SELECT page_number,status,width,height,error_code,error_message FROM material_pages WHERE session_id = ? AND attachment_id = ? AND render_version = ? AND original_hash = ?',
            (context.session_id, material_id, RENDER_VERSION, material['content_hash']))}
        return {'materialId': material_id, 'renderVersion': RENDER_VERSION, 'pageCount': count,
                'limit': MAX_PAGES, 'historical': material['use_status'] == 'history' or material['resume_generation'] != session['current_resume_generation'],
                'pages': [page_view(rows.get(number), number) for number in range(1, min(count or 1, MAX_PAGES) + 1)],
                'recognitionAvailable': False}


def prepare_material_page(service, session_id, material_id, payload):
    repo = service.repository
    context = RequestContext(session_id)
    number = _integer(payload['pageNumber'], '原页码')
    if number > MAX_PAGES:
        raise RepositoryError('invalid_input', '本次最多准备前 40 页，请拆分文件。')
    # Rendering is outside the database transaction. Cache hits skip native work.
    with repo._operation(context) as (connection, _):
        material = dict(material_row(connection, context, material_id))
        cached = connection.execute('SELECT status FROM material_pages WHERE session_id = ? AND attachment_id = ? AND page_number = ? AND render_version = ? AND original_hash = ?',
            (session_id, material_id, number, RENDER_VERSION, material['content_hash'])).fetchone()
        if cached is None or cached['status'] != 'ready':
            material['original_data'] = connection.execute('SELECT original_data FROM attachments WHERE session_id = ? AND attachment_id = ?', (session_id, material_id)).fetchone()['original_data']
    prepared = None if cached and cached['status'] == 'ready' else prepare_page(material['media_type'], material['original_data'], number)
    if prepared and prepared.get('errorCode') == 'invalid_page':
        raise RepositoryError('invalid_input', prepared['errorMessage'])

    def apply(bound):
        checked = RequestContext(session_id, payload['expectedStateRevision'])
        bound._user_path(checked)
        with bound._operation(checked, write=True) as (connection, _):
            current = material_row(connection, checked, material_id)
            if current['content_hash'] != material['content_hash']:
                raise RepositoryError('stale_state', '附件内容已变化，请重新读取。')
            row = connection.execute('SELECT status,original_hash FROM material_pages WHERE session_id = ? AND attachment_id = ? AND page_number = ? AND render_version = ?',
                (session_id, material_id, number, RENDER_VERSION)).fetchone()
            if prepared and (row is None or row['status'] != 'ready' or row['original_hash'] != current['content_hash']):
                report = {'renderVersion': RENDER_VERSION, 'originalHash': current['content_hash'], 'pageCount': prepared.get('pageCount')}
                old = json.loads(current['page_report_json']) if current['page_report_json'] else {}
                if report['pageCount'] is None and old.get('renderVersion') == RENDER_VERSION and old.get('originalHash') == current['content_hash']:
                    report['pageCount'] = old.get('pageCount')
                connection.execute('UPDATE attachments SET page_report_json = ? WHERE session_id = ? AND attachment_id = ?', (json.dumps(report), session_id, material_id))
                connection.execute('INSERT OR REPLACE INTO material_pages(session_id,attachment_id,page_number,render_version,original_hash,status,image_data,width,height,error_code,error_message) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (session_id, material_id, number, RENDER_VERSION, current['content_hash'], prepared['status'], prepared.get('imageData'), prepared.get('width'), prepared.get('height'), prepared.get('errorCode'), prepared.get('errorMessage')))
        # Derived view cache does not change the session, source or draft version.
        return list_pages(bound, RequestContext(session_id), material_id)
    return service._mutate(f'session:{session_id}', 'prepare_material_page', {**payload, 'materialId': material_id}, payload['requestId'], apply, session_id)


def read_page_image(repo, context, material_id, number):
    """User preview or trusted Host read; never a model-selectable file tool.

    Host supplies a persisted run context. Unknown current material is readable
    for classification, but old generations and cancelled runs are not.
    """
    _integer(number, '页码')
    with repo._operation(context) as (connection, session):
        material = material_row(connection, context, material_id)
        if context.run_id is not None and (session['mode'] != 'local' or material['use_status'] == 'history' or material['resume_generation'] != session['current_resume_generation']):
            raise RepositoryError('stale_state', '历史或模拟材料不能进入当前图像识别。')
        row = connection.execute('SELECT * FROM material_pages WHERE session_id = ? AND attachment_id = ? AND page_number = ? AND render_version = ? AND original_hash = ?',
            (context.session_id, material_id, number, RENDER_VERSION, material['content_hash'])).fetchone()
        if row is None or row['status'] != 'ready':
            raise RepositoryError('source_not_found', '该页图像尚未准备成功，请先准备或重试。')
        return {'imageData': row['image_data'], 'mediaType': 'image/png', 'page': number,
                'materialId': material_id, 'sessionId': context.session_id, 'contentHash': material['content_hash'],
                'width': row['width'], 'height': row['height'], 'renderVersion': RENDER_VERSION}
