# Task 3 Report: Context hint, non-tech copy, decide prompt

## Status

DONE

## What changed

1. `backend/app/agent/context.py`
   - Import now `from app.agent.memory import ExperienceDoc, Slot, effect_state` (line 3).
   - Added `_effect_block(doc)` before `_sources_block` (lines 62-75). Maps `effect_state` to:
     - `confirmed` -> `已确认：<statement clipped to 200>`
     - `followup` -> `已追问 1 次，仍待具体变化（最多再问 1 次）`
     - `absent` -> `用户已说明没有可确认的实际效果：照实改写，不写效果类结论`
     - `missing` -> `尚未询问`
   - Focus section now emits `## 实际效果\n{_effect_block(focus)}` between `## 已确认事实` and `## 问题与缺口` (lines 150-152).

2. `backend/app/agent/loop.py`
   - `_PHRASES["tech"]` changed from `这段经历用到了哪些技术` to `这段经历用到了哪些工具、方法或资源` (line 49).

3. `backend/app/agent/prompts/decide.md` (paragraph-level replacements only)
   - Line 18: "(A) 技术决策" -> "(A) 方法决策".
   - Line 24: item 3 examples expanded with non-technical actions.
   - Line 25: item 4 generalized to business/result metrics, tech vs non-tech metrics, added "只在本地运行" to the not-an-effect list.
   - Lines 27-28: items 6/7 generalize from tech-only to methods/tools aligned to target role; no keyword stuffing.
   - Line 33: `tech` clarified as 工具/方法细节.
   - Line 34: rewritten so an absent effect never ends the segment with `limited`; only missing action + unanswered user still allows `limited`.
   - Line 37: decline rule no longer forces `finish(limited)` on effect decline; delivery status (no launch/deploy/local-only) is not treated as refusal; one follow-up then honest rewrite.
   - Line 42: adds the "没上线也可以..." follow-up phrasing.
   - Line 51: `after` guidance generalized to methods/tools and explicitly forbids effect conclusions when effect is unconfirmed.

4. `backend/tests/test_agent_session_policy.py`
   - Added `BrakeTests.test_context_explains_absent_effect` (lines 322-350), verbatim from brief.

## Test commands and counts

Step 2 (expected failure, confirmed):
```
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_agent_session_policy.BrakeTests.test_context_explains_absent_effect -q
```
Result: 1 test, FAILED with `'用户已说明没有可确认的实际效果' not found in ...`.

Step 6 (green):
```
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_agent_session_policy tests.test_effect_gate -q
```
Result: Ran 50 tests, OK.

## Concerns

- `_effect_block`'s `confirmed` branch uses `next(...)` without a default. It is safe because `effect_state` returns `confirmed` only when an active `effect` fact exists, but it is coupled to that invariant in memory.py.
- No changes to guard.py, memory.py, router.py, or frontend, per scope.
