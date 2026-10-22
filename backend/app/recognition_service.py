"""Trusted Host result adapter and user intents. Never calls a model."""
import json
from app.repository import RepositoryError, RequestContext, _choice, _id, _integer, _keys, _text
from app.page_renderer import MAX_PAGES, RENDER_VERSION


def current_material(connection, context, material_id, session):
    row = connection.execute('SELECT attachment_id,media_type,content_hash,role,use_status,resume_generation,page_report_json FROM attachments WHERE session_id = ? AND attachment_id = ?', (context.session_id, material_id)).fetchone()
    if row is None:
        raise RepositoryError('source_not_found', '附件不属于当前会话。')
    if session['mode'] != 'local' or row['use_status'] == 'history' or row['resume_generation'] != session['current_resume_generation']:
        raise RepositoryError('stale_state', '历史或模拟材料不能写入当前识别结果。')
    if row['media_type'] not in {'application/pdf','image/png','image/jpeg'}:
        raise RepositoryError('unsupported_format', '该材料不是图像或 PDF。')
    return row


def host_check(repo, context):
    if not isinstance(context, RequestContext) or context.run_id is None:
        raise RepositoryError('scope_violation', '识别回执只允许绑定真实运行的受信 Host 保存。', False)


def latest_source(connection, sid, mid, page):
    return connection.execute("SELECT * FROM source_blocks WHERE session_id = ? AND attachment_id = ? AND page_number = ? AND json_extract(origin_json, '$.recognitionVersion') IS NOT NULL ORDER BY rowid DESC LIMIT 1", (sid,mid,page)).fetchone()


def result_view(repo, connection, row):
    source = latest_source(connection,row['session_id'],row['attachment_id'],row['page_number'])
    return {'resultId':row['result_id'],'materialId':row['attachment_id'],'pageNumber':row['page_number'],
            'status':row['status'],'text':row['text'],'proposedRole':row['proposed_role'],'errorMessage':row['error_message'],
            'recognitionVersion':row['recognition_version'],'renderVersion':row['render_version'],'mode':row['mode'],
            'currentSource':repo._source(source) if source else None,
            'expectedSourceRevision':source['source_revision'] if source else 0,
            'preservedCorrection':bool(row['preserved_correction']),
            'userCorrected':bool(row['preserved_correction'] or source and row['source_id'] == source['source_id'] and row['source_revision'] != source['source_revision'])}


def cached_result(repo, context, mid, page, recognition_version):
    host_check(repo,context); _integer(page,'页码'); _text(recognition_version,'识别版本')
    with repo._operation(context) as (connection,session):
        material=current_material(connection,context,mid,session)
        row=connection.execute('SELECT * FROM recognition_results WHERE session_id = ? AND attachment_id = ? AND page_number = ? AND original_hash = ? AND render_version = ? AND recognition_version = ? AND resume_generation = ? ORDER BY rowid DESC LIMIT 1',
            (context.session_id,mid,page,material['content_hash'],RENDER_VERSION,recognition_version,session['current_resume_generation'])).fetchone()
        source=latest_source(connection,context.session_id,mid,page)
        retries=[{'retryId':r['retry_id'],'pageNumbers':json.loads(r['pages_json']),'completed':json.loads(r['completed_json'])} for r in connection.execute("SELECT * FROM recognition_retry_requests WHERE session_id = ? AND attachment_id = ? AND original_hash = ? AND resume_generation = ? AND status = 'pending'", (context.session_id,mid,material['content_hash'],session['current_resume_generation']))]
        return {'cacheHit':bool(row and row['status'] in {'success','partial'}),'result':result_view(repo,connection,row) if row else None,
                'expectedSourceRevision':source['source_revision'] if source else 0,'currentSource':repo._source(source) if source else None,'retryRequests':retries}


def save_result(service, context, payload, synthetic=False):
    host_check(service.repository,context)
    required={'requestId','materialId','pageNumber','contentHash','renderVersion','recognitionVersion','expectedSourceRevision','status','text','role','errorMessage'}
    _keys(payload,required,{'retryId'})
    for key in ('requestId','materialId','contentHash','renderVersion','recognitionVersion'):
        if len(_text(payload[key],key)) > 160:
            raise RepositoryError('invalid_input','识别回执标识过长。')
    page=_integer(payload['pageNumber'],'页码'); _integer(payload['expectedSourceRevision'],'预期来源版本',minimum=0)
    if page > MAX_PAGES:
        raise RepositoryError('invalid_input','页码超过本次支持范围。')
    status=_choice(payload['status'],{'success','partial','failed'},'识别状态')
    _choice(payload['role'],{'resume','jd','supplement','unknown'},'建议材料用途')
    text=_text(payload['text'],'识别文字',empty=status=='failed')
    error=_text(payload['errorMessage'],'识别说明',empty=status=='success')
    if len(text)>50000 or len(error)>1000 or status=='failed' and text != '':
        raise RepositoryError('invalid_input','识别文字或失败说明不符合限制。')
    # Replays may return the previous result only while the trusted run remains
    # current; new writes repeat the same checks inside the transaction.
    with service.repository._operation(context):
        pass

    def apply(repo):
        with repo._operation(context,write=True) as (connection,session):
            material=current_material(connection,context,payload['materialId'],session)
            if material['content_hash'] != payload['contentHash'] or payload['renderVersion'] != RENDER_VERSION:
                raise RepositoryError('stale_state','识别依据的原件或图页版本已变化。')
            prepared=connection.execute('SELECT status FROM material_pages WHERE session_id = ? AND attachment_id = ? AND page_number = ? AND render_version = ? AND original_hash = ?',
                (context.session_id,material['attachment_id'],page,RENDER_VERSION,material['content_hash'])).fetchone()
            if prepared is None or status!='failed' and prepared['status']!='ready':
                raise RepositoryError('stale_state','回执缺少本次对应的可用图页。')
            old=latest_source(connection,context.session_id,material['attachment_id'],page)
            if payload['expectedSourceRevision'] != (old['source_revision'] if old else 0):
                raise RepositoryError('stale_state','来源已修正或已有较新回执，旧结果不能覆盖。')
            retry=None
            if payload.get('retryId'):
                retry=connection.execute("SELECT * FROM recognition_retry_requests WHERE retry_id = ? AND session_id = ? AND attachment_id = ? AND original_hash = ? AND resume_generation = ? AND status = 'pending'",
                    (payload['retryId'],context.session_id,material['attachment_id'],material['content_hash'],session['current_resume_generation'])).fetchone()
                if retry is None or page not in json.loads(retry['pages_json']) or str(page) in json.loads(retry['completed_json']):
                    raise RepositoryError('stale_state','重识别请求已处理或范围不匹配。')
            source_id=source_revision=None
            preserved=False
            if status!='failed':
                previous=connection.execute('SELECT max(source_revision) FROM recognition_results WHERE session_id = ? AND source_id = ? AND preserved_correction = 0',(context.session_id,old['source_id'] if old else None)).fetchone()[0]
                preserved=bool(old and previous is not None and old['source_revision']>previous)
                source_id=old['source_id'] if old else _id('source')
                source_revision=old['source_revision'] if preserved else (old['source_revision'] if old else 0)+1
                if old and not preserved:
                    connection.execute("UPDATE source_blocks SET use_status = 'history' WHERE session_id = ? AND source_id = ?",(context.session_id,source_id))
                    connection.execute("UPDATE suggestions SET status = 'invalidated' WHERE session_id = ? AND status = 'pending' AND suggestion_id IN (SELECT suggestion_id FROM suggestion_sources WHERE session_id = ? AND source_id = ?)",(context.session_id,context.session_id,source_id))
                origin={'kind':'material','materialId':material['attachment_id'],'page':page,'lineStart':1,'lineEnd':max(1,len(text.splitlines())),'recognitionVersion':payload['recognitionVersion'],'renderVersion':RENDER_VERSION}
                if not preserved:
                    connection.execute("INSERT INTO source_blocks(source_id,source_revision,session_id,attachment_id,role,text,origin_json,page_number,line_start,line_end,use_status,resume_generation) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
                        (source_id,source_revision,context.session_id,material['attachment_id'],material['role'],text,json.dumps(origin,ensure_ascii=False),page,1,origin['lineEnd'],session['current_resume_generation']))
            rid=_id('recognition')
            connection.execute('INSERT INTO recognition_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (rid,context.session_id,material['attachment_id'],page,material['content_hash'],RENDER_VERSION,payload['recognitionVersion'],context.run_id,session['current_resume_generation'],status,text,payload['role'],error,source_id,source_revision,'synthetic' if synthetic else 'host',int(preserved)))
            if retry:
                completed=json.loads(retry['completed_json']); completed[str(page)]=status
                done=all(str(p) in completed for p in json.loads(retry['pages_json']))
                outcome='failed' if done and 'failed' in completed.values() else 'completed' if done else 'pending'
                connection.execute('UPDATE recognition_retry_requests SET completed_json = ?,status = ? WHERE retry_id = ?', (json.dumps(completed),outcome,retry['retry_id']))
            rows=connection.execute('SELECT page_number,status FROM recognition_results WHERE session_id = ? AND attachment_id = ? AND original_hash = ? AND render_version = ? AND resume_generation = ? ORDER BY rowid',
                (context.session_id,material['attachment_id'],material['content_hash'],RENDER_VERSION,session['current_resume_generation'])).fetchall()
            states={r['page_number']:r['status'] for r in rows}
            page_report=json.loads(material['page_report_json']) if material['page_report_json'] else {}
            count=page_report.get('pageCount')
            full=bool(count and count<=MAX_PAGES and len(states)==count and all(s=='success' for s in states.values()))
            usable=connection.execute("SELECT 1 FROM source_blocks WHERE session_id = ? AND attachment_id = ? AND use_status != 'history' LIMIT 1",(context.session_id,material['attachment_id'])).fetchone()
            aggregate='parsed' if full else 'partial' if usable else 'failed'
            report={'status':aggregate,'mode':'synthetic' if synthetic else 'host','message':'构造识别回执，仅用于验证保存；不是真实模型结果。' if synthetic else {'parsed':'识别文字已保存，请对照原件核对。','partial':'部分文字已保存，其他内容仍需核对。','failed':'本次识别未完成，原件和已有内容保留。'}[aggregate],'pageCount':count,'completed':len(states),'failed':sum(s=='failed' for s in states.values())}
            connection.execute('UPDATE attachments SET parse_status = ?,recognition_report_json = ? WHERE session_id = ? AND attachment_id = ?', (aggregate,json.dumps(report,ensure_ascii=False),context.session_id,material['attachment_id']))
            return result_view(repo,connection,connection.execute('SELECT * FROM recognition_results WHERE result_id = ?', (rid,)).fetchone())
    fingerprint={**payload,'runId':context.run_id,'generation':context.generation,'stateRevision':context.expected_state_revision,'synthetic':synthetic}
    return service._mutate(f'session:{context.session_id}','recognition_result',fingerprint,payload['requestId'],apply,context.session_id)


def request_retry(service,sid,mid,payload):
    def apply(repo):
        context=RequestContext(sid,payload['expectedStateRevision']); repo._user_path(context)
        with repo._operation(context,write=True) as (connection,session):
            material=current_material(connection,context,mid,session)
            old=connection.execute("SELECT * FROM recognition_retry_requests WHERE session_id = ? AND attachment_id = ? AND original_hash = ? AND resume_generation = ? AND status = 'pending'",(sid,mid,material['content_hash'],session['current_resume_generation'])).fetchone()
            if old:
                return {'retryId':old['retry_id'],'status':'pending','stateRevision':session['state_revision']}
            report=json.loads(material['page_report_json']) if material['page_report_json'] else {}
            count=report.get('pageCount')
            if not count or count>MAX_PAGES:
                raise RepositoryError('invalid_input','请先打开附件；文件过大或加密时请转换后重传。')
            rid=_id('retry')
            connection.execute('INSERT INTO recognition_retry_requests(retry_id,session_id,attachment_id,original_hash,resume_generation,pages_json) VALUES (?,?,?,?,?,?)',(rid,sid,mid,material['content_hash'],session['current_resume_generation'],json.dumps(list(range(1,count+1)))))
            repo._touch(connection,sid)
            return {'retryId':rid,'status':'pending','stateRevision':repo.get_session(RequestContext(sid))['stateRevision']}
    return service._mutate(f'session:{sid}','request_recognition_retry',{**payload,'materialId':mid},payload['requestId'],apply,sid)


def reorder_images(service,sid,payload):
    ids=payload['materialIds']
    if not isinstance(ids,list) or any(not isinstance(mid,str) or not mid for mid in ids) or len(set(ids))!=len(ids) or len(ids)>200:
        raise RepositoryError('invalid_input','请提交完整且无重复的图片顺序。')
    def apply(repo):
        context=RequestContext(sid,payload['expectedStateRevision']); repo._user_path(context)
        with repo._operation(context,write=True) as (connection,session):
            if session['mode']!='local':
                raise RepositoryError('scope_violation','模拟历史不能修改为个人材料顺序。')
            rows=connection.execute("SELECT attachment_id,coalesce(reading_order,original_order) AS position FROM attachments WHERE session_id = ? AND resume_generation = ? AND use_status != 'history' AND media_type IN ('image/png','image/jpeg') ORDER BY coalesce(reading_order,original_order),original_order,attachment_id",(sid,session['current_resume_generation'])).fetchall()
            if set(ids)!={r['attachment_id'] for r in rows}:
                raise RepositoryError('stale_state','图片范围已变化，请重新读取附件。')
            if ids!=[r['attachment_id'] for r in rows]:
                for mid,row in zip(ids,rows):
                    connection.execute('UPDATE attachments SET reading_order = ? WHERE session_id = ? AND attachment_id = ?', (row['position'],sid,mid))
                repo._touch(connection,sid)
            return repo.get_conversation(RequestContext(sid))
    return service._mutate(f'session:{sid}','reorder_images',payload,payload['requestId'],apply,sid)
