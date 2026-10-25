# 无实际效果时照实改写 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户两次都提供不出可确认实际效果时，仍基于职责、动作、方法给出不编造效果的改写，而不是结束本段。

**Architecture:** 把 `effect_state` 的 `blocked` 改为 `absent` 并允许 `propose_change`；Guard 用“新增效果措辞”检查守住不编造；loop 删除缺效果即 `limited` 的路径；上下文与提示词明确“照实改写”。同时修正中文单字数字误判，并把提示词措辞泛化到非技术岗。

**Tech Stack:** Python 3.13、FastAPI、pydantic、stdlib unittest、内存 store 与 FakeRepo（测试不花钱）。

## Global Constraints

- **仓库不是 git 仓库：不要执行任何 git 命令**（包括 `git add` / `git commit`）。每个任务以“目标测试通过”收尾。
- 测试命令统一为：
  `cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest <目标> -q`
- 不改前端 SSE 类型（只允许 `progress|question|done`）、不改 `agent_progress`、不重命名持久化字段 `tech`。
- 不得编造效果或数字。无效果时改写只能写职责、动作、方法与必要背景。
- 面向用户的问题必须恰好一个问号、不超过 60 字、一次只问一件事。
- 规格来源：`docs/superpowers/specs/2026-10-25-no-effect-rewrite-design.md`。


---

### Task 2: Guard 放行 `absent`、拦截新增效果、修复中文数字


**Files:**
- Modify: `backend/app/agent/guard.py:20-25, 48-49, 106-146, 189-195, 240-251, 295-337`
- Modify: `backend/app/agent/loop.py:13-20, 95-113, 355-379, 581-615`
- Test: `backend/tests/test_effect_gate.py`

**Interfaces:**
- Consumes: Task 1 的 `effect_state(...)`。
- Produces:
  - `guard.EFFECT_EXHAUSTED_TEXT`
  - 违规码 `effect_exhausted`（可重试）、`effect_claim_without_evidence`（可重试）
  - `unsupported_number` 变为可重试
  - `_fallback_question(doc) -> tuple[str | None, str | None]`（`absent` 跳过 effect）

- [ ] **Step 1: 改写 test_effect_gate 中的 Guard/fallback 用例**

把导入行改为：

```python
from app.agent.guard import EFFECT_EXHAUSTED_TEXT, GuardState, check
```

把 `decision`、`state` 两个辅助函数替换为：

```python
def decision(target='p1', after='协助完成接口测试。', before=BEFORE):
    return StepDecisionAdapter.validate_python({'action': 'tool', 'tool': 'propose_change', 'args': {
        'operation': 'replace', 'target': target, 'before': before, 'after': after,
        'reason': '梳理职责表达',
        'source_refs': [{'source_id': 'resume', 'source_revision': 1, 'quote': '接口测试'}],
        'gaps': []}})


def state(*, effect_state_value=None, direct=False, before=BEFORE):
    doc = ExperienceDoc(
        experience_id='p1', seq=1, kind='project', title='构造项目',
        raw_text=before, block_id='p1', block_revision=1,
    )
    doc.questions.append(Question(
        id='q0', slot='responsibility', text='你负责哪部分？',
        fingerprint='f0', outcome='answered',
    ))
    if effect_state_value == 'followup':
        doc.questions.append(effect_question(1))
    elif effect_state_value == 'absent':
        doc.questions.extend([effect_question(1), effect_question(2)])
    elif effect_state_value == 'declined':
        doc.questions.append(effect_question(1, 'declined'))
        doc.declined.append('effect')
    elif effect_state_value == 'active':
        doc.facts.append(Fact(
            id='f1', slot='effect', statement='接口测试效率提高',
            created_at='2026-01-01T00:00:00Z',
        ))
    elif effect_state_value == 'superseded':
        doc.facts.append(Fact(
            id='f1', slot='effect', statement='接口测试效率提高',
            status='superseded', created_at='2026-01-01T00:00:00Z',
        ))
    return GuardState(
        session=SessionContext(
            session=SessionInfo(session_id='test', experience_order=['p1']),
            draft_blocks=[DraftBlock(block_id='p1', markdown=before)],
        ),
        focus_doc=doc, anchor_text=before, focus_block_markdown=before,
        source_texts={'resume': before}, source_kinds={'resume': 'resume'},
        direct_rewrite=direct,
    )
```

把整个 `EffectGuardTests` 替换为：

```python
class EffectGuardTests(unittest.TestCase):
    def test_no_effect_questions_asks_effect(self):
        result = check(decision(), state())
        self.assertEqual(result.violation.code, 'ask_effect')
        self.assertTrue(result.violation.retryable)

    def test_one_answered_effect_question_asks_followup(self):
        result = check(decision(), state(effect_state_value='followup'))
        self.assertEqual(result.violation.code, 'ask_effect_followup')
        self.assertTrue(result.violation.retryable)

    def test_one_declined_effect_question_also_asks_followup(self):
        result = check(decision(), state(effect_state_value='declined'))
        self.assertEqual(result.violation.code, 'ask_effect_followup')

    def test_two_effect_questions_allow_honest_rewrite(self):
        self.assertIsNone(
            check(decision(), state(effect_state_value='absent')).violation
        )

    def test_absent_rewrite_cannot_add_effect_claim(self):
        result = check(
            decision(after='协助完成接口测试，提升了测试效率。'),
            state(effect_state_value='absent'),
        )
        self.assertEqual(result.violation.code, 'effect_claim_without_evidence')
        self.assertTrue(result.violation.retryable)

    def test_absent_rewrite_keeps_effect_wording_from_the_original(self):
        before = '方便记录信息，参与接口测试。'
        result = check(
            decision(before=before, after='方便记录信息，协助完成接口测试。'),
            state(effect_state_value='absent', before=before),
        )
        self.assertIsNone(result.violation)

    def test_active_effect_passes(self):
        self.assertIsNone(check(decision(), state(effect_state_value='active')).violation)

    def test_superseded_effect_is_still_missing(self):
        result = check(decision(), state(effect_state_value='superseded'))
        self.assertEqual(result.violation.code, 'ask_effect')

    def test_direct_rewrite_cannot_skip_effect(self):
        result = check(decision(), state(direct=True))
        self.assertEqual(result.violation.code, 'ask_effect')

    def test_third_effect_question_is_retryably_blocked(self):
        guard = state(effect_state_value='absent')
        step = AskStep(
            action='ask_user', message='还有没有别的实际变化？', slot='effect',
        )
        result = check(step, guard)
        self.assertEqual(result.violation.code, 'effect_exhausted')
        self.assertEqual(result.violation.message, EFFECT_EXHAUSTED_TEXT)
        self.assertTrue(result.violation.retryable)

    def test_cn_filler_one_is_not_a_number(self):
        result = check(
            decision(after='协助完成一个博客系统的接口测试。'),
            state(effect_state_value='absent'),
        )
        self.assertIsNone(result.violation)

    def test_added_cn_quantity_is_still_blocked(self):
        result = check(
            decision(after='协助三人完成接口测试。'),
            state(effect_state_value='absent'),
        )
        self.assertEqual(result.violation.code, 'unsupported_number')
        self.assertTrue(result.violation.retryable)
```

把 `EffectFallbackTests.doc` 里的 `'blocked'` 分支替换为 `'absent'`：

```python
        if kwargs.get('effect') == 'followup':
            doc.questions.append(effect_question(1))
        elif kwargs.get('effect') == 'absent':
            doc.questions.extend([effect_question(1), effect_question(2)])
        elif kwargs.get('effect') == 'declined':
            doc.questions.append(effect_question(1, 'declined'))
            doc.declined.append('effect')
```

把 `test_fallback_stops_when_effect_is_blocked` 替换为：

```python
    def test_fallback_after_decline_asks_one_followup(self):
        self.assertEqual(
            _fallback_question(self.doc(effect='declined')),
            ('除了使用情况，实际带来了什么变化？', 'effect'),
        )

    def test_fallback_skips_absent_effect(self):
        self.assertEqual(
            _fallback_question(self.doc(effect='absent')),
            ('有没有材料能支持这段经历的描述？', 'evidence'),
        )
```

把 `test_fallback_limited_records_missing_effect_issue` 替换为：

```python
    def test_absent_effect_still_produces_an_honest_suggestion(self):
        repo = FakeRepo(
            session_id=policy.SESSION,
            direction='前端开发实习',
            sources=[SourceText(
                source_id='resume', kind='resume', text=BEFORE,
            )],
            experience_order=['p1'],
            current_focus_id='p1',
            draft_blocks=[DraftBlock(
                block_id='p1', experience_id='p1', block_revision=1,
                markdown=BEFORE,
            )],
        )
        store = InMemoryStateStore()
        save_experience(store, policy.SESSION, self.doc(effect='absent'), None)
        llm = FakeLLM(decide_script=[
            {'action': 'tool', 'tool': 'propose_change', 'args': {
                'operation': 'replace', 'target': 'p1', 'before': BEFORE,
                'after': '协助完成接口测试与结果记录。',
                'reason': '梳理职责与动作',
                'source_refs': [{
                    'source_id': 'resume', 'source_revision': 1,
                    'quote': '接口测试',
                }],
                'gaps': [],
            }},
            {'action': 'finish', 'outcome': 'completed', 'summary': '建议已生成。'},
        ])
        result = asyncio.run(run_loop(
            run_id='r_absent', session_id=policy.SESSION, repo=repo,
            store=store, llm=llm, focus_experience_id='p1',
        ))
        self.assertEqual(result.outcome, 'waiting_user')
        self.assertEqual(len(repo.suggestions), 1)
        self.assertFalse(any(issue.kind == 'missing_effect' for issue in repo.issues))
```

把测试文件顶部的 `from app.agent.ports import DraftBlock, SessionContext, SessionInfo` 改为：

```python
from app.agent.ports import DraftBlock, SessionContext, SessionInfo, SourceText
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_effect_gate -q
```

Expected: FAIL —— `EFFECT_EXHAUSTED_TEXT` 不存在、`absent` 被旧 Guard 拦截、数字误判、fallback 仍返回旧结果。

- [ ] **Step 3: 修改 Guard 常量与数字辅助**

把 `backend/app/agent/guard.py` 的 `MISSING_EFFECT_TEXT` 常量替换为：

```python
EFFECT_EXHAUSTED_TEXT = "效果已经问过两次，请基于职责、动作和方法照实改写。"
_EFFECT_CLAIMS = (
    "提升", "提高", "降低", "减少", "节省", "加快", "缩短",
    "改善", "避免", "替代", "方便", "效率",
)
```

在 `_CN_NUMBER` 定义之后新增：

```python
def _cn_numbers(text: str):
    for match in _CN_NUMBER.finditer(text):
        tail = text[match.end() : match.end() + 1]
        if match.group() == "一" and tail in ("个", "次", "人"):
            continue
        yield match.group()
```

- [ ] **Step 4: 修改提问规则**

把 `_question_violation` 中的：

```python
        if step.slot is not None:
            if step.slot in state.focus_doc.declined:
                return Violation("question_declined", "该维度已被用户拒绝")
```

替换为：

```python
        if step.slot is not None:
            exempt_effect_followup = (
                step.slot == "effect"
                and effect_state(state.focus_doc) == "followup"
            )
            if step.slot in state.focus_doc.declined and not exempt_effect_followup:
                return Violation("question_declined", "该维度已被用户拒绝")
```

把 `AskStep` 分支中的：

```python
            if effect_questions >= 2:
                return GuardResult(
                    violation=Violation(
                        "missing_effect", MISSING_EFFECT_TEXT, False
                    )
                )
```

替换为：

```python
            if effect_questions >= 2:
                return GuardResult(
                    violation=Violation(
                        "effect_exhausted", EFFECT_EXHAUSTED_TEXT, True
                    )
                )
```

- [ ] **Step 5: 修改改写门控**

把 `propose_change` 分支中从 `effect = effect_state(...)` 到 `_evidence_gate` 之前的整段替换为：

```python
            effect = effect_state(state.focus_doc)
            if effect == "followup":
                return GuardResult(
                    violation=Violation(
                        "ask_effect_followup",
                        "用户只说明了使用情况，请再追问一次具体变化；仍没有就照实改写。",
                        True,
                    )
                )
            if effect == "missing":
                return GuardResult(
                    violation=Violation(
                        "ask_effect",
                        "改写前必须先问这段经历带来的实际效果，不能把“有人使用/上线”当成效果。",
                        True,
                    )
                )
            if effect == "absent":
                anchor = (
                    args.before
                    if args.before is not None
                    else state.focus_block_markdown
                )
                claims = [
                    word
                    for word in _EFFECT_CLAIMS
                    if word in args.after and word not in anchor
                ]
                if claims:
                    return GuardResult(
                        violation=Violation(
                            "effect_claim_without_evidence",
                            "用户已说明没有可确认的实际效果，改写只写职责、动作和方法，不要写效果结论。",
                            True,
                        )
                    )
```

- [ ] **Step 6: 修改数字校验**

把 `_evidence_gate` 中的：

```python
    for number in _NUMBER.findall(args.after) + _CN_NUMBER.findall(args.after):
        if number not in compact:
            return GuardResult(
                violation=Violation(
                    "unsupported_number", f"数字 {number} 没有依据", False
                )
            )
```

替换为：

```python
    for number in _NUMBER.findall(args.after) + list(_cn_numbers(args.after)):
        if number not in compact:
            return GuardResult(
                violation=Violation(
                    "unsupported_number", f"数字 {number} 没有依据", True
                )
            )
```

- [ ] **Step 7: 修改 loop，删除“缺效果就结束”**

在 `backend/app/agent/loop.py` 的 guard 导入中删除 `MISSING_EFFECT_TEXT`，导入块变为：

```python
from app.agent.guard import (
    MAX_ROUNDS,
    MAX_STEPS,
    GuardState,
    call_signature,
    check,
)
```

把 `_fallback_question` 中的：

```python
    effect = effect_state(doc)
    if effect == "blocked":
        return None, None
    if effect == "followup":
        return f"{_EFFECT_FOLLOWUP}？", "effect"
    active_slots = {fact.slot for fact in doc.facts if fact.status == "active"}
    for slot in _SLOT_ORDER:
        if slot in doc.declined or slot in active_slots:
            continue
```

替换为：

```python
    effect = effect_state(doc)
    if effect == "followup":
        return f"{_EFFECT_FOLLOWUP}？", "effect"
    active_slots = {fact.slot for fact in doc.facts if fact.status == "active"}
    for slot in _SLOT_ORDER:
        if slot == "effect" and effect == "absent":
            continue
        if slot in doc.declined or slot in active_slots:
            continue
```

把 `fallback()` 中的：

```python
        if text is None:
            if effect_state(focus_doc) == "blocked":
                return limited("missing_effect", MISSING_EFFECT_TEXT)
            focus_doc.last_run_outcome = "limited"
            persist(focus_doc)
            return finish_result("limited")
```

替换为：

```python
        if text is None:
            focus_doc.last_run_outcome = "limited"
            persist(focus_doc)
            return finish_result("limited")
```

删除 `FinishStep` 分支中的整段：

```python
            if (
                outcome == "limited"
                and focus_doc is not None
                and effect_state(focus_doc) == "blocked"
            ):
                repo.save_issues(
                    session_id,
                    [
                        Issue(
                            kind="missing_effect",
                            summary=MISSING_EFFECT_TEXT,
                            experience_id=focus_id,
                        )
                    ],
                )
```

- [ ] **Step 8: 运行 test_effect_gate 确认通过**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_effect_gate -q
```

Expected: PASS（新增与改写用例全部通过）。

---

