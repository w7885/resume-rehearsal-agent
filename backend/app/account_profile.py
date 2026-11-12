from fastapi import APIRouter, Request
from pydantic import field_validator
from starlette.responses import JSONResponse

from app.api import InputModel, error_response
from app.auth import current_identity
from app.db import database, transaction


router = APIRouter(prefix='/api')


class ProfileUpdate(InputModel):
    displayName: str

    @field_validator('displayName')
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 30 or '\x00' in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('Invalid display name')
        return value


def profile_response(identity, display_name):
    return JSONResponse({
        'id': identity.id, 'email': identity.email,
        'emailVerified': identity.email_verified, 'displayName': display_name,
    }, headers={'Cache-Control': 'no-store'})


@router.get('/me')
def get_profile(request: Request):
    identity = current_identity.get()
    if identity is None:
        return error_response('unauthorized', '请先登录。', 401)
    with database(request.app.state.active_database_path, read_only=True) as connection:
        row = connection.execute('SELECT display_name FROM account_profiles WHERE user_id=?', (identity.id,)).fetchone()
    return profile_response(identity, row[0] if row else '')


@router.patch('/me')
def update_profile(request: Request, body: ProfileUpdate):
    identity = current_identity.get()
    if identity is None:
        return error_response('unauthorized', '请先登录。', 401)
    with database(request.app.state.active_database_path) as connection, transaction(connection):
        connection.execute('''INSERT INTO account_profiles(user_id, display_name) VALUES (?, ?)
            ON CONFLICT (user_id) DO UPDATE SET display_name=excluded.display_name''',
                           (identity.id, body.displayName))
    return profile_response(identity, body.displayName)
