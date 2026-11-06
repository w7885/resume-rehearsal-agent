"""Bounded ownership classification for active web intake asks."""
import asyncio
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import web_intake
from app.agent.llm import LLMError
from app.agent.memory import _block_title
from app.repository import RepositoryError
from app.web_candidates import _CONFIRM, _NUMERAL, parse_choice, parse_numeral
from app.web_citations import goal_state

_URL_OR_GENERIC = re.compile(
    r'https?://|通用修改|按通用|^重试联网[。！!]*$|^重新搜索[。！!]*$')
_EXPERIENCE_EDIT = re.compile(
    r'(?:改|修改|调整|优化|重写|润色|处理|切换|切到|切回).{0,6}(?:第)?\s*' + _NUMERAL + r'\s*[段个]'
    r'|(?:第)?\s*' + _NUMERAL + r'\s*[段个].{0,6}(?:改|修改|调整|优化|重写|润色|处理)'
    r'|(?:第)?\s*' + _NUMERAL + r'\s*[段个](?:经历|简历)')


def _clean(text):
    return (text or '').strip().rstrip('。！!')


_TITLE_SIGNAL = re.compile(r'^\s{0,3}#{1,6}\s+|^\*\*.+\*\*$|[|｜]|\s{2,}')


def _experience_title(markdown):
    """首行须有明确标题信号（Markdown 标题、加粗、表格分隔）才外发；
    短正文行与标题无法区分，未识别就只给占位名，绝不能把正文送出本机。"""
    first = next((line.strip() for line in (markdown or '').splitlines()
                  if line.strip()), '')
    if not first or not _TITLE_SIGNAL.search(first):
        return '未命名经历'
    title = re.split(r'\s{2,}|[|｜]', _block_title(markdown))[0].strip()
    if not title or len(title) > 20 or re.search(r'[，,。；;！？!?]', title):
        return '未命名经历'
    return title


def _selection_or_edit(value, state=None):
    # 先认经历编辑动作：出现“改/切换”时，数字指向简历段落而非候选编号。
    if match := _EXPERIENCE_EDIT.search(value):
        # 只从命中片段取序号，避免正文其它数字干扰；解析复用共享实现。
        numeral = re.search(_NUMERAL, match.group())
        return {'decision': 'edit_experience',
                'index': parse_numeral(numeral[0]) if numeral else None}
    if state and state.get('phase') == 'awaiting_selection':
        if value in _CONFIRM:
            return {'decision': 'select_candidate',
                    'index': 1 if len(state.get('candidates') or []) == 1 else None}
        for index, candidate in enumerate(state.get('candidates') or [], 1):
            if value == candidate.get('candidateId'):
                return {'decision': 'select_candidate', 'index': index}
        index = parse_choice(value)
        if index:
            return {'decision': 'select_candidate', 'index': index}
    return None


def classify_text(text, state=None):
    # “通用修改”同时出现在拒绝词表，先判联网请求，由它接管语义。
    value = _clean(text)
    if _URL_OR_GENERIC.search(value):
        return {'decision': 'deterministic_web'}
    if web_intake.is_web_decline(value):
        return {'decision': 'decline'}
    return _selection_or_edit(value, state)


# 只有仍停在联网问答或失败收尾的阶段才值得问模型；ready/deferred/exploring
# 已有确定走向或正在自动流程中，模型介入只会抢走普通对话。
LIVE_PHASES = frozenset({'awaiting_selection', 'needs_direction',
                         'needs_material', 'failed'})

_OWNERSHIP_INSTRUCTION = (
    '判断这条用户消息在联网流程中的归属。只输出 JSON。'
    'select_candidate：用户在回答“找到以下候选……”并选择其中一个岗位；'
    '纯数字、“第N段”“第N个岗位”都按选择处理。'
    'edit_experience：用户明确要改或切换某一段简历经历（出现改、修改、调整、切换等动词，'
    '或“第N段经历”）。web_request：用户提出新的联网检索请求，或要求按具体岗位重新查找。'
    'decline：用户表示不查、不联网、先按通用处理。normal：其他情况。'
    '无法确定时输出 normal。不要根据职位名称猜测用户经历。'
)


class WebOwnership(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decision: Literal['select_candidate', 'edit_experience', 'web_request',
                      'decline', 'normal']
    index: int | None = Field(default=None, ge=1, le=9)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


async def _model_ownership(runtime, turn, state):
    blocks = runtime.repo.get_draft(runtime.context).get('blocks', [])[:10]
    # 只喂消息、阶段、候选与经历标题，不喂简历正文或 URL：
    # 判定只需要归属线索，正文既无必要也不该离开本地。
    payload = {
        'message': turn['userMessage']['content'][:500],
        'phase': state['phase'],
        'candidates': [
            {'index': index, 'company': item.get('company', ''),
             'role': item.get('role', '')}
            for index, item in enumerate(state.get('candidates') or [], 1)
        ],
        'experiences': [
            {'index': index,
             'title': _experience_title(block.get('markdown', ''))}
            for index, block in enumerate(blocks, 1)
        ],
    }
    try:
        async with asyncio.timeout(10):
            data = await runtime._json(_OWNERSHIP_INSTRUCTION, payload)
        parsed = WebOwnership.model_validate(data)
    except (TimeoutError, LLMError, RepositoryError, ValidationError):
        # 分类器只是增强：超时、模型错误、读取失败或 schema 不符都降级成 None，
        # 交回普通对话，绝不让它阻断或拖延主流程。
        return None
    if parsed.confidence < 0.7 or parsed.decision == 'normal':
        return None
    if parsed.decision in {'select_candidate', 'edit_experience'} and parsed.index is None:
        return None
    return {'decision': parsed.decision, 'index': parsed.index}


async def classify_ownership(runtime, turn):
    text = turn['userMessage']['content']
    value = _clean(text)
    with runtime.repo._operation(runtime.context) as (connection, session):
        if not session['web_enabled']:
            return None
        if _URL_OR_GENERIC.search(value):
            return {'decision': 'deterministic_web'}
        if web_intake.is_web_decline(value):
            return {'decision': 'decline'}
        state = web_intake.load(connection, session['session_id'])
        if (not state or state['permissionRevision'] != session['web_settings_revision']
                or state['intentHash'] != goal_state(connection, session)['baseHash']):
            return None
        prompt_id = (state.get('candidatePrompt') or {}).get('messageId')
        # 回复绑定优先：用户明确点的是候选提示，正文里的“改第N段”也只是在选候选。
        bound_candidate = bool(turn.get('replyToMessageId')
                               and turn['replyToMessageId'] == prompt_id
                               and state['phase'] == 'awaiting_selection')
        bound_input = bool(turn.get('replyToMessageId')
                           and turn['replyToMessageId'] == state.get('inputPromptId'))
        decision = None if bound_candidate else _selection_or_edit(value, state)
        phase = state['phase']
    # 连接已关闭；state 只是一份普通 dict 快照，可安全用于模型输入。
    if bound_candidate:
        index = parse_choice(value)
        return {'decision': 'select_candidate', 'index': index}
    if bound_input:
        return {'decision': 'deterministic_web'}
    if decision:
        return decision
    if phase not in LIVE_PHASES:
        return None
    return await _model_ownership(runtime, turn, state)


async def resolve_ownership(runtime, turn):
    try:
        ownership = await classify_ownership(runtime, turn)
    except RepositoryError:
        ownership = None
    runtime.web_ownership = ownership
    turn['webOwnership'] = ownership
    return bool(ownership and ownership['decision'] not in {'edit_experience', 'normal'})
