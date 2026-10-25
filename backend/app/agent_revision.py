"""Recognize bounded wording feedback only when Host has one valid target."""
import re

_STYLE = re.compile(r'冗余|啰嗦|太长|太随意|太口语|口语化|不够正式|不够简洁|精简|简短|简洁|压缩|更正式|书面|太笼统|不够具体')
_AMBIGUOUS = re.compile(r'如果|假如|假设|比如|例如|他说|她说|[“”「」"？?]|不要改|不用改|不改了|暂不改|先不改|不是.{0,8}(?:这|这一)条')


def wording_feedback(text):
    return bool(_STYLE.search(text)) and not _AMBIGUOUS.search(text)
