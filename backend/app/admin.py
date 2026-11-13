"""Read-only account support and aggregate reporting for administrators."""
from datetime import datetime, time, timedelta, timezone
import re
import sqlite3

from fastapi import APIRouter, Request
from pydantic import Field, field_validator
from starlette.responses import JSONResponse

from app.api import InputModel, error_response
from app.db import database
from app.postgres import is_postgres


router = APIRouter(prefix='/api/admin')
BEIJING = timezone(timedelta(hours=8))


class AccountLookup(InputModel):
    email: str = Field(max_length=254)

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r'[^\s@\x00]+@[^\s@\x00.]+(?:\.[^\s@\x00.]+)+', value) or any(
            0xD800 <= ord(character) <= 0xDFFF for character in value
        ):
            raise ValueError('A complete email address is required')
        return value


def lookup_unavailable():
    return error_response('account_lookup_unavailable', '暂时无法查询账号，请稍后重试。', 503)


@router.post('/accounts/lookup')
def lookup_account(request: Request, body: AccountLookup):
    path = request.app.state.active_database_path
    if not is_postgres(path):
        return lookup_unavailable()
    try:
        with database(path, read_only=True) as connection:
            rows = connection.execute(
                '''SELECT email, created_at, email_confirmed_at IS NOT NULL AS email_verified
                   FROM auth.users WHERE lower(email) = ? LIMIT 2''', (body.email,),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return lookup_unavailable()
    if not rows:
        return error_response('account_not_found', '未找到该邮箱对应的账号。', 404)
    if len(rows) != 1:
        return lookup_unavailable()
    row = rows[0]
    return JSONResponse({
        'email': row['email'], 'createdAt': row['created_at'].isoformat(), 'emailVerified': row['email_verified'],
    }, headers={'Cache-Control': 'no-store'})


def read_overview(connection, today):
    first_day = today - timedelta(days=14)
    recent_start = today - timedelta(days=7)
    counts = connection.execute(
        '''SELECT CAST(created_at AT TIME ZONE 'Asia/Shanghai' AS date) AS day,
                  count(*) AS registrations,
                  count(email_confirmed_at) AS verified
           FROM auth.users WHERE created_at >= ? AND created_at < ? GROUP BY 1''',
        (datetime.combine(first_day, time.min, BEIJING), datetime.combine(today, time.min, BEIJING)),
    ).fetchall()
    total = connection.execute('SELECT count(*) FROM auth.users').fetchone()[0]
    feedback = dict(connection.execute('SELECT status, count(*) FROM user_feedback GROUP BY status').fetchall())
    by_day = {row['day']: (row['registrations'], row['verified']) for row in counts}
    days = [first_day + timedelta(days=offset) for offset in range(14)]
    series = [{'date': day.isoformat(), 'count': by_day.get(day, (0, 0))[0]} for day in days]
    recent = sum(by_day.get(day, (0, 0))[0] for day in days if day >= recent_start)
    verified = sum(by_day.get(day, (0, 0))[1] for day in days if day >= recent_start)
    return {
        'totalRegistrations': total, 'recentRegistrations': recent,
        'emailVerification': {'verified': verified, 'unverified': recent - verified},
        'registrations': {'previous': series[:7], 'recent': series[7:]},
        'feedback': {status: feedback.get(status, 0) for status in ('pending', 'in_progress', 'resolved')},
    }


def beijing_today():
    return datetime.now(BEIJING).date()


@router.get('/overview')
def overview(request: Request):
    path = request.app.state.active_database_path
    if not is_postgres(path):
        return error_response('overview_unavailable', '此环境暂不支持读取概览。', 503)
    try:
        with database(path, read_only=True) as connection:
            connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            try:
                data = read_overview(connection, beijing_today())
            finally:
                connection.rollback()
    except (sqlite3.Error, OSError):
        return error_response('overview_unavailable', '暂时无法读取概览，请稍后重试。', 503)
    return JSONResponse(data, headers={'Cache-Control': 'no-store'})
