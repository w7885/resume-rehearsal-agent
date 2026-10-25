# Task 2 Report: Guard 放行 absent、拦截新增效果、修复中文数字

**Status:** DONE

## What changed

### Tests (`backend/tests/test_effect_gate.py`)
- Import switched to `EFFECT_EXHAUSTED_TEXT` and added `SourceText`.
- `decision()` gained `after` / `before` parameters; `state()` gained `before` and the
  `'blocked'` fixture branch became `'absent'`.
- `EffectGuardTests` replaced: absent allows honest rewrite; new effect wording is
  blocked with `effect_claim_without_evidence`; wording already in the original is
  allowed; declined now gets a follow-up; third effect question yields retryable
  `effect_exhausted`; “一个” / “一个次/人” filler is not a number while added “三人”
  is a retryable `unsupported_number`.
- `EffectFallbackTests`: absent fixture branch; decline asks one follow-up; absent
  skips the effect slot and falls through to evidence; end-to-end absent run produces
  a suggestion and records no `missing_effect` issue.

### Guard (`backend/app/agent/guard.py`)
- L22-26: `MISSING_EFFECT_TEXT` replaced by `EFFECT_EXHAUSTED_TEXT` and `_EFFECT_CLAIMS`.
- L53-58: new `_cn_numbers()` skips single “一” before 个/次/人.
- L144-148: effect follow-up is allowed even when the effect slot was declined and
  the state is `followup`.
- L208: `unsupported_number` is now retryable.
- L264: third effect question -> `effect_exhausted` (retryable).
- L327-362: `propose_change` allows `absent`, blocking only newly introduced claim
  words (`_EFFECT_CLAIMS` not present in `args.before` / current block).

### Loop (`backend/app/agent/loop.py`)
- L13-19: dropped `MISSING_EFFECT_TEXT` import.
- L94-113: removed `blocked -> (None, None)`; `absent` skips only the effect slot.
- L361-364: `fallback()` no longer emits a `missing_effect` limited result.
- L581-590: removed the `FinishStep` `blocked -> missing_effect` issue block.

## Commands and results

1. Before source changes (expected red):
   `cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_effect_gate -q`
   -> ERROR: `ImportError: cannot import name 'EFFECT_EXHAUSTED_TEXT'` (expected).

2. After source changes:
   same command -> `Ran 32 tests ... OK`.

3. Regression subset:
   `... -m unittest tests.test_segment_guard tests.test_agent_session_policy tests.test_chat_decisions tests.test_proactive_flow -q`
   -> `Ran 59 tests ... OK`.

## Concerns
- None blocking. The full suite is Task 5; only the related 59 tests were re-run here.
