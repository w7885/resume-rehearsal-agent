# 历历在练

当前已完成 A-T03 基础工程和 A-T04 数据库结构：React／TypeScript 三栏工作台，Python／FastAPI 状态接口，以及 SQLite 初始化、只读检查和结构测试。页面业务保存、材料查询工具与 Agent 尚未接入。

## 本机启动

环境：Node.js 24、Python 3.13。前后端使用两个终端；分别进入对应目录。

### 后端（PowerShell）

```powershell
cd backend
# 首次准备环境时执行；已有 .venv 不必重复创建
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 3001 --workers 1
```

### 前端（PowerShell）

```powershell
cd frontend
npm ci
npm run dev
```

打开 http://127.0.0.1:5173 。前端固定使用 5173，将 `/api` 转发到本机 3001，端口被占用时明确报错，不自动换端口。停止时在对应终端按 Ctrl+C。

## 当前可以检查什么

- 历史会话／对话材料／当前稿三栏基础布局。
- 页面自动获取真实后端状态，支持重新检查、超时与断开提示。
- 求职方向、简历文字、选填 JD 的临时输入。它们只在页面内存中，刷新即清空，不会发送给后端或模型。
- 无密钥启动：`GET /api/health` 返回服务状态及 `model_configured` 布尔值，不返回密钥，不调用模型。

会话业务接口、文件导入、Agent 分析、建议确认、Markdown 成稿及导出尚未实现，相关按钮明确禁用。SQLite 结构及隔离构造数据检查已通过，不代表页面会保存输入或模型效果已验证。

## 配置

后端当前只读取进程环境变量 `DEEPSEEK_API_KEY`；缺失时模型配置状态为 false。`backend/.env.example` 仅说明配置项，复制为 `.env` 不会自动生效。当前工程没有真实模型调用入口，不需要配置密钥即可检查页面。

`.venv`、`.env`、缓存、`.local`、前端依赖和构建目录忽略提交；依赖清单／锁文件和源码可纳入版本管理。学习、分工及计划文档仅本地保留。

## 基础检查

```powershell
cd frontend
npm run typecheck
npm run build
```

后端启动后访问 http://127.0.0.1:3001/api/health ，或通过前端 http://127.0.0.1:5173/api/health 检查代理。配置状态仅表示是否提供非空密钥，不验证凭证、余额或数据库保存能力。

## SQLite 初始化与检查

在 `backend` 目录执行以下命令。后端启动时也会自动初始化／核对数据库结构，失败会阻止本次启动，不删除数据库重建。

```powershell
# 首次建立结构；再次执行保留原数据
.\.venv\Scripts\python.exe -m app.db init

# 只读检查，不创建缺失文件或自动修复
.\.venv\Scripts\python.exe -m app.db check

# 运行独立构造数据检查，无模型请求
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

默认业务文件是 `backend/.local/resume-agent.sqlite`，根据源码位置确定，不随运行目录漂移。初始化正式文件只建立空结构，不装入测试履历。CLI 的 `--path` 只供开发者选择检查／初始化目标，不暴露为模型工具参数。

### 表关系与数据范围

| 表 | 用途 |
|---|---|
| `schema_version` | 已成功应用的结构升级版本 |
| `sessions` | 会话、求职方向、当前稿版本和当前简历代次 |
| `attachments`、`messages` | 原文件 BLOB 与对话；标明当前／历史用途及简历代次 |
| `source_blocks` | 带位置和不可变版本的材料原文 |
| `draft_blocks`、`draft_sources` | 当前／历史正文快照及对应引用 |
| `suggestions`、`suggestion_sources` | 建议状态、目标正文版本及确切来源 |
| `issues`、`issue_sources`、`issue_blocks` | 诊断／冲突、依据及受影响正文 |
| `runs`、`run_events` | 运行代号、输入版本、状态与有序事件 |
| `draft_replacements` | 明确替换操作的前后正文和材料范围，供追溯 |

```text
sessions
├── attachments / messages → source_blocks
├── draft_blocks → draft_sources → 确切来源版本
├── suggestions → suggestion_sources → 确切来源版本
├── issues → issue_sources / issue_blocks
├── runs → run_events
└── draft_replacements
```

- 会话级数据通过外键关联，整会话删除时级联清理，不删除电脑原始文件，也不管理独立费用账本。
- 目标段落和引用使用包含 session_id 的组合外键，拒绝“编号存在但属于另一会话”的关联。部分引用在事务提交时检查，事务失败会回滚整组变化。
- 已有来源文本和正文快照不原地覆盖；修正创建新版本，旧引用仍能回看。当前标记与有效范围仍可按用户操作更新。
- 每个连接开启外键；同会话最多一个 running 运行。未知版本、缺表、缺索引或损坏文件明确失败，不清库、不自动重建。
- 版本迁移 SQL 放在 `backend/app/migrations/`；已发布的迁移不应随意改写，后续变更追加新迁移。DDL 与版本记录在同一短事务内提交。
- 数据库约束仅保证结构、类型和关联。引用原文是否包含 quote、来源是否当前有效、是否支持改写、用户是否授权等仍需 A-T05／A-T07 的业务层检查；数据库不认证经历真实性。

测试使用内存库及 `backend/.local/schema-tests/` 下的独立临时文件；关闭重开、重复初始化、升级回滚及跨会话错误均不在正式业务文件中试验。临时升级文件只属于测试，不是实际发布的版本 2。
