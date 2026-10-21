# 本机业务 API（A-T06）

基础地址 `http://127.0.0.1:3001`，前端开发时经 `http://127.0.0.1:5173/api` 代理访问。
FastAPI 的 `/docs` 和 `/openapi.json` 展示已登记接口；材料上传的两种正文格式以本文为准。
当前没有 Agent／预算路由、接受建议、手改或整体替换接口。

## 接口清单

| 方法与路径 | 输入／结果 |
|---|---|
| GET `/api/health` | 保留 `{status, model_configured}` 状态格式，不请求模型 |
| GET `/api/sessions` | 会话列表 |
| POST `/api/sessions` | direction、requestId → 会话 |
| GET `/api/sessions/{id}` | 恢复会话、全部消息、材料清单、当前稿、历史建议及问题 |
| DELETE `/api/sessions/{id}` | requestId、expectedStateRevision、confirmed=true → 关联删除结果 |
| GET `/api/sessions/{id}/messages` | 消息列表，含 message／supplement／correction／skip 的 kind |
| POST `/api/sessions/{id}/messages` | content、requestId、expectedStateRevision、可选 kind → 消息、对应来源及新版本 |
| GET `/api/sessions/{id}/materials` | 原材料清单，不含原文件字节 |
| POST `/api/sessions/{id}/materials` | 粘贴 JSON 或原始文件二进制，见下文 |
| GET `/api/sessions/{id}/materials/{materialId}` | 材料元信息及提取来源（可供用户核对，包含待澄清／历史来源） |
| GET `/api/sessions/{id}/materials/{materialId}/file` | 下载原附件，原始二进制，不用 JSON 包装 |
| GET `/api/sessions/{id}/draft` | 当前稿版本与有序正文块 |
| GET `/api/sessions/{id}/sources/{sourceId}/revisions/{revision}` | 用户回看确切来源版本，含 historical 标记 |
| GET `/api/sessions/{id}/suggestions/{suggestionId}` | 查看对应历史建议及引用 |

除 health 和附件下载外，成功统一为 `{ "ok": true, "data": ... }`；错误为：

```json
{
  "ok": false,
  "error": {
    "code": "stale_state",
    "message": "会话已更新，请读取最新状态。",
    "recoverable": true
  }
}
```

创建类操作返回 201，读取／删除返回 200。参数无效 422，目标不存在 404，作用域／跨站拒绝 403，版本或请求冲突 409，不支持格式 415，请求过大 413，存储失败 503，未处理内部错误 500。不存在或尚未实现的路由不会假称成功。
字段验证错误可含 error.fields（字段位置与错误类型），不回显原输入、密钥或底层堆栈。

## 消息保存与幂等

```json
{
  "content": "我整理了 8 条反馈，归纳为 5 项需求。",
  "requestId": "message-001",
  "expectedStateRevision": 1,
  "kind": "supplement"
}
```

- requestId 为 1～128 位字母／数字开头的字母数字、点、横线、下划线或冒号；建议前端每次真实操作生成独立 UUID。
- role 固定由该入口写为 user，不接受 role／runId／SQL／path 等额外字段。
- expectedStateRevision 为正整数；布尔值、数字字符串和超范围整数拒绝。
- 非 skip 消息同步建立可引用的用户原话 SourceBlock；这不认证原话为事实，不将要求／假设自动认定为经历。Agent 仍须检查语义。skip 不建立个人依据。
- 一次用户操作内的消息、来源、版本与去重结果在同一 SQLite 事务提交；多个入库子步骤只推进一次用户上下文版本。返回的新版本应按实际值使用，不自行计算。
- 去重窗口目前为 24 小时。创建请求在公共创建范围去重；其他操作在各自会话范围去重，同一范围内同 ID 不得更换动作或内容。
- 成功重试先返回原结果，再考虑新操作的版本检查；响应丢失后保留原 requestId、原正文与原预期版本重试，不产生第二条消息。重放返回的是原操作结果，不保证其中版本等于后来最新会话版本。
- request_log 持久保存去重结果，进程重启后仍可重试；到期记录在后续写入时清理，没有后台定时任务。超过窗口后应读取最新状态再决定下一次独立操作。

## 材料上传

### 粘贴 JSON

POST `/api/sessions/{id}/materials`，`Content-Type: application/json`：

```json
{
  "requestId": "material-001",
  "expectedStateRevision": 2,
  "name": "简历.md",
  "text": "# 简历\n参与校园报名工具开发，负责需求整理。",
  "role": "resume"
}
```

name 默认“粘贴材料.md”；role 默认 unknown，可为 resume／jd／supplement。role 仅是材料用途或用户明确纠正，不是系统身份；前端不需要增加用户必经分类步骤。

UTF-8 文本保存为原附件及来源块。明确 resume 且当前无稿时，按原文形成初始 Markdown，不做模型改写；明确补充或 JD 不自动改当前稿。再次提交整份 resume 时保存为待澄清，不覆盖旧稿、不自动混入依据；unknown 保持待澄清，等待后续 Agent 材料理解能力。

### 原始文件二进制

同一路径，直接用文件字节作为 body，**当前不使用 multipart/form-data，也不要求 Base64**。query 参数为 name、requestId、expectedStateRevision、可选 role；同一参数不得重复。例如：

```text
POST /api/sessions/{id}/materials?name=resume.pdf&requestId=file-001&expectedStateRevision=3
Content-Type: application/pdf

[文件原始字节]
```

文件名不得含路径分隔符或控制字符，原附件以 BLOB 入库，不按传入名称写磁盘。支持 .md、.docx、PDF、PNG/JPEG；旧 .doc 提示转换。校验声明 MIME／扩展名与文件头或 DOCX 容器。**文件头通过不表示完整格式解析通过**：仅 MD 当前可提取文本；Word／PDF／图片保存后为 pending，不生成假识别结果。

当前初始边界：JSON 请求最多 1 MiB，原文件最多 10 MiB，文本最多 50000 字符。属于待多格式样例校准的实现上限，不宣称适合所有实际简历；后续限制调整需同步文档。流式接收也检查累计大小，不能只相信 Content-Length。

成功结果含 material、sources、draftCreated、needsClarification、stateRevision、draftRevision。来源 pending 时不会进入 A-T05 个人依据查询；用户材料查看入口与模型事实检索入口分别使用，不得互换。

## 删除与本地访问范围

DELETE 请求需 confirmed=true 和当前版本。数据库同时使旧运行失效、级联删除会话业务数据；A-T06 只实现持久化逻辑失效，实际模型网络请求的取消由 B 后续接入。用户原文件和独立费用账本不改动。

删除触发器清除该会话已有请求缓存的正文及指纹，仅保留窗口内的操作编号／范围等最小拒绝重放标记；已成功删除的同一请求可取回删除结果，旧创建／消息重试不会恢复已删会话。完整复盘内容不留在去重缓存中。

只接受本机 Host 与工作台 Origin（localhost／127.0.0.1 的 5173、3001）；显式跨站请求拒绝。命令行无 Origin 的本机请求可用。这不是账号鉴权，同一本机后端仍无多人身份隔离。Agent 工具不能调用这些用户 HTTP 入口；未来付费 Agent 入口仍需 B 配合接入既定本地请求令牌及预算控制。

## 验证

在 backend 目录：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

接口检查通过实际 FastAPI／ASGI 应用和隔离 SQLite 文件执行，无真实模型或外部网络；包含保存、重启重试、并发重试、回滚、材料回看、跨会话拒绝与删除清理。前端业务交互在 A-T08 接入，当前页面按钮仍禁用。
