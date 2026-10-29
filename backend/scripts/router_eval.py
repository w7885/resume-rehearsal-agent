"""Manual router accuracy check. Not part of CI."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.llm import DeepSeekClient, LLMSettings
from app.agent_router import build_user_prompt, classify


def _value(out, key):
    return len(out.mixed) if key == 'mixed' else getattr(out, key)


async def main() -> int:
    if not os.getenv('DEEPSEEK_API_KEY'):
        print('DEEPSEEK_API_KEY missing; skipped.')
        return 0
    cases = json.loads((Path(__file__).parent / 'router_eval_cases.json').read_text(encoding='utf-8'))
    llm = DeepSeekClient(LLMSettings.from_env())
    hits, misses = 0, []
    for case in cases:
        prompt = build_user_prompt(message=case['message'], blocks=[], focus_id=None,
                                   question=None, suggestions=[], recent=[])
        result = await classify(llm, user=prompt)
        out = result.output
        ok = out is not None and all(_value(out, key) == value
                                     for key, value in case['expect'].items())
        hits += ok
        if not ok:
            misses.append({'message': case['message'], 'expect': case['expect'],
                           'got': out.model_dump() if out else result.fallback_reason})
    print(f'{hits}/{len(cases)} = {hits / len(cases):.0%}')
    for miss in misses:
        print(json.dumps(miss, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
