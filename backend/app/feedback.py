"""Authenticated feedback and administrator handling."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import Field, field_validator
from starlette.responses import JSONResponse

from app.api import InputModel, error_response
from app.auth import current_identity
from app.db import database, transaction
from app.postgres import is_postgres


router = APIRouter(prefix='/api')
Category = Literal['bug', 'suggestion', 'other']
Status = Literal['pending', 'in_progress', 'resolved']
PAGE_SIZE = 20


def clean_text(value: str) -> str:
    if '\x00' in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError('Invalid feedback text')
    return value.strip()


class FeedbackInput(InputModel):
    submissionId: str = Field(min_length=36, max_length=36)
    category: Category
    description: str = Field(max_length=2000)

    @field_validator('submissionId')
    @classmethod
    def clean_submission_id(cls, value):
        return str(UUID(value))

    @field_validator('description')
    @classmethod
    def clean_description(cls, value):
        value = clean_text(value)
        if not value:
            raise ValueError('Feedback description is required')
        return value


class FeedbackUpdate(InputModel):
    status: Status
    note: str = Field(max_length=2000)
    revision: int = Field(ge=0, lt=2147483647)

    @field_validator('note')
    @classmethod
    def clean_note(cls, value):
        return clean_text(value)


def unavailable():
    return error_response('feedback_unavailable', '此环境暂不支持反馈。', 503)


def result(data, status=200):
    return JSONResponse(data, status_code=status, headers={'Cache-Control': 'no-store'})


def detail(connection, feedback_id):
    row = connection.execute(
        '''SELECT id, category, description, status, note, revision, created_at, submitter_email
           FROM user_feedback WHERE id=?''', (feedback_id,),
    ).fetchone()
    if row is None:
        return None
    history = connection.execute(
        '''SELECT actor_id, old_status, new_status, note, changed_at
           FROM feedback_history WHERE feedback_id=? AND revision<=? ORDER BY revision DESC''',
        (feedback_id, row['revision']),
    ).fetchall()
    return {
        'id': str(row['id']), 'category': row['category'], 'description': row['description'],
        'status': row['status'], 'note': row['note'], 'revision': row['revision'],
        'createdAt': row['created_at'].isoformat(), 'submitterEmail': row['submitter_email'],
        'history': [
            {'actorId': str(entry[0]), 'oldStatus': entry[1], 'newStatus': entry[2],
             'note': entry[3], 'changedAt': entry[4].isoformat()} for entry in history
        ],
    }


@router.post('/feedback', status_code=201)
def submit_feedback(request: Request, body: FeedbackInput):
    path = request.app.state.active_database_path
    if not is_postgres(path):
        return unavailable()
    actor = current_identity.get()
    feedback_id = str(body.submissionId)
    with database(path) as connection, transaction(connection):
        inserted = connection.execute(
            '''INSERT INTO user_feedback (id, submitter_id, submitter_email, category, description)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING''',
            (feedback_id, actor.id, actor.email, body.category, body.description),
        )
        if not inserted.rowcount:
            existing = connection.execute(
                'SELECT submitter_id, category, description FROM user_feedback WHERE id=?', (feedback_id,),
            ).fetchone()
            if (existing is None or str(existing[0]) != actor.id
                    or existing[1] != body.category or existing[2] != body.description):
                return error_response('feedback_conflict', '反馈提交已冲突，请重新打开表单。', 409)
    return result({'id': feedback_id}, 201)


@router.get('/admin/feedback')
def list_feedback(request: Request, category: Category | None = None, status: Status | None = None,
                  page: Annotated[int, Query(ge=1, le=10000)] = 1):
    path = request.app.state.active_database_path
    if not is_postgres(path):
        return unavailable()
    with database(path, read_only=True) as connection:
        rows = connection.execute(
            '''SELECT id, category, left(description, 120), status, created_at FROM user_feedback
               WHERE (CAST(? AS TEXT) IS NULL OR category=?) AND (CAST(? AS TEXT) IS NULL OR status=?)
               ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?''',
            (category, category, status, status, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
        ).fetchall()
    return result({'items': [
        {'id': str(row[0]), 'category': row[1], 'summary': row[2], 'status': row[3], 'createdAt': row[4].isoformat()}
        for row in rows[:PAGE_SIZE]
    ], 'hasMore': len(rows) > PAGE_SIZE})


@router.get('/admin/feedback/{feedback_id}')
def get_feedback(request: Request, feedback_id: UUID):
    path = request.app.state.active_database_path
    if not is_postgres(path):
        return unavailable()
    with database(path, read_only=True) as connection:
        data = detail(connection, str(feedback_id))
    return result(data) if data else error_response('feedback_missing', '未找到这条反馈。', 404)


@router.patch('/admin/feedback/{feedback_id}')
def update_feedback(request: Request, feedback_id: UUID, body: FeedbackUpdate):
    path = request.app.state.active_database_path
    if not is_postgres(path):
        return unavailable()
    actor = current_identity.get()
    feedback_id = str(feedback_id)
    with database(path) as connection, transaction(connection):
        row = connection.execute(
            'SELECT status, note, revision FROM user_feedback WHERE id=? FOR UPDATE', (feedback_id,),
        ).fetchone()
        if row is None:
            return error_response('feedback_missing', '未找到这条反馈。', 404)
        if row['revision'] != body.revision:
            return error_response('feedback_stale', '这条反馈已更新，请重新读取详情后再保存。', 409)
        if row['status'] != body.status or row['note'] != body.note:
            revision = body.revision + 1
            connection.execute(
                'UPDATE user_feedback SET status=?, note=?, revision=? WHERE id=?',
                (body.status, body.note, revision, feedback_id),
            )
            connection.execute(
                '''INSERT INTO feedback_history (feedback_id, revision, actor_id, old_status, new_status, note)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (feedback_id, revision, actor.id, row['status'], body.status, body.note),
            )
        data = detail(connection, feedback_id)
    return result(data)
