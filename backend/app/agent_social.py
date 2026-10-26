"""Brief social replies use conversation state without running the interview."""
import re

from app.agent_progress import plan
from app.repository import RepositoryError

THANKS = {'谢谢', '谢谢你', '谢谢您', '谢谢啦', '感谢', '感谢你', '多谢', '辛苦了', '辛苦你了'}
ACKNOWLEDGEMENTS = {'好', '好的', '好哒', '嗯', '嗯嗯', '哦', '噢', '明白了', '知道了', '了解了', '收到', '行', '可以', '没问题', '是', '是的'}


def social_kind(text):
    # Full-message matching preserves mixed requests such as "谢谢，第二段改短".
    token = re.sub(r'[\s。！!，,～~]+', '', text)
    return 'thanks' if token in THANKS else 'ack' if token in ACKNOWLEDGEMENTS else None


def handle(runtime, turn):
    kind = social_kind(turn['userMessage']['content'])
    if kind is None or (kind == 'ack' and runtime._pending()):
        return False
    with runtime.repo._operation(runtime.context) as (connection, _):
        if connection.execute('SELECT 1 FROM chat_message_attachments WHERE session_id=? AND message_id=? LIMIT 1',
                              (runtime.sid, turn['userMessage']['messageId'])).fetchone():
            return False
    context = runtime.repo.get_session_context(runtime.context)
    # With no draft, "是的" may confirm a material-intake prompt. Leave that
    # flow untouched instead of swallowing its authorization as small talk.
    if kind == 'ack' and not context['draft']['blocks']:
        return False
    try:
        progress = plan(runtime)
    except RepositoryError as error:
        if error.code != 'edit_scope_ambiguous':
            raise
        progress = {'blocks': [], 'remaining': [], 'review': [], 'pending': context['suggestions']}
    pending = progress['pending']
    if pending:
        if kind == 'ack':
            # An effective suggestion still uses the original approval checks.
            return False
        text = ('不客气。刚才的建议还没有写入当前稿，要采用这条吗？也可以继续调整。'
                if len(pending) == 1 else '不客气。还有几条建议等你决定，想采用或调整时，告诉我对应哪条就好。')
        runtime._reply(text,
                       'change_question', [p['suggestionId'] for p in pending])
        return True
    completed = bool(progress['blocks']) and not progress['remaining'] and not progress['review']
    if kind == 'ack' and not completed and re.sub(r'[\s。！!，,～~]+', '', turn['userMessage']['content']) in {'是', '是的', '可以', '没问题', '好', '行'}:
        return False
    if kind == 'thanks':
        text = ('不客气，这轮就先到这里。你可以慢慢看看当前稿，有想调整的地方再告诉我。'
                if completed else '不客气，我们按你的节奏来。有想补充或调整的地方，直接告诉我就好。')
    elif completed:
        text = '好，先到这里。' if turn['userMessage']['content'].strip() not in {'哦', '噢', '嗯', '嗯嗯'} else '嗯，慢慢看就好。'
    else:
        text = '好，不着急。准备好后可以直接补充，暂时想不起来也没关系。'
    runtime._reply(text)
    return True
