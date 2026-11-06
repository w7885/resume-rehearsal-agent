"""Host-owned, versioned intake state. No model, provider or budget calls.

Only trusted orchestration supplies intent hashes and explicit retry decisions.
Permission epochs invalidate results, but never replenish an episode's allowance.
"""
import re
from copy import deepcopy
from uuid import uuid4

from app.repository import RepositoryError
from app.web_target import _load, _put

KEY = 'web_intake'
PHASES = frozenset({'needs_direction', 'exploring', 'awaiting_selection',
                    'awaiting_source_consent', 'ready', 'needs_material',
                    'deferred', 'failed'})

_DECLINE = frozenset({
    '不查', '不查了', '不搜', '不搜了', '不用查', '不用搜', '不要查',
    '不联网', '不用联网', '不需要联网', '无需搜索', '先不查', '先不搜',
    '按通用', '通用修改', '跳过联网',
})


def is_web_decline(text):
    # 先去掉空白和首尾标点：同一句“不查了。”不应因排版或句号漏判
    value = re.sub(r'\s+', '', text or '').strip('。！!，,.；;')
    # 剥离口语前缀，让“这次不查了”归一到词表里的“不查”
    for prefix in ('这次', '这回', '暂时', '先'):
        if value.startswith(prefix):
            value = value[len(prefix):]
    # 剥离句末语气词，如“不查了吧”
    value = re.sub(r'[了吧啦]+$', '', value)
    # 只做全等匹配，避免“不查户口，但要联网看岗位”这类内容回答被误判为拒绝
    return value in _DECLINE


def load(connection, session_id):
    value = _load(connection, session_id, KEY)
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get('formatVersion')) is not int
            or value['formatVersion'] != 1):
        raise RepositoryError('unsupported_intake_version', '联网接续状态版本不受支持。', False)
    if (value.get('phase') not in PHASES
            or type(value.get('revision')) is not int or value['revision'] < 1
            or not isinstance(value.get('episodeId'), str)
            or not value['episodeId']
            or not isinstance(value.get('intentHash'), str) or not value['intentHash']
            or type(value.get('permissionRevision')) is not int or value['permissionRevision'] < 1
            or type(value.get('resumeGeneration')) is not int or value['resumeGeneration'] < 1
            or type(value.get('searchCount')) is not int or not 0 <= value['searchCount'] <= 2
            or not isinstance(value.get('fetchIdentities'), list)
            or len(value['fetchIdentities']) > 3
            or any(not isinstance(item, str) or not item for item in value['fetchIdentities'])
            or len(set(value['fetchIdentities'])) != len(value['fetchIdentities'])
            or not isinstance(value.get('requestReceipts'), dict)):
        raise RepositoryError('invalid_intake_state', '联网接续状态无效，请保留记录并检查。', False)
    return value


def ensure(connection, session, intent_hash, *, explicit_retry=False):
    """Initialize or invalidate result bindings without discarding spent attempts."""
    if not isinstance(intent_hash, str) or not intent_hash or len(intent_hash) > 256:
        raise RepositoryError('invalid_input', '公开目标指纹无效。')
    if type(explicit_retry) is not bool:
        raise RepositoryError('invalid_input', '重试标记无效。')
    old = load(connection, session['session_id'])
    if old and old['intentHash'] == intent_hash and not explicit_retry:
        if (old['permissionRevision'] == session['web_settings_revision']
                and old['resumeGeneration'] == session['current_resume_generation']):
            return old
        value = deepcopy(old)
        value.update(permissionRevision=session['web_settings_revision'],
                     resumeGeneration=session['current_resume_generation'],
                     phase='needs_direction', candidates=[], selectedCandidateId=None,
                     confirmedThirdPartyReferenceIds=[], outcomeCode=None)
    else:
        value = dict(formatVersion=1, revision=0, intentHash=intent_hash,
                     permissionRevision=session['web_settings_revision'],
                     resumeGeneration=session['current_resume_generation'],
                     episodeId='web-episode-' + uuid4().hex, phase='needs_direction',
                     explicitFields={}, userUrlBindings=[], candidates=[],
                     selectedCandidateId=None, confirmedThirdPartyReferenceIds=[],
                     searchCount=0, fetchIdentities=[], requestReceipts={}, outcomeCode=None)
        if explicit_retry:
            value['skipLegacyImport'] = True
    value['revision'] = (old['revision'] if old else 0) + 1
    _put(connection, session['session_id'], KEY, value)
    return value


def _bound(connection, session, *, episode_id, expected_revision):
    value = load(connection, session['session_id'])
    if (not value or value['episodeId'] != episode_id
            or type(expected_revision) is not int or value['revision'] != expected_revision
            or value['permissionRevision'] != session['web_settings_revision']
            or value['resumeGeneration'] != session['current_resume_generation']):
        raise RepositoryError('stale_state', '目标、权限或联网接续状态已变化。')
    if not session['web_enabled']:
        raise RepositoryError('web_disabled', '联网已关闭。')
    return value


def set_phase(connection, session, *, episode_id, expected_revision, phase):
    if phase not in PHASES:
        raise RepositoryError('invalid_input', '联网阶段无效。')
    value = _bound(connection, session, episode_id=episode_id, expected_revision=expected_revision)
    if phase == 'ready':
        # Evidence qualification belongs to the next integration step. Never let
        # a generic phase setter bypass target selection and source acceptance.
        raise RepositoryError('scope_violation', '可用岗位依据必须通过来源核对后接入。', False)
    value.update(phase=phase, revision=value['revision'] + 1)
    _put(connection, session['session_id'], KEY, value)
    return value


def claim_attempt(connection, session, *, episode_id, expected_revision,
                  request_id, operation, fingerprint, url_identity=None):
    """Reserve an attempt in the caller's transaction before provider dispatch.

    This does not reserve money. Receipts survive settings changes and runs;
    caller must still use the existing permission/run/budget guards for I/O.
    """
    value = _bound(connection, session, episode_id=episode_id, expected_revision=expected_revision)
    if (operation not in {'search', 'fetch'} or not isinstance(request_id, str)
            or not request_id or len(request_id) > 128
            or not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 128):
        raise RepositoryError('invalid_input', '联网尝试参数无效。')
    if operation == 'fetch' and (not isinstance(url_identity, str)
                                 or not url_identity or len(url_identity) > 4096):
        raise RepositoryError('invalid_input', '页面标识无效。')
    receipt = {'operation': operation, 'fingerprint': fingerprint, 'urlIdentity': url_identity}
    previous = value['requestReceipts'].get(request_id)
    if previous:
        if previous != receipt:
            raise RepositoryError('request_conflict', '联网请求编号已用于其他输入。')
        return value, False
    if operation == 'search':
        if value['searchCount'] >= 2:
            raise RepositoryError('web_limit', '本轮搜索次数已达上限。')
        value['searchCount'] += 1
    else:
        if url_identity in value['fetchIdentities']:
            raise RepositoryError('web_duplicate_page', '本轮已尝试读取该页面，请复用已有结果。')
        if len(value['fetchIdentities']) >= 3:
            raise RepositoryError('web_limit', '本轮页面读取次数已达上限。')
        value['fetchIdentities'].append(url_identity)
    value['requestReceipts'][request_id] = receipt
    value['revision'] += 1
    _put(connection, session['session_id'], KEY, value)
    return value, True
