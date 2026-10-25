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

### Task 5: 全量验证、重启与人工验收


**Files:** 无代码改动。

- [ ] **Step 1: 全量测试**

```bash
cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=".;tests" ./.venv/Scripts/python.exe -m unittest discover -s tests -q
```

Expected: PASS。基线 378 项，本计划新增约 10 项，最终约 388 项。

- [ ] **Step 2: 重启后端（必须，代码不会热加载）**

先查端口 3001 的 PID：

```bash
/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command "Get-NetTCPConnection -State Listen | Where-Object { \$_.LocalPort -eq 3001 } | Select-Object OwningProcess"
```

用返回的 PID 停止整个进程树（Git Bash 下必须加 `MSYS_NO_PATHCONV=1`，否则 `/PID` 会被改写）：

```bash
MSYS_NO_PATHCONV=1 /c/Windows/System32/taskkill.exe /PID <上一步的PID> /T /F
```

再用同一命令重启（不要加 `--reload`，后台运行）：

```bash
cd backend && set -a && source .env && set +a && ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 3001 --workers 1 > .local/backend.log 2>&1
```

检查：

```bash
curl -s -m 5 http://127.0.0.1:3001/api/health
```

Expected: `{"status":"ok","model_configured":true}`。

- [ ] **Step 3: 人工验收（按规格 §11）**

1. 新建会话，上传含“姓名/联系方式 + 教育 + 项目 + 实习”的简历，回答目标岗位。
2. 第 1 段效果问题回答“没有上线，只在本地运行” → 只出现一次跟进问题，不结束本段。
3. 回答“确实没有别的效果” → 出现建议卡；建议只写职责/动作/方法，不含“提升/减少/效率/方便”等效果结论，不含新数字。
4. 第 2 段回答“方便记录信息” → 出现建议卡，不再报“数字 一 没有依据”。
5. 非工科抽查：上传教师/运营类简历，技术提问显示为“工具、方法或资源”，改写中无“技术栈/架构/性能”等无关词。
6. 抽查数据库：

```bash
cd backend && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe - <<'PY'
import sqlite3
c = sqlite3.connect('file:.local/resume-agent.sqlite?mode=ro', uri=True)
sid = c.execute(
    "select session_id from sessions order by updated_at desc limit 1"
).fetchone()[0]
print(sid)
print(list(c.execute(
    "select kind, summary from issues where session_id=? order by rowid", (sid,)
)))
PY
```

Expected: 没有 `missing_effect` 类型的新 issue；第 1 段没有编造的 `effect` 事实。
