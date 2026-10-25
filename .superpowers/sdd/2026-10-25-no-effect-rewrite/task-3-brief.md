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

### Task 3: 上下文提示、非工科文案与提示词


**Files:**
- Modify: `backend/app/agent/context.py:1-14, 49-59, 133-143`
- Modify: `backend/app/agent/loop.py:44-53`
- Modify: `backend/app/agent/prompts/decide.md:18-28, 33-37, 42, 51`
- Test: `backend/tests/test_agent_session_policy.py`

**Interfaces:**
- Consumes: Task 1 的 `effect_state(...)`。
- Produces: 上下文小节 `## 实际效果`；面向用户的技术提问改为“工具、方法或资源”。

- [ ] **Step 1: 写上下文失败测试**

在 `backend/tests/test_agent_session_policy.py` 的 `test_context_renders_brake_section` 之后追加：

```python
    def test_context_explains_absent_effect(self):
        from app.agent.context import assemble_context

        repo = make_repo()
        ctx = repo.get_session_context(SESSION)
        doc = ExperienceDoc(
            experience_id="exp_01",
            seq=1,
            kind="project",
            title="校园二手交易平台",
            raw_text=MARKDOWN,
            block_id="exp_01",
            block_revision=1,
        )
        doc.questions.extend([
            Question(
                id='q1', slot='effect', text='带来了哪些实际变化？',
                fingerprint='f1', outcome='answered',
            ),
            Question(
                id='q2', slot='effect', text='还有别的变化吗？',
                fingerprint='f2', outcome='declined',
            ),
        ])
        text = assemble_context(
            ctx, [doc], {"exp_01": "interviewing"}, "exp_01"
        )
        self.assertIn("用户已说明没有可确认的实际效果", text)
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_agent_session_policy.BrakeTests.test_context_explains_absent_effect -q
```

Expected: FAIL（文本中没有该提示）。

- [ ] **Step 3: 修改 context.py**

导入改为：

```python
from app.agent.memory import ExperienceDoc, Slot, effect_state
```

在 `_questions_block` 之后新增：

```python
def _effect_block(doc: ExperienceDoc) -> str:
    state = effect_state(doc)
    if state == "confirmed":
        statement = next(
            fact.statement
            for fact in doc.facts
            if fact.slot == "effect" and fact.status == "active"
        )
        return f"已确认：{_clip(statement, 200)}"
    if state == "followup":
        return "已追问 1 次，仍待具体变化（最多再问 1 次）"
    if state == "absent":
        return "用户已说明没有可确认的实际效果：照实改写，不写效果类结论"
    return "尚未询问"
```

把当前焦点小节中的：

```python
            f"## 已确认事实\n{_facts_block(focus)}\n\n"
            f"## 问题与缺口\n{_questions_block(focus)}\n\n"
```

改为：

```python
            f"## 已确认事实\n{_facts_block(focus)}\n\n"
            f"## 实际效果\n{_effect_block(focus)}\n\n"
            f"## 问题与缺口\n{_questions_block(focus)}\n\n"
```

- [ ] **Step 4: 修改 loop 的技术提问文案**

把 `backend/app/agent/loop.py` 的 `_PHRASES["tech"]` 改为：

```python
    "tech": "这段经历用到了哪些工具、方法或资源",
```

- [ ] **Step 5: 修改 decide.md**

按以下精确替换执行（逐段替换，不要整篇重写）：

`## 好经历的标准` 第一段：

```markdown
一段改写后的项目/实习经历，应让读者看出：背景与问题（S）→ 个人职责与目标（T）→ 具体行动与方法决策（A）→ 可量化结果（R）。
```

清单第 3 条：

```markdown
3. 动作具体可验证：「加索引」「拆分订单表」「重新拆分客户分组」「固定回访渠道」可以，「优化系统」「提升性能」不行。
```

清单第 4 条：

```markdown
4. 实际效果：优先业务/结果指标。技术岗可写延迟、准确率、召回率、覆盖率；非技术岗写通过率、满意度、投诉率、复购率、排课或审批步骤等。没有数字时，定性效果也可以，但必须是变化：替代了什么、减少了哪一步、避免了什么错误、反馈中描述的改善。上线、被采用、被使用、通过验收、功能完成、只在本地运行都不算效果。没有依据的数字不写。
```

清单第 6、7 条：

```markdown
6. 方法与工具重点与目标岗位对齐：技术岗按技术栈突出关键点；非技术岗突出与岗位相关的方法、流程或工具（如教学中的分层练习、运营中的活动复盘）。
7. 不堆砌无关内容：只写真正用到的工具和方法，不为了凑关键词塞技术栈。
```

`## 决策约束` 第 2 条中的：

```markdown
业务背景（`context`）和技术深度（`tech`）只在原文完全看不出、且还有提问预算时作为加分项询问
```

改为：

```markdown
业务背景（`context`）和工具/方法细节（`tech`）只在原文完全看不出、且还有提问预算时作为加分项询问
```

把“生成 `propose_change` 前……”这一整条替换为：

```markdown
- 生成 `propose_change` 前，必须已确认职责、至少一个具体动作，并且效果状态是“已确认”或“已追问两次仍没有”。用户只回答使用、上线、验收、有人用、邀请同学使用、功能完成时，只能再追问一次实际变化；第二次仍没有或用户明确说没有，直接照实改写，不写效果结论、不编数字，不得再追问，也不得因缺效果 `finish(limited)`。使用/交付事实不是效果，不能记成 `effect`；需要记录时用 `result` 或 `context`。动作同样缺失且用户答不上来时，不要硬写，`finish` 用 `limited`。
```

把 decline 那一条替换为：

```markdown
- 用户回答含"不记得/不确定/应该吧/大概/可能/不知道"，视为该维度 decline；但“没有上线/没部署/只在本地运行”是交付状态，不算拒答。效果维度被拒答时也只再追问一次定性变化，之后照实改写；其他维度 decline 不阻塞，继续按已有事实改写，并把缺失写入 `gaps`。此前的 effect decline 不阻止用户后续主动补充效果；一旦出现 active 的 `effect` 事实，按正常流程改写。
```

问题规范那一条中的：

```markdown
问效果时问“带来了哪些实际变化”，不要问“有没有人用”。
```

改为：

```markdown
问效果时问“带来了哪些实际变化”；用户说没上线时问“没上线也可以，这件事有没有让流程或结果发生变化”，不要问“有没有人用”。
```

`after` 那一条的结尾：

```markdown
实际效果放在后面并优先业务价值；技术重点按目标岗位倾斜，不堆砌无关技术。
```

改为：

```markdown
实际效果放在后面并优先业务价值；方法与工具重点按目标岗位倾斜，不堆砌无关内容。没有确认实际效果时，只写职责、动作与必要背景，不写“提升了/减少了/更方便”等效果结论。
```

- [ ] **Step 6: 运行上下文测试与全量回归**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_agent_session_policy tests.test_effect_gate -q
```

Expected: PASS。

---

