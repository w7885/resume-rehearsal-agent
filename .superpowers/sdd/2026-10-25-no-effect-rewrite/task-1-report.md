# Task 1 Report: effect_state `absent` + delivery markers

Status: DONE

## What changed

1. **`backend/app/agent/memory.py`** — replaced `effect_state` (lines 173-188).
   - Return type is now `Literal["confirmed", "followup", "absent", "missing"]` (removed `blocked`).
   - Removed the `if "effect" in doc.declined: return "blocked"` early exit.
   - `settled >= 2` now returns `"absent"` instead of `"blocked"`.
   - Semantics untouched otherwise: active effect fact → `confirmed`; one settled effect question → `followup`; none → `missing`.

2. **`backend/app/agent/router.py`** — delivery status no longer counts as refusal.
   - Added `_DELIVERY_MARKERS` (lines 45-48) after `_EFFECT_MARKERS`:
     `("没有上线", "没上线", "未上线", "没有部署", "没部署", "只在本地", "本地运行", "单机")`.
   - `_is_decline` line 55 now checks `_EFFECT_MARKERS + _DELIVERY_MARKERS` before the `_DECLINE_WORDS` fallback, so "没上线/只在本地" returns `False`.
   - Lines 51-56 total; no other logic changed.

3. **`backend/tests/test_effect_gate.py`** — appended `EffectStateTests` (8 tests) and `DeliveryDeclineTests` (3 tests) verbatim from the brief, before `if __name__ == '__main__':`. No other test file touched.

No other files touched. No new abstractions, helpers, or comments added.

## Test commands and results

Failure-first run (before source changes):

```
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_effect_gate.EffectStateTests tests.test_effect_gate.DeliveryDeclineTests -q
```

Result: `Ran 11 tests in 0.003s — FAILED (failures=4)`
- `test_two_answers_are_absent` → `'blocked' != 'absent'`
- `test_answer_plus_decline_is_absent` → `'blocked' != 'absent'`
- `test_superseded_fact_with_two_questions_is_absent` → `'blocked' != 'absent'`
- `test_no_deployment_is_not_a_decline` → `True is not false`

Green run (after source changes, same command):

Result: `Ran 11 tests in 0.000s — OK` (11 passed, 0 failed).

## Concerns

- **Expected downstream breakage, not addressed in Task 1:** `backend/app/agent/guard.py` and `backend/app/agent/loop.py` still reference the old `blocked` effect state, and `tests/test_effect_gate.py`'s pre-existing `EffectGuardTests` / `EffectFallbackTests` / `EffectDeclineTests` fixture the old behavior (`effect_state_value='blocked'`, `_fallback_question` returning `(None, None)` on missing effect). Per the brief these are Task 2-4 scope; only the two new classes were run. The wider suite will be red until those tasks land.
- `doc.declined` no longer influences `effect_state` at all; the unit-test fixtures still append to `declined` but the state is now derived solely from `facts`/`questions`, matching the brief's code.
