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

### Task 1: 效果状态增加 `absent`，交付状态不算拒答


**Files:**
- Modify: `backend/app/agent/memory.py:173-190`
- Modify: `backend/app/agent/router.py:36-53`
- Test: `backend/tests/test_effect_gate.py`

**Interfaces:**
- Consumes: `ExperienceDoc` 现有 `facts` / `questions` / `declined`。
- Produces:
  - `effect_state(doc: ExperienceDoc) -> Literal["confirmed", "followup", "absent", "missing"]`
  - `router._is_decline(text: str) -> bool`（“没上线/只在本地”返回 False）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_effect_gate.py` 末尾、`if __name__ == '__main__':` 之前追加：

```python
class EffectStateTests(unittest.TestCase):
    def doc(self, *outcomes, status='active'):
        doc = ExperienceDoc(
            experience_id='p1', seq=1, kind='project', title='构造项目',
            raw_text=BEFORE, block_id='p1', block_revision=1,
        )
        for index, outcome in enumerate(outcomes, start=1):
            doc.questions.append(Question(
                id=f'q{index}', slot='effect', text='带来了哪些实际变化？',
                fingerprint=f'f{index}', outcome=outcome,
            ))
        if status is not None:
            doc.facts.append(Fact(
                id='f1', slot='effect', statement='接口测试效率提高',
                status=status, created_at='2026-01-01T00:00:00Z',
            ))
        return doc

    def test_no_effect_question_is_missing(self):
        self.assertEqual(effect_state(self.doc(status=None)), 'missing')

    def test_one_answer_is_followup(self):
        self.assertEqual(effect_state(self.doc('answered', status=None)), 'followup')

    def test_one_decline_is_followup(self):
        self.assertEqual(effect_state(self.doc('declined', status=None)), 'followup')

    def test_two_answers_are_absent(self):
        self.assertEqual(
            effect_state(self.doc('answered', 'answered', status=None)), 'absent'
        )

    def test_answer_plus_decline_is_absent(self):
        self.assertEqual(
            effect_state(self.doc('answered', 'declined', status=None)), 'absent'
        )

    def test_active_fact_is_confirmed(self):
        self.assertEqual(effect_state(self.doc()), 'confirmed')

    def test_superseded_fact_without_questions_is_missing(self):
        self.assertEqual(effect_state(self.doc(status='superseded')), 'missing')

    def test_superseded_fact_with_two_questions_is_absent(self):
        self.assertEqual(
            effect_state(self.doc('answered', 'answered', status='superseded')),
            'absent',
        )


class DeliveryDeclineTests(unittest.TestCase):
    def test_no_deployment_is_not_a_decline(self):
        self.assertFalse(_is_decline('没有上线，只在本地运行'))

    def test_local_only_is_not_a_decline(self):
        self.assertFalse(_is_decline('只在本地跑过'))

    def test_no_effect_is_still_a_decline(self):
        self.assertTrue(_is_decline('没有实际效果'))
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_effect_gate.EffectStateTests tests.test_effect_gate.DeliveryDeclineTests -q
```

Expected: FAIL —— `followup`/`absent` 断言拿到 `blocked`，两个交付状态测试返回 True。

- [ ] **Step 3: 修改 `memory.effect_state`**

把 `backend/app/agent/memory.py:173-190` 整体替换为：

```python
def effect_state(doc: ExperienceDoc) -> Literal["confirmed", "followup", "absent", "missing"]:
    if any(
        fact.slot == "effect" and fact.status == "active" for fact in doc.facts
    ):
        return "confirmed"
    settled = [
        question
        for question in doc.questions
        if question.slot == "effect"
        and question.outcome in ("answered", "declined")
    ]
    if len(settled) >= 2:
        return "absent"
    if len(settled) == 1:
        return "followup"
    return "missing"
```

- [ ] **Step 4: 修改 `router` 拒答识别**

在 `backend/app/agent/router.py` 的 `_EFFECT_MARKERS` 之后新增：

```python
_DELIVERY_MARKERS = (
    "没有上线", "没上线", "未上线", "没有部署", "没部署",
    "只在本地", "本地运行", "单机",
)
```

并把 `_is_decline` 中的：

```python
    if any(marker in value for marker in _EFFECT_MARKERS):
```

改为：

```python
    if any(marker in value for marker in _EFFECT_MARKERS + _DELIVERY_MARKERS):
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest tests.test_effect_gate.EffectStateTests tests.test_effect_gate.DeliveryDeclineTests -q
```

Expected: PASS（11 项）。

---

