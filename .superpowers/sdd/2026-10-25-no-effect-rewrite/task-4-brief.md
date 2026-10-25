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

### Task 4: 两轮无效果的 HTTP 级用例


**Files:**
- Test: `backend/tests/test_proactive_flow.py`

**Interfaces:**
- Consumes: `ProactiveClient`、`ready()`、`turn()`、`pending()` 现有辅助。
- Produces: 模式 `noeffect`，验证“问两次 → 照实改写 → 有建议、无效果措辞”。

- [ ] **Step 1: 给 `ProactiveClient.decide` 增加 `noeffect` 分支**

在 `test_proactive_flow.py` 的 `ProactiveClient.decide` 中，把 `if fresh:` 提问块之后、`ctx = self.runtime.repo.get_session_context(...)` 之前插入：

```python
        if mode == 'noeffect':
            if '这段经历上线或交付后' not in kwargs['user']:
                return SimpleNamespace(data={'action': 'ask_user', 'message': '这段经历上线或交付后，带来了哪些实际变化？', 'slot': 'effect', 'related_source_ids': [], 'rationale': '确认实际效果'})
            if '没上线也可以' not in kwargs['user']:
                return SimpleNamespace(data={'action': 'ask_user', 'message': '没上线也可以，这件事有没有让流程或结果发生变化？', 'slot': 'effect', 'related_source_ids': [], 'rationale': '确认定性变化'})
            if not self.proposed:
                self.proposed = True
                ctx = self.runtime.repo.get_session_context(self.runtime.context)
                block = next(b for b in ctx['draft']['blocks'] if b['blockId'] == block_id)
                ref = block['sourceRefs'][0]
                return SimpleNamespace(data={'action': 'tool', 'tool': 'propose_change', 'args': {
                    'operation': 'replace', 'target': block_id, 'before': block['markdown'],
                    'after': block['markdown'].replace('参与', '协助'), 'reason': '梳理原有职责', 'gaps': [],
                    'source_refs': [{'source_id': ref['sourceId'], 'source_revision': ref['sourceRevision'], 'quote': ref['quote']}]}})
            return SimpleNamespace(data={'action': 'finish', 'outcome': 'completed', 'summary': '建议已生成。'})
```

- [ ] **Step 2: 追加测试**

在 `ProactiveFlowTests` 中追加：

```python
    def test_no_effect_still_produces_an_honest_suggestion(self):
        session, host, ids = self.ready()
        host.modes[ids[0]] = 'noeffect'
        self.turn(session, '继续', 'first')
        self.turn(session, '我负责测试用例执行与结果记录。', 'resp')
        self.turn(session, '没有上线，只在本地运行。', 'effect-1')
        self.assertEqual(self.pending(session), [])
        self.turn(session, '确实没有别的变化。', 'effect-2')
        pending = self.pending(session)
        self.assertEqual([p['target']['blockId'] for p in pending], ids[:1])
        self.assertIn('协助项目一', pending[0]['after'])
        self.assertNotIn('提升', pending[0]['after'])
        self.assertNotIn('效率', pending[0]['after'])
```

- [ ] **Step 3: 运行**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_proactive_flow.ProactiveFlowTests.test_no_effect_still_produces_an_honest_suggestion -q
```

Expected: PASS。若失败，优先检查：第二个问题是否被 `question_declined` 拦截（Task 2 Step 4），以及 `effect_state` 是否已是 `absent`（Task 1）。

- [ ] **Step 4: 运行相邻回归**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_proactive_flow tests.test_agent_integration tests.test_segment_guard tests.test_personal_evidence -q
```

Expected: PASS。

---

