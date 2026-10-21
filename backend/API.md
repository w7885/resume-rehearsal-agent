# 本机业务 API（A-T06／A-T07／A-T08）

基础地址 `http://127.0.0.1:3001`，前端开发时经 `http://127.0.0.1:5173/api` 代理访问。
FastAPI 的 `/docs` 和 `/openapi.json` 展示已登记接口；材料上传的两种正文格式以本文为准。
已提供建议确认、手改与明确整体替换接口，并接入本机页面闭环；没有真实 Agent／预算路由。模拟示例与个人材料会话分开，页面不会对个人履历套用预设建议。

## 接口清单

| 方法与路径 | 输入／结果 |
|---|---|
| GET `/api/health` | 保留 `{status, model_configured}` 状态格式，不请求模型 |
| GET `/api/sessions` | 会话列表 |
| POST `/api/sessions` | direction、requestId → 会话 |
| POST `/api/demo/sessions` | requestId → 独立构造材料会话及初始稿，mode=mock |
| POST `/api/sessions/{id}/mock-analysis` | requestId、expectedStateRevision → 固定构造流程的建议、问题及模拟事件；仅限未改变的模拟示例 |
| GET `/api/sessions/{id}` | 恢复会话、全部消息、材料清单、当前稿、历史建议及问题 |
| DELETE `/api/sessions/{id}` | requestId、expectedStateRevision、confirmed=true → 关联删除结果 |
| GET `/api/sessions/{id}/messages` | 消息列表，含 message／supplement／correction／skip 的 kind |
| POST `/api/sessions/{id}/messages` | content、requestId、expectedStateRevision、可选 kind → 消息、对应来源及新版本 |
| GET `/api/sessions/{id}/materials` | 原材料清单，不含原文件字节 |
| POST `/api/sessions/{id}/materials` | 粘贴 JSON 或原始文件二进制，见下文 |
| GET `/api/sessions/{id}/materials/{materialId}` | 材料元信息及提取来源（可供用户核对，包含待澄清／历史来源） |
| GET `/api/sessions/{id}/materials/{materialId}/file` | 下载原附件，原始二进制，不用 JSON 包装 |
| GET `/api/sessions/{id}/draft` | 当前稿版本与有序正文块 |
| PUT `/api/sessions/{id}/draft` | markdown、requestId、expectedStateRevision → 手改后的当前稿 |
| POST `/api/sessions/{id}/suggestions/{suggestionId}/decision` | decision、expectedBlockRevision、feedback、requestId、expectedStateRevision → 用户决定及当前稿 |
| POST `/api/sessions/{id}/draft/replacement` | 完整新版材料及明确用户授权，见下文 |
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

UTF-8 文本保存为原附件及来源块。明确 resume 且从未导入过稿件时，按原文形成初始 Markdown，不做模型改写；明确补充或 JD 不自动改当前稿。再次提交整份 resume 时保存为待澄清，不覆盖旧稿、不自动混入依据；即使用户已清空当前正文，只要仍有历史稿件，也必须走明确整体替换，不重新当作首份导入。unknown 保持待澄清，等待后续 Agent 材料理解能力。

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

## 建议确认与正文编辑（A-T07）

建议由 B 通过受限 Python 工具保存为 pending；下列接口只能由用户操作路径调用，模型工具白名单未开放这些写入。

```json
{
  "requestId": "decision-001",
  "expectedStateRevision": 4,
  "expectedBlockRevision": 1,
  "decision": "accept",
  "feedback": ""
}
```

- decision 为 accept／reject／revise；revise 必须附非空 feedback，其他决定可附反馈。
- 接受前重新校验当前会话版本、pending 状态、简历代次、段落版本和目标旧文，以及引用的确切来源版本及有效范围。替换／插入／删除只作用于该目标；同目标的其他旧 pending 建议失效。
- 正文、来源关联、建议状态、用户决定、版本、旧运行失效和请求去重结果在同一事务提交，任一步失败全部回滚。24 小时窗口内同请求重放返回原结果；换 requestId 再接受已处理建议返回 stale_state，不重复插入。
- 拒绝只记录 rejected；要求调整记录反馈并将原建议标为 invalidated，二者不改变 draftRevision／blockRevision，但会推进 stateRevision 并中断旧运行。
- 用户恢复和单条建议读取会返回已保存的 decision（decision、feedback、createdAt）。Host 的 get_session_context 增加 decisionFeedback，包含当前代次被拒绝或要求调整的建议及反馈；这类历史建议不能当作当前个人事实来源。B 后续生成新的 pending 建议时可附 supersedesSuggestionId，须指向同会话当前代次、用户曾要求调整的旧建议；A 不代替 B 生成改写。

PUT draft 输入：`{requestId, expectedStateRevision, markdown}`。允许清空正文；单次最多 50000 字符。输出含 session、draft、stateRevision、draftRevision。draft 增加完整 markdown 字段；保留分块已有换行，独立且相邻的无换行块之间补空行，防止插入内容粘连。前端编辑／导出应使用此字段。

编辑采用保守、可还原原文的分块：按顶层标题及空行边界识别，围栏代码和缩进列表不在内部拆开。完整文本唯一且未变的块保留编号、版本和依据；改变、重复、合并、拆分或无法确定对应的块分配新编号且不继承依据，相应旧 pending 建议失效。完全相同的整稿保存不推进版本。已有初始导入采用粗块，因此首轮整稿编辑可能扩大旧建议失效范围；不使用相似度猜测对应。

用户补充 correction 不静默改动已接受正文；B 仍需主动发现冲突、记录问题、提出纠正，用户再次接受才改正文。A 的事务检查不等于已完成 B 的语义判断或前端冲突提示。

## 明确整体替换（A-T07）

先保存用户明确的替换要求消息，并展示影响：新版取代当前稿，旧优化／手改不自动迁入，旧 pending 失效。上传本身不表示授权。请求体示例：

```json
{
  "requestId": "replacement-001",
  "expectedStateRevision": 7,
  "expectedDraftRevision": 3,
  "materialId": "material-xxx",
  "authorizationMessageId": "message-xxx",
  "confirmed": true,
  "completeConfirmed": true
}
```

confirmed 表示用户操作路径已获得明确替换授权；completeConfirmed 表示已确认同人同岗位且提供完整新版。它们是用户路径的结构化确认，不要求额外点击按钮，也不能由模型自行代填。后端校验授权引用确属本会话当前有效用户消息，并保存确切消息编号／原文；不会通过关键词推断自然语言是否真有授权。无登录的本机接口不能提供身份认证。

当前仅支持角色明确为 resume、parse_status=parsed、当前代次且非历史的粘贴／Markdown 材料，须有一个来源块与原始 UTF-8 全文一致；partial、unknown、历史／跨会话材料，以及尚未解析的 Word／PDF／图片均拒绝。全文一致只能验证提取完整性，不能自动证明用户未遗漏经历或页面，内容完整性仍需用户核对。不要把此检查称为通用格式完整性识别。

一次事务更换当前稿、增加简历代次与稿件／会话版本、归档旧个人材料与问答、失效全部旧 pending 和旧运行、归档旧问题当前状态，保存替换前后正文、有效来源范围和授权引用；只激活新版个人来源，保留 JD 与岗位。历史已决建议及确切来源快照继续可回看。失败全部回滚；同 requestId 重试不再次换代。结构版本为 3，新增 suggestion_decisions，启动时从旧版本无损迁移。

输出含 session、draft、stateRevision、draftRevision、replacementId。替换记录用于追溯，没有历史稿切换或恢复接口；旧补充重新沿用需后续用户明确指定路径，A-T07 不自动恢复历史事实。真实模型网络取消仍由 B 接入，本项仅保证数据库运行失效及迟到结果写入被拒绝。

## 本机页面闭环与模拟示例（A-T08）

会话结构增加 mode=local／mock，旧会话迁移后默认为 local，普通创建接口不接受调用方指定 mode。结构版本现为 4／17 表；新增 004_session_mode.sql 仅追加字段，保留既有会话、正文、历史及去重记录。

创建模拟示例使用已标注 synthetic 的 tests/fixtures/minimal-resume-case.json，以真实 Repository 保存独立构造材料、补充与初始稿；不会复制用户会话或读取个人材料。示例岗位为 FDE 仅用于固定验收，不代表已验证其他岗位质量。创建操作与请求去重原子提交；同编号重放不生成第二份示例。

模拟分析只允许 mode=mock、未运行且初始正文和构造来源未改变的会话。行动顺序与建议文字为预设；实际执行受限 search_evidence、propose_change、record_issues 并保存结果。生成的建议仍是 pending，当前稿保持原文，用户接受时走同一真实确认事务。创建运行、工具保存、事件及去重结果一起提交，失败整体回滚。不调用模型，不产生费用，也不是模型自主选择工具或 MCP 联调证据。示例发生变动或已运行时，新请求拒绝继续套用固定建议，可新建模拟示例；不能自动忽略用户调整反馈再重提原建议。

用户恢复会话结果增加 events（当前简历代次的运行事件；含 runId、sequence、type、mode、payload）；本项持久保存 progress／diagnosis／tool_progress／question／suggestion／done，普通读取恢复而非 SSE。历史 issues 在恢复结果中增加 isCurrent；页面仅对 isCurrent 且 open 的问题显示未解决提示。模拟诊断是当次预设场景的记录，不会根据后续补充自动更新。页面另有独立标注的冲突／错误交互说明，不能把它们当作当前诊断或实际故障测试记录。

页面从 127.0.0.1:5173 访问现有代理：

- 新建时提供方向和完整简历，可在统一材料入口选择 Markdown 用作简历；可选 JD 单独保存。其他附件保留为 unknown／pending，不用未经实现的自动分类冒充识别。
- 已有会话可添加材料、保存补充／事实纠正／跳过、查看原文及确切引用、接受／拒绝／要求调整建议、编辑／预览当前稿。上传明确完整的 Markdown 新版候选不会自动替换，用户核对原文和完整性并明确授权后才调用替换事务；自然语言意图自动识别仍待 B。
- 编辑停顿 500ms 后保存。前端串行发送，保存途中继续输入不会被旧响应覆盖；失败保留原 requestId、正文与预期版本，先确认原操作，再保存后续编辑。网络断开或存储失败显示未保存，可重试／复制／下载。
- 切换会话前等待稿件保存；失败则留在原会话保留文字。版本冲突保留本地文字并展示服务器稿，用户可明确保留本地后按最新版本重试，或放弃本地采用服务器稿。
- 下载仅取编辑区的当前稿，不包含未接受建议、聊天或诊断；有未保存修改时允许导出可见文字，文件名标记“未保存”。未解决问题在预览／导出按钮所在区域提示，不伪称全部确认。
- 预览用 React 文本节点实现基础 Markdown，不执行原始 HTML／脚本／危险 URL，也不加载远程图片。完整 Markdown 保存在编辑器与下载文件中，预览不保证支持全部 Markdown 扩展。
- 未提交表单、消息、调整反馈及待上传文件在当前页面内存保留，离开时有 beforeunload 提示；不是浏览器持久化副本，关闭、强制刷新或开发热更新后不保证恢复未提交输入。已确认保存的正文／材料从 SQLite 恢复；URL 只记录会话编号。

前端验证命令（frontend 目录）：npm run typecheck、npm test、npm run build。前端测试使用 Node.js 自带 test runner，不需要新测试依赖或模型服务。实际浏览器烟测使用独立 SQLite，不把构造验收记录放入正式业务库。

## 删除与本地访问范围

DELETE 请求需 confirmed=true 和当前版本。数据库同时使旧运行失效、级联删除会话业务数据；A-T06 只实现持久化逻辑失效，实际模型网络请求的取消由 B 后续接入。用户原文件和独立费用账本不改动。

删除触发器清除该会话已有请求缓存的正文及指纹，仅保留窗口内的操作编号／范围等最小拒绝重放标记；已成功删除的同一请求可取回删除结果，旧创建／消息重试不会恢复已删会话。完整复盘内容不留在去重缓存中。

只接受本机 Host 与工作台 Origin（localhost／127.0.0.1 的 5173、3001）；显式跨站请求拒绝。命令行无 Origin 的本机请求可用。这不是账号鉴权，同一本机后端仍无多人身份隔离。Agent 工具不能调用这些用户 HTTP 入口；未来付费 Agent 入口仍需 B 配合接入既定本地请求令牌及预算控制。

## 验证

在 backend 目录：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

接口检查通过实际 FastAPI／ASGI 应用和隔离 SQLite 文件执行，无真实模型或外部网络；包含保存、重启重试、并发重试、回滚、材料回看、跨会话拒绝与删除清理。A-T07 增加目标替换／插入／删除、拒绝／调整反馈、重复段落／代码围栏／嵌套列表、手改失效、整稿替换范围、迁移保留及注入失败回滚检查。A-T08 增加模式迁移、模拟创建／运行的原子性及重放、真实材料拒绝、已修改示例拒绝、模拟事件恢复和实际确认事务；模拟功能测试不等于真实 Agent 评估。
