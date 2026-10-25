# Task 4 Report: 两轮无效果的 HTTP 级用例

## Status

DONE

## What changed

Single file: `backend/tests/test_proactive_flow.py`

1. `ProactiveClient.decide` — inserted a `mode == 'noeffect'` branch after the `if fresh:` question block and before the `ctx = self.runtime.repo.get_session_context(...)` line, verbatim per the brief:
   - first call asks the effect question (`这段经历上线或交付后，带来了哪些实际变化？`);
   - second call asks the softened follow-up (`没上线也可以，这件事有没有让流程或结果发生变化？`);
   - third call proposes a `replace` change with `参与` → `协助` and `gaps: []`;
   - after the proposal, finishes with `outcome: completed` (loop upgrades to `waiting_user` because a suggestion is pending).
2. Appended `ProactiveFlowTests.test_no_effect_still_produces_an_honest_suggestion` verbatim at the end of the class (before `if __name__ == '__main__'`).

No application code or other tests were modified. Existing helpers (`ready`, `turn`, `pending`, `SimpleNamespace`, `FakeClient.proposed`) were reused.

## Test commands and results

Target test:

```
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_proactive_flow.ProactiveFlowTests.test_no_effect_still_produces_an_honest_suggestion -q
```

Result: `Ran 1 test in 0.996s` — OK.

Adjacent regression:

```
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_proactive_flow tests.test_agent_integration tests.test_segment_guard tests.test_personal_evidence -q
```

Result: `Ran 55 tests in 28.821s` — OK.

## Why it passes without touching app code

Flow observed:

- Turn 1 (`继续`): fresh branch asks the responsibility question.
- Turn 2: the effect phrase is absent from the assembled context, so the `noeffect` branch asks the first effect question. No suggestion yet.
- Turn 3 (`没有上线，只在本地运行。`): the first effect question is now recorded as asked, the second prompt phrase is absent, so the softened follow-up is asked. `pending()` stays empty, so `question_declined` does not fire (slot `effect` was never declined).
- Turn 4 (`确实没有别的变化。`): both effect questions are settled → `effect_state == "absent"` (Task 1). Guard's absent path allows `propose_change` and the rewrite (`参与` → `协助`) contains no new effect-claim words (Task 2). Assertions on `协助项目一` / no `提升` / no `效率` hold.

## Concerns

- The `noeffect` script decides its stage by searching the assembled context for the literal question phrases rather than tracking state explicitly. This matches the brief and existing `FakeClient` conventions, but it would break if the prompt wording in `app/agent/loop.py` changes.
- The branch finishes with `outcome: completed`; the loop converts it to `waiting_user` because a suggestion is pending. This is intentional per existing behavior, not an assertion in the test.
- No git commands were run (repository is not a git repo).
