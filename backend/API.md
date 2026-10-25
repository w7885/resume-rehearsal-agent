# 本机业务 API（持久聊天与 A/B Agent 整合）

基础地址 `http://127.0.0.1:3001`，前端开发时经 `http://127.0.0.1:5173/api` 代理访问。
聊天页通过会话菜单接入既有 DELETE `/api/sessions/{id}`：请求包含 requestId、expectedStateRevision、confirmed=true；删除成功返回 `{sessionId,deleted:true}`。同一成功请求可重放；版本变化返回 stale_state，运行中的会话返回 run_busy，前端重新确认或等待停止，不自动删除较新版本。会话关联内容级联删除，项目费用账本保留。
GET conversation 增加只读 `originalDraft`（无可靠导入记录为 null）：`{sessionId,resumeGeneration,markdown,sourceRefs,materialIds}`。在同一读取事务中取本代次 import／replacement 的首版记录，按创建顺序拼回正文，避免手改重排影响初版；引用保留导入时的确切版本，materialIds 仅包含该初版关联的简历原件。不会用当前稿、JD 或后续补充冒充初版，不进行模型调用或数据库迁移。
FastAPI 的 `/docs` 和 `/openapi.json` 展示已登记接口；材料上传的两种正文格式以本文为准。
已提供建议确认、手改、明确整体替换，以及已保存聊天到 B Agent 的受信 Host 连接。模拟示例与个人材料会话分开；真实调用默认关闭，未满足密钥与预算条件时只保存内容，不调用模型。

## 整合入口、迁移与运行边界

应用启动时装配 AgentHost、运行／停止／预算路由。B 的推理循环、路由、工具、记忆与模型客户端保留，HTTP 接口改为受信 Host 管理；不再接收客户端传来的独立 message 文本或“已同意”证明。

前端只保留真实聊天界面：首页 `/` 与既有 `/chat` 链接加载同一个 ChatPage，查询参数中的 session 继续用于恢复会话。旧工作台和纯内存预览页已删除，不再提供切回入口；此界面清理不删除数据库记录，也不移除已有业务 API。

| 方法与路径 | 输入／结果 |
|---|---|
| POST `/api/sessions/{id}/runs` | requestId、userMessageId、expectedStateRevision → 持久 SSE；原话只从当前已保存用户消息读取 |
| GET `/api/sessions/{id}/agent-requests/{requestId}` | 请求状态、结果及已保存事件，用于恢复；不触发新模型调用 |
| POST `/api/runs/{runId}/cancel` | 停止对应请求链，拒绝迟到写入；已结束的请求返回原状态 |
| GET `/api/sessions/{id}/budget` | 统一账本的 limitCents、spentCents、reservedCents、remainingCents |

`conversation.capabilities` 动态反映当前是否可用。环境须同时具备 DEEPSEEK_API_KEY、AGENT_ENABLED=1、AGENT_BUDGET_CONFIRMED=1，才会启用真实客户端；缺条件时仍保留消息、附件、预览和当前稿。配置读取进程环境，不自动载入 .env，样例不含密钥。开发测试可显式注入构造客户端，回复标记 synthetic；普通 HTTP 不能选择构造模式或注入解析结果。

前端先保存用户消息，再按消息编号启动一次分析；分析失败不重复发送原话。请求编号不可更换内容；同一保存回合换分析编号也只返回原处理记录，不再次收费调用。已有已完成／失败／中断记录只重放，不自动重新执行；用户新的输入是新回合。SSE 只输出短状态与已保存消息编号，成功展示依据已提交事务；主对话不展开内部工具日志。连接丢失／停止会清理任务，旧运行与旧版本不能继续写入。服务重启将未完成运行记为 interrupted，未知费用预留保守计入支出。请求链允许自身材料确认／决定事务之后创建合法续接运行，不复活旧 runId；事件序号在整个请求链内递增。流处理上限 180 秒，停止前端等待不代表提供商未收费。

附件初始保持 unknown／pending。Host 读取当前附件的确切文本版本进行自动分类，校验返回编号及逐字证据；清楚的简历／JD 自动激活角色，内容或用途不明确、多份简历或提取不完整时才询问。单份分类文本上限 12000 字符，超出时不截取导入。分类通过追加来源版本保存，不原地改写旧来源。仅供参考的附件保留原件，其来源不激活为个人事实；JD 不进入简历正文。已有稿件时，明确替换要求须绑定当前真实用户回合及逐字授权，并排除否定、条件、引用和提问；否则只询问用途。图片转录是否可用取决于视觉开关与配置，不代表识别质量已经验证。

无稿件历史时，Host 检查最近 6 条当前代次的用户聊天，模型仅返回候选消息编号及正文第一／最后非空完整原文行；服务器检查锚点唯一性，计算连续正文范围，绑定消息哈希、正文范围及正文哈希后发起用途确认。原始消息不变，确认后仅以指定正文建立来源及初始稿，保存来源行号。范围外聊天指令不进入正文；正文中夹指令、重复／改写锚点或边界不明时不导入，要求分开发送。旧待确认询问缺少正文范围时重新识别，不沿用整条导入。来源、稿件和回合效果在同一事务写入；假设、否定、歧义、越界与过期确认均不能导入；单条超过 20000 字符改用文件上传，不截取导入。已有稿件不由此入口覆盖。

修改决定先由 B 解析为 none／clarify／resolved，再通过 A-T11.2 的原话／范围／版本校验与持久消费；旧“最后一条建议直接接受”路径关闭。普通 B 回复和建议在 Host 统一保存，结束运行发生在回复与事件持久化之后。B 记忆按简历代次、稿件版本和有效来源版本隔离；旧数据保留但不用于新稿。

唯一有效询问的简短肯定答复由 Host 直接解析为采用。采用事务完成后，在同一请求链内选择指定范围内下一未处理段落，最多保存一条新建议或发出一个追问，下一建议仍待确认。Host 在现有 agent_state 内另存本代次处理进度，不将稿件版本变化导致的 B 事实记忆重建当成重新改写第一段的理由；当前已采用的稿件版本也可恢复旧会话进度。保留原文的检查记录绑定正文和来源版本；来源失效先提示复核。明确停止不调用模型；待确认时“继续”只重呈待确认询问，不执行采用。接续失败或预算不足只报告下一步未完成，已提交的采用不回滚；相同用户回合仍只重放结果。

数据库统一至 schema 12／31 表：保留两份已发布的第 4 号迁移文件；仅对完整匹配已知 B4 结构的库补齐 A4，再执行追加迁移。A4～A10、B4 和新建库均可升级；未知结构拒绝，不直接改版本号、覆盖旧迁移或清库，失败全部回滚。B 的 agent_state 及预算记录保留。新增请求、流事件、回合效果、附件询问与项目预算表；消费及普通状态随会话删除，项目账本及费用预留独立保留。

ProjectBudget 约束统一账本控制的所有会话／日期，最大 3000 分。遗留 B 账本的已花金额和未知预留保守导入，不因会话删除、跨日或重启重置。每次调用先原子预留，响应按 usage 结算；取消、坏 JSON 或缺失 usage 不算免费。已有预留在重启后保守计入；无法理解的历史金额拒绝启用。该上限不覆盖其他机器／外部脚本绕过统一入口的调用；启用前须核对真实历史费用、费率并统一账本，不能将新建本地库当作重新获得 30 元。金额来自 B 的定价估算及提供商 usage，构造测试不证明真实账单准确。

整合验证使用隔离构造客户端与 MockTransport，没有调用真实提供商；语义准确性、实际 OCR、真实费用与团队端到端验收仍需独立证据。PR 合并表示工程整合通过，不表示这些外部效果已验证。

## 接口清单

| 方法与路径 | 输入／结果 |
|---|---|
| GET `/api/health` | 保留 `{status, model_configured}` 状态格式，不请求模型 |
| GET `/api/sessions` | 会话列表 |
| POST `/api/sessions` | direction、requestId → 会话 |
| POST `/api/chat/sessions` | requestId → 允许先保存材料的个人对话，求职方向待补齐 |
| GET `/api/sessions/{id}/conversation` | 恢复原始消息顺序、消息附件、全部原件元信息及当前稿；不返回内部问题清单 |
| POST `/api/sessions/{id}/chat/messages` | content、attachmentIds、requestId、expectedStateRevision → 保存用户原话及附件关联，不建个人事实来源、不执行建议决定 |
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

UTF-8 文本保存为原附件及来源块。明确 resume 且从未导入过稿件时，按原文形成初始 Markdown，不做模型改写；明确补充或 JD 不自动改当前稿。再次提交整份 resume 时保存为待澄清，不覆盖旧稿、不自动混入依据；即使用户已清空当前正文，只要仍有历史稿件，也必须走明确整体替换，不重新当作首份导入。unknown 在保存接口保持待分类，由后续聊天 Host 自动分类或按歧义情况追问。

### 原始文件二进制

同一路径，直接用文件字节作为 body，**当前不使用 multipart/form-data，也不要求 Base64**。query 参数为 name、requestId、expectedStateRevision、可选 role；同一参数不得重复。例如：

```text
POST /api/sessions/{id}/materials?name=resume.pdf&requestId=file-001&expectedStateRevision=3
Content-Type: application/pdf

[文件原始字节]
```

文件名不得含路径分隔符或控制字符，原附件以 BLOB 入库，不按传入名称写磁盘。支持 .md、.docx、PDF、PNG/JPEG；旧 .doc 提示转换。校验声明 MIME／扩展名与文件头或 DOCX 容器。**文件头通过不表示完整格式解析通过**：MD 保留原文及行号，DOCX／文字型 PDF 接本地解析器；PNG／JPEG 上传时仅保存为 pending；在启用视觉识别、确认预算且开启 Host 后，聊天处理中取得完整转录时为 parsed，带无法识别标记时为 partial，识别失败时为 failed。上传接口本身不调用模型；Host 绑定当前已保存回合、统一账本和附件哈希，每次调用／重试均先预留费用，再按返回 usage 保守结算，缺失费用或取消按预留记未知费用。旧图片可继续识别，已有文字不再次识别；失败需要新的明确重试回合。具体结果看 parseStatus 与 parseReport，失败仍保留已识别格式的原件，不生成假识别文本。

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

编辑采用保守、可还原原文的分块：按顶层标题及空行边界识别，围栏代码和缩进列表不在内部拆开。完整文本唯一且未变的块保留编号、版本和依据；改变、重复、合并、拆分或无法确定对应的块分配新编号且不继承依据，相应旧 pending 建议失效。完全相同的整稿保存不推进版本。新版聊天导入与附件用途激活按明确栏目、项目编号／标题或项目区日期边界分块，保留原文字及换行，各块保存与来源相交的逐字引用；不确定结构保守保留，不猜测拆分。旧粗块稿件不自动迁移，因此手改仍可能扩大旧建议失效范围；不使用相似度猜测对应。

聊天项目范围限制来自当前代次真实用户消息中最近的明确请求，按项目序号或可唯一匹配的完整名称定位服务器已有块，不接受模型提供的权限列表。建议保存和明确接受均重新校验目标；引用简历原文时须位于该项目保存的引用片段内，不能只凭相同整文件 sourceId 引用另一项目。无法唯一定位则询问；后续明确切换项目更新范围，拒绝／假设／引用不作为修改授权。

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

confirmed 表示用户操作路径已获得明确替换授权；completeConfirmed 表示已确认同人同岗位且提供完整新版。它们是用户路径的结构化确认，不要求额外点击按钮。聊天 Host 仅在材料范围明确、当前用户原话包含可校验的替换授权且新版完整可读时绑定这些字段，不能仅凭模型的 replace 标签代填。后端校验授权引用确属本会话当前有效用户消息，并保存确切消息编号／原文；底层替换接口不自行判断自然语言语义；聊天 Host 另做用途判断及保守的授权原句检查。无登录的本机接口不能提供身份认证。

整体替换支持角色明确为 resume、parse_status=parsed、当前代次且非历史的 Markdown、DOCX 和文字型 PDF。Markdown 须有一个来源块与原始 UTF-8 全文一致；DOCX／PDF 须有 parsed 且无警告的提取报告，并按保存顺序保留全部最新来源及各自引用。partial、unknown、历史／跨会话材料及图片替换均拒绝。完整提取报告不证明用户没有遗漏经历或页面，也不证明识别语义准确，内容仍需核对。

一次事务更换当前稿、增加简历代次与稿件／会话版本、归档旧个人材料与问答、失效全部旧 pending 和旧运行、归档旧问题当前状态，保存替换前后正文、有效来源范围和授权引用；只激活新版个人来源，保留 JD 与岗位。历史已决建议及确切来源快照继续可回看。失败全部回滚；同 requestId 重试不再次换代。结构版本为 3，新增 suggestion_decisions，启动时从旧版本无损迁移。

输出含 session、draft、stateRevision、draftRevision、replacementId。替换记录用于追溯，没有历史稿切换或恢复接口；旧补充重新沿用需后续用户明确指定路径，A-T07 不自动恢复历史事实。真实模型网络取消仍由 B 接入，本项仅保证数据库运行失效及迟到结果写入被拒绝。

## 本机页面闭环与模拟示例（A-T08）

A-T08.1 的会话结构增加 mode=local／mock，旧会话迁移后默认为 local，普通创建接口不接受调用方指定 mode。该阶段结构版本为 4／17 表；004_session_mode.sql 仅追加字段。后续 A-T08.3 的结构版本 5 见下文，两次迁移均保留既有数据。

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

## 连续聊天保存与恢复（A-T08.3，本次已实现范围）

真实聊天页现统一作为 `/` 首页，既有 `/chat` 会话链接继续有效；旧工作台与 `/chat-preview` 内存示例的实现已删除。A-T08.3 当时实现消息与原件保存，A-T08.4 复用既有 draft 接口接入聊天内当前稿编辑、保存、预览、复制、导出，以及附件原文／确切版本依据的按需查看；后续真实 Agent 与结构化决定接入见顶部整合说明，不把旧模拟事件编成真实助手发言。

新增结构版本 5／18 表：sessions 增加 direction_status、chat_title，messages 增加 input_channel，新增 chat_message_attachments。旧会话方向默认为 provided，旧消息默认 legacy；不重写原文或原件。迁移仍按序事务执行，不改旧迁移。

POST `/api/chat/sessions` 输入 `{requestId}`，以同一创建范围做 24 小时成功请求去重；返回真实 Session。初始 direction="新对话" 仅为兼容显示值，directionStatus=pending、targetDirection=null 才表示业务目标尚未记录。title 在首次消息成功后取原话首行或附件名，不能把标题当成岗位。已明确方向的旧会话为 provided，targetDirection 为原方向。此阶段不从用户文字猜方向，实际语义接入仍由 B 负责。

POST chat/messages 请求示例：

```json
{
  "requestId": "chat-message-001",
  "expectedStateRevision": 3,
  "content": "如果我做过 API 集成，是不是更有优势？",
  "attachmentIds": ["material-xxx"]
}
```

- 原文最多 50000 字符，最多 10 个不重复附件编号；有附件时允许正文为空，不补造“我上传了简历”等原话。无原文且无附件拒绝。role、kind、inputChannel、decision、SQL 等额外字段均拒绝；服务端固定保存 role=user、kind=message、inputChannel=chat。
- 附件必须为本会话、当前简历代次且非历史的已保存原件。附件顺序按请求保存；跨会话／缺失附件拒绝，不回显外部记录。关联、原话、标题、会话版本／旧运行失效与去重结果在同一事务保存，失败一起回滚。
- 新聊天入口不调用 save_source，不把“可以”“不要改”或假设变成个人事实，也不调用 accept／reject／revise。复用上传接口的 Markdown 可已有 unknown／pending 原文块，但这不表示个人事实已激活。旧 messages API 的来源建立行为保持兼容，不能混用为新聊天入口。
- 返回 `{session, message, stateRevision, draftRevision}`，message 带附件元信息，避免为每条写入缓存整段聊天历史。新消息不改变 draftRevision／当前稿。成功重放先于旧版本检查；同编号换内容报 request_conflict，独立操作相同文字是另一条消息。
- 新聊天写入仅允许 local 会话；旧 mock 会话可读取，但该页只供回顾，不能混入个人消息。模型运行上下文不能调用聊天用户写入或历史读取。

GET conversation 返回 `{session, messages, materials, draft, draftReviewRequired, capabilities}`：messages 按服务端插入顺序恢复并带 1 起始 sequence、inputChannel、historical、attachments；附件元信息含 byteSize 与 current，不读取原件 BLOB 进普通消息列表。materials 含已保存但还未绑定消息的原件，用户可按需查看并加入下一条消息；下载复用原件用户入口。capabilities 由 Host 的启用、密钥及预算条件共同决定；默认两个字段均为 false，具体条件见顶部整合说明，不能只根据是否配置密钥推断可用。

前端一次发送分为“幂等建会话 → 各原件分别上传 → 原话与附件关联提交”。这些 HTTP 步骤不是一个跨请求数据库事务：原件成功、后续消息失败时，原件仍保留且可恢复。客户端保留每步编号、原内容和已成功结果；失败不清空输入，结果未知时只允许重试原发送。已明确拒绝的步骤可以返回编辑，复用已存原件，修正后再发送，不重复上传成功项。消息已确认、后续读取失败时只重试读回，不重发消息。

待发送输入和 File 对象仅在当前页面内存按会话保留，离开时提示；不是浏览器持久备份。刷新可恢复已入库原话与原件，未提交的文字不保证恢复。新页没有自然语言解析、自动分类／方向识别、自动成稿或建议确认能力；这类消息只保存原话，分析服务未接入时显示真实系统状态，不伪装 Agent 回复。

## 删除与本地访问范围

DELETE 请求需 confirmed=true 和当前版本。数据库同时使旧运行失效、级联删除会话业务数据；A-T06 只实现持久化逻辑失效，实际模型网络请求的取消由 B 后续接入。用户原文件和独立费用账本不改动。

删除触发器清除该会话已有请求缓存的正文及指纹，仅保留窗口内的操作编号／范围等最小拒绝重放标记；已成功删除的同一请求可取回删除结果，旧创建／消息重试不会恢复已删会话。完整复盘内容不留在去重缓存中。

只接受本机 Host 与工作台 Origin（localhost／127.0.0.1 的 5173、3001）；显式跨站请求拒绝。命令行无 Origin 的本机请求可用。这不是账号鉴权，同一本机后端仍无多人身份隔离。Agent 工具不能调用这些用户 HTTP 入口；整合后的 Agent 入口复用同一本机 Origin 检查，并由受信 Host 核对已保存输入、运行和项目预算；不将它视为多用户鉴权。

## 验证

在 backend 目录：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

接口检查通过实际 FastAPI／ASGI 应用和隔离 SQLite 文件执行，无真实模型或外部网络；包含保存、重启重试、并发重试、回滚、材料回看、跨会话拒绝与删除清理。A-T07 增加目标替换／插入／删除、拒绝／调整反馈、重复段落／代码围栏／嵌套列表、手改失效、整稿替换范围、迁移保留及注入失败回滚检查。A-T08 增加模式迁移、模拟创建／运行的原子性及重放、真实材料拒绝、已修改示例拒绝、模拟事件恢复和实际确认事务；模拟功能测试不等于真实 Agent 评估。

### 当前稿整合（A-T08.4）

`draftReviewRequired` 为简短交付提示：当前代次未解决的 `kind=conflict` 记录，或当前稿仍引用非当前有效来源时为 true。不返回内部问题清单、摘要或工具日志；读取、复制和下载均不修改 Issue 状态。识别新的语义冲突仍由 B 负责，不用该字段宣称事实已全部核验。

聊天页右侧复用 PUT draft 的版本与幂等语义及原 DraftBuffer。停止输入 500ms 自动保存；切换、重新读取或发送新消息前等待同一保存缓冲，失败保持当前会话及本地文字。版本冲突由用户明确选择保留本地文字或采用服务器稿；未知结果重试原编号与原正文。隐藏面板不销毁缓冲。未入库编辑只在当前页面内存，关闭／刷新前提示，不能当作持久备份。

预览使用已有安全 Markdown 组件。复制与 Markdown 下载仅取可见当前稿正文，不混入消息、材料、内部问题或未接受建议；未保存文件名带“未保存”，模拟历史文件带“模拟示例”。聊天页模拟会话仍只读。附件与依据弹窗按需请求既有 material detail／source revision 入口，依据校验确切 sourceId 与 sourceRevision，不能用最新版本替代旧引用；旧建议决定仅作历史记录展示，不提供独立确认面板。


## 文档解析与原文修正（A-T09）

结构版本 6 追加 attachments.parse_report_json（可空 JSON），不新增表、不改变原件字节或既有来源。旧材料报告为 null，不追溯伪造已解析结果；既有 DOCX／PDF 需要重新上传才使用新解析器。新增本地依赖 pypdf==6.21.2、defusedxml==0.7.1，不调用模型或云端 OCR。

新上传仍使用原 materials 入口。解析在短时独立进程中执行，工作在 SQLite 写事务之前；最多等待 6 秒。DOCX 按结构顺序提取段落、表格单元格和文本框文字，origin 保留 paragraph／part／table／row／column。文本框段落单独保存；Office 兼容性替代内容只取首个分支，避免重复，但不删除其他位置真实重复的文字。浮动文本框仍标记 partial。同一个无普通正文的锚定段落内，仅当页面大小、统一的页面纵坐标与横向基准可用、文本框横向范围存在共同交集，且没有分页、分栏或文字旋转等歧义时，按纵坐标排序各框并保留框内顺序。不跨锚定段落／节／表格单元格推测页序，不对独立双栏或缺失坐标强行排序；不满足条件时保留结构顺序并报告 floating_order_unresolved，成功局部排序报告 floating_order。此规则不等于完整视觉还原。分栏、自动编号、图片、修订、合并单元格和脚注等未完整处理的内容会提示核对，不宣称视觉还原。页眉页脚单独提取；DOCX 段落编号不是 Word 页码。原件不解压写入工作目录，XML 禁用 DTD／实体。此规则用于新解析，不自动重解析历史附件或重写旧来源。

PDF 按原页序保存非空文字页，每页一个来源块，origin.page 为原 PDF 页码，lineStart／lineEnd 指提取文本的行号，并非原页面坐标。保留布局间距；发现疑似分栏、无文本页、图片／图形、字符错误或单页失败时标记风险。最多前 40 页、总计 50000 字符／1000 块，超限提示部分结果，不静默截断为成功。扫描／图片 OCR 仍属于后续 A-T10，不在本项完成；字体、布局等造成的所有遗漏不能自动保证识别。

material.parseReport 为 `{parser,status,pageCount,message,warnings:[{code,message}]}`：parsed 表示本次提取未发现已检测风险，不证明内容完整或事实真实；partial 表示有可用文字但存在遗漏／顺序等风险；failed 表示没有可用提取结果或损坏、加密、超时；pending 留待图片识别。来源、原件、报告及请求去重一起落库，存储失败整体回滚；解析失败属于“原件已保存”的业务结果，返回 201 和明确 failed 状态。

unknown 角色始终 pending。partial／failed 不自动建立初始稿，也不直接进入有效个人事实；用途明确且本次 parsed 的旧版 resume 上传可建立初始稿，已有稿件则仍作候选。聊天页不预先分类材料，不因提取成功自动改稿。材料 detail 按页／段位置及版本顺序返回原文快照，附件列表只显示短状态，报告在详情按需展开。

```
POST /api/sessions/{sid}/materials/{mid}/sources/{sourceId}/revisions
{"requestId":"correction-001","expectedStateRevision":8,"expectedSourceRevision":1,"text":"按原件修正后的文字"}
```

这是用户路径，仅接非空文字与版本，不接角色、来源位置或任意路径。拒绝模拟／跨会话／跨材料／历史代次与过期来源。成功返回 `{source,stateRevision,draftRevision}`；来源新增一版，继承原位置及当前用途，不因修正就把 unknown 激活。旧引用／原件／当前稿不改写，旧 pending 建议失效，旧运行由上下文版本推进失效，Host 后续可据此重评估；B 的真实重评估尚未联调。同一请求重放只返回原版本，数据库故障不留下半个快照。

聊天附件详情提供“修正提取文字”。编辑未保存时阻止关闭详情／提示离开；结果未知时保留原文字、原编号和版本，只能重试原修正。已确认保存而读回失败时只重读，不再新增版本。用户明确取消才能丢弃未提交编辑；纠正来源与手改当前稿是两个不同操作。


## 本地图页准备（A-T10.1，已实现；识别结果接入未实现）

结构版本 7：attachments 追加 page_report_json，新增 material_pages（共 19 表），图页与原件组合外键绑定、删除会话时级联清理。原件 bytes、解析报告、来源和当前稿不被图像副本覆盖。依赖 pypdfium2==5.14.0、Pillow==12.2.0，仅本机渲染／解码，不调用模型、OCR 或云端接口。

- GET `/api/sessions/{sid}/materials/{mid}/pages`：只读页状态，返回 `{materialId,renderVersion,pageCount,limit,historical,pages,recognitionAvailable:false}`。初次 pageCount=null、第一页 pending，读取状态不会触发渲染；已知页数后按原 PDF 页码返回前 40 页，未准备页为 pending。图像文件只有第 1 页。
- POST `/api/sessions/{sid}/materials/{mid}/pages/prepare`：`{requestId,expectedStateRevision,pageNumber}`，页码为 1～40，不接文件路径、模型参数、密码或外部 URL。每次只准备一页，返回同样页状态结构。图页准备失败属于明确 failed 结果并保留原件，可发起新的重试；网络结果未知则重试同编号／正文。缓存成功结果跳过再次渲染；同请求成功重放不重复落库。
- GET `/api/sessions/{sid}/materials/{mid}/pages/{page}/image`：只返回已准备成功的 `image/png`，附 no-store／nosniff；未准备／失败返回 source_not_found。没有公共图床链接或任意路径访问。以上均经过既有本机 Host／Origin 守卫与会话归属检查。

渲染在 SQLite 事务外的独立进程中执行，最长 6 秒；PDF 原页序不重排，按最大边 1600 像素、最高 120dpi 准备预览。PNG／JPEG 验证实际可解码与静态单帧，应用 EXIF 方向后生成独立预览；原文件保留原尺寸与字节。最大原图 2500 万像素、单页输出 6MiB；加密、坏文件、超时和页码错误有可定位反馈。缓存键含本会话附件、原件摘要、原页码和 renderVersion，版本或摘要不符不读取旧图。

这是派生视图缓存，成功准备不推进 stateRevision／draftRevision，不创建 SourceBlock、不将 parseStatus 改成 parsed，也不取消正常运行。图像、尺寸／失败记录、页数报告及请求去重原子提交，失败整体回滚。原页数超过 40 会提示拆分，其余页面不假装已经准备。图像已准备不代表识别文字已完成。

受信 Host 可直接调用 `Repository.read_material_page(RequestContext(sid,stateRevision,runId,generation), materialId, pageNumber)`，返回 `{imageData,mediaType,sessionId,materialId,contentHash,page,width,height,renderVersion}`。Host 持有真实运行上下文；拒绝取消／过期运行、跨会话、旧简历代次、历史或模拟材料。当前 unknown 材料允许由受信 Host 读图作后续分类，不自动成为事实。该读取方法不是模型可任意选择的文件工具，原 MaterialTools 白名单保持不变。

按用户反馈，聊天附件打开后直接显示内容：PDF／图片自动准备并连续预览，不展示原页码、准备按钮、解析状态或版本卡片；Word／MD 默认显示最新提取正文，不重复拼入旧来源版本。仅保留文件名、下载和关闭，无法完整显示时给出简短重试／下载提示。图像自动准备仍按原页码顺序和既有范围／缓存规则执行；请求未知保留原请求，读到成功缓存即可显示，不重复准备。关闭后停止继续准备后续图页，重开复用已落库缓存；后台页码、报告和确切引用不删除。原文修正入口移至“修改依据与记录”中的可选材料修正区域。截图顺序纠正、B 识别回执保存／修正和版本缓存已在后续 A-T10.2 实现，见后文；真实 OCR 调用仍未接入，不能把本阶段标为完整图片识别。


## 助手回复与询问关联（A-T11.1，A 侧已实现）

结构版本 9（23 表）：messages 追加回复关联、保存前后版本与候选询问；assistant_messages 保存受信 Host 回执，assistant_suggestion_links 保存建议快照。旧消息关联为空，不推断历史授权。整合后的 B 路由和动态 capabilities 见顶部说明；本节仅描述回复存储与关联约束。

```python
context = RequestContext(session_id, state_revision, run_id, generation)
turn = service.get_chat_turn_context(context, user_message_id)
service.save_assistant_message(context, {
    "requestId": "reply-001", "userMessageId": user_message_id,
    "content": "是否采用这个表述？", "kind": "change_question",
    "suggestionIds": [saved_suggestion_id]
})
```

两个方法仅供绑定当前已保存 running 运行的 Host 调用，没有浏览器注入助手消息的 HTTP 端点，不新增模型可任意选择的写入工具。触发回合必须是真实已保存、同会话、当前简历代次、版本与最新用户消息；拒绝旧运行、迟到回执与跨会话编号。kind 为 reply／question／change_question，只有 change_question 必须绑定 1～10 个真实 pending 建议。服务器核对目标块版本、before 与有效来源，保存建议及确切来源版本快照，不接收客户端自造目标。可选 sourceRefs 最多 50 条，正文最多 50000 字符。

回执、关联与去重结果原子保存；同一请求同一上下文可重放，换正文／运行拒绝。保存回复不推进会话状态，不新增事实来源、不修改当前稿、不执行建议。测试必须显式 synthetic=True，消息标记“构造示例 · 非真实 Agent 回复”，构造演示不进入个人会话。

`POST /api/sessions/{sessionId}/chat/messages` 增加可选 replyToMessageId。它只关联同会话、同简历代次的已保存询问；正文原样保存，不按“可以”等关键词执行决定，不默认选择最后一条建议。缺省／null 不改变旧请求的去重指纹。用户消息记录 savedBeforeRevision／savedAfterRevision 及保存前候选询问。明确关联已过期询问可保留追溯关系，但不能恢复有效性。

会话读取的 assistant 元数据返回 kind、mode、replyToUserMessageId、sourceRefs、questionState。状态为 awaiting_reply／reply_saved／outdated／history，普通回复为空；不表示接受或执行成功。界面仅对 awaiting_reply 显示可选“回复这条询问”，查看依据按这条回复当时引用的原文版本读取；编辑当前稿成功后重新读取询问状态。

Host get_chat_turn_context 返回真实用户原话、明确 replyToMessageId 与候选询问快照。普通答复造成的 S→S+1 可标记 reply_saved；中间附件上传、手改、来源变化、整体替换或后续新询问不能借此跳过版本检查。关联本身不是授权；确认事务适配见 A-T11.2，运行循环／费用与真实 B 联调属于尚未完成的 A-T11.3，自然语言理解仍由 B 提供。

## 对话决定执行适配（A-T11.2，A 侧已实现）

结构版本 10（24 表），新增 chat_decision_resolutions，持久记录每条用户答复的处理结果。仅受信 Host 调用，不开放 HTTP 注入接口，不进入 MaterialTools 模型工具白名单。

```python
service.apply_resolved_user_decision(context, {
    "requestId": "decision-001", "userMessageId": saved_user_id,
    "userContent": exact_saved_user_content, "status": "resolved",
    "decisions": [{"promptMessageId": saved_prompt_id, "suggestionId": saved_suggestion_id,
                   "decision": "accept", "feedback": ""}]
})
```

context 须绑定当前真实 running 运行、最新输入版本及代次。userContent 必须逐字等于数据库中的完整用户原话，不能用助手摘要代替。status 只取 none／clarify／resolved；前两者 decisions 须为空，不改稿、不改建议状态，也不推进会话版本。resolved 须有 1～10 个互不重复的建议选择；decision 为 accept／reject／revise。feedback 为空或是这条原话的连续片段；revise 须非空。A 不分析自然语言，结构化解析由 B 提供，出处核对不能证明 B 对否定／条件／引用等语义理解正确。

目标必须在答复保存时捕获的候选询问内，明确 replyToMessageId 时不能换另一条询问。核对原询问的建议快照、pending、正文、来源、稿件版本与代次，以及仅由该答复造成的 S→S+1；不得用当前版本替代缺失的旧上下文。过期关联保留追溯意义，但不能直接执行。只处理 B 明确选择的建议，不默认最后一条，不自动扩展到全部。整批预检后再执行，选择过期／同一段落多个相互竞争的接受决定时整批拒绝，保留原稿供澄清；执行中任何失败也整批回滚，不跳过失败项后谎报全成功。

在同一业务事务内复用 A-T07，决定、稿件／依据、建议状态、消费记录及请求缓存一起提交；多处选择只推进一次会话版本，各接受操作沿用原稿件版本规则。拒绝／要求调整只记录决定及原话反馈，调整后的新改写仍由 B 生成。接受仍须有个人材料依据。修改会中断旧运行；已拒绝、失效或采用的建议不能复活。

请求缓存仍为 24 小时；消息消费记录随会话长期保留。响应丢失后相同请求可读原回执，即使缓存过期也不会重做。同一用户消息换目标、动作、原话或运行上下文拒绝；none／clarify 也是该回合的已保存结果，后续澄清应产生新的真实用户消息。会话删除级联清理消费及回执，旧请求不能恢复会话。重复结果仅代表当时已提交事实，Host 必须重新读取当前稿，不能用旧回执覆盖后来的界面状态。

返回 userMessageId、status、mode、decisions、stateRevision、draftRevision，不返回旧稿正文。事务成功后旧运行失效；Host 新建合法运行，可通过 get_chat_turn_context 的 decisionReceipt 读取该回合结果并保存对用户的结果回复。只允许该回合自身提交后的精确版本，其他手改／新输入之后仍拒绝旧回合回存。

构造检查显式 synthetic=True，所选询问也必须为 synthetic；默认 host 模式不能消费构造询问。本节的构造检查只验证预设结构化结果；整合后的真实入口和默认关闭条件见顶部说明，实际模型语义与费用尚未经真实调用验证。前端仍使用连续对话和现有当前稿，不增接受／拒绝工作流面板。

## 识别回执、图片阅读顺序与重试意图（A-T10.2，A 侧已实现）

结构版本 8（21 表）：attachments 追加 reading_order／recognition_report_json，新增 recognition_results、recognition_retry_requests。原件 original_order 继续表示上传顺序；读取材料数组按 coalesce(reading_order,original_order) 排列，不应再自行按 original_order 覆盖用户采用的顺序。PDF 内部页码与消息原始附件顺序不变。

### 受信 Host 回存及缓存

识别结果没有普通浏览器注入端点，也不新增模型可直接选的写入工具。由 B 的 Host 在真实已保存的最新 running 运行上下文中调用：

```python
context = RequestContext(session_id, state_revision, run_id, generation)
cached = service.get_cached_recognition(context, material_id, page_number, recognition_version)
# B 决定是否按显式重试请求及预算调用识别；A 不调用任何模型。
service.save_recognition_result(context, {
    "requestId": "recognition-job-001", "materialId": material_id, "pageNumber": 1,
    "contentHash": original_hash, "renderVersion": render_version,
    "recognitionVersion": recognition_version, "expectedSourceRevision": 0,
    "status": "success", "text": "识别文字", "role": "unknown", "errorMessage": ""
})
```

可选 retryId 关联当前材料待处理重试请求。status 为 success／partial／failed；partial 须有非空文字及问题说明，failed 须文字为空、说明非空。每页文字最多 50000 字符，说明最多 1000 字符。不接会话覆盖、路径、SQL、来源编号或任意页位置。校验 Host 运行、当前简历代次、同会话附件、原件摘要、图页版本／缓存以及预期来源版本；拒绝取消／旧运行、历史／模拟材料、错误页或迟到回执。识别失败可关联失败图页，但成功文字必须对应准备成功的图。

role 仅保存为 proposedRole，来源继承附件当前角色且保持 pending；不会因回执自称 resume 就激活个人事实。回存通常新建来源版本并保留旧快照，旧 pending 建议失效；所有状态、来源、重试进度、材料报告与去重在同一事务提交，失败一起回滚。当前稿、已接受内容和原件不改写。待核对结果写入不推进会话版本，避免同一运行批量回存把自己取消；用户修正、调整顺序或申请重识别仍推进版本并使旧运行失效。

缓存以附件摘要、原页码、渲染版本、识别版本及当前代次核对，返回 cacheHit、result、currentSource、expectedSourceRevision、retryRequests。失败回执不作为成功缓存；已有成功页／旧可用文字不会因其他页失败而删除。原始回执 text 与人工当前文字分开保存：如果已有人工作出的来源修正，即使主动重识别也保留 currentSource，标记 preservedCorrection／userCorrected，不让新回执覆盖人工版本。Host 应优先使用 currentSource，不能将 raw result.text 冒充人工修正版；引用仍以确切 SourceBlock 文本校验。

同一请求、同一运行可重放且不重复新增快照；同编号换内容／运行拒绝。运行已结束时旧回存拒绝，可在新的合法运行中通过缓存读取已保存结果，不复活旧运行。测试注入需显式 synthetic=True，回执及报告标记 synthetic，仅用于隔离构造验证；不能据此声称真实 B 识别通过。

### 用户图片顺序与重新识别意图

POST `/api/sessions/{sid}/materials/image-order` 接 `{requestId,expectedStateRevision,materialIds}`。materialIds 须包含当前范围全部 PNG／JPEG，完整、无重复、最多 200 个；只置换图片已有阅读位置，其他材料位置、PDF 内部页序及原始上传顺序不动。成功返回 conversation。实际变动推进一次上下文版本，正文／来源内容不变；同请求重放不重复移动，存储失败整次回滚。

POST `/api/sessions/{sid}/materials/{mid}/recognition-retry` 接 `{requestId,expectedStateRevision}`，返回 `{retryId,status:'pending',stateRevision}`。它只记录材料的全部已知页范围，不调用模型；同材料当前范围已有 pending 请求时返回原请求，不重复排队或推进版本。未知页数、加密或超过 40 页时需先查看／转换／拆分。Host 在缓存中读到请求，自行核对预算后处理；逐页回执携带 retryId 更新进度，全部返回后为 completed 或 failed，失败页不清除其他页文字。

附件列表按需“调整图片顺序”，上下移动仅作用图片；有识别记录的附件可“申请重新识别”，只提示已记录、等待服务。界面操作未知时保留原请求；已确认写入而刷新失败只重读。附件点击打开仍仅显示原内容，不铺开识别页码、状态／版本卡片；原文校对保留在可选依据区域。实际 B 接口、模型预算与真实识别效果尚未联调。

### JD 目标与简短确认

单份当前有效 JD 的明确标题可同步为会话目标；没有明确标题时使用“按已提供的岗位描述优化”，不编造岗位。用户已明确的目标优先，多份 JD 不自动挑选。目标同步在确认事务之后或正常分析之前进行，避免仅更新岗位信息就使有效确认过期；Agent 记忆同步目标并清除旧的 awaiting_goal。分析上下文另包含 JD 原文及来源版本，标明岗位要求不能作为个人事实。

Host 对只有一张有效询问且只有一条建议时的严格肯定词（是／是的／采用／确认／同意／好／可以等）直接构造绑定该询问的 accept 选择；不调用模型解析该答复。多候选不会默认选最后一条。决定仍进入既有版本、来源、代次、范围、消费与幂等事务；条件句、引用或带问号的表达不走简短肯定分支。历史消息及已处理的旧答复不被静默重放。

### 数字与职责等级的个人依据检查

propose_change 的新增数字／职责等级校验，仅采用目标当前稿和该建议引用的 resume／supplement 原句。JD 可用于岗位匹配，但不能为新增个人年限、业绩或主导职责提供依据；未引用的材料、原始聊天中的假设和模型自己保存的事实记忆也不能单独授权这些新增内容。此检查不是通用语义真实性判定：不含数字或已知职责词的新增资质等表述仍可能漏检，A-T12 离线语义验收有未通过项，不能据此宣称无虚构。

### 问答来源与失败接续（A 统一修复）

聊天保存入口仍不自动建立事实来源。Host 已定位当前经历且该经历的采访问题被标记为已回答后，可将当前代次、当前会话、有效用户消息中的明确回答原样接为 supplement 来源；来源绑定原消息，供建议逐字引用。控制词、假设、示例、他人表述、疑问和不确定回答按保守规则留在聊天中，不自动提升为个人事实；混合回答也可能需要用户另行明确事实。这是规则过滤，不是通用语义真实性证明；“有代码”仅是用户陈述，不代表服务访问或核验了仓库。

Host 可恢复当前经历已答问题关联的历史回答来源，按消息去重，不将整段聊天无差别变成事实；不复活被修正或停用的来源。新增来源、记忆命名空间迁移、当前运行版本及回合回执在同一事务内更新，写入失败回滚。此可信增量不会清空已问记录或已完成进度；任意来源修正／简历替换仍走原有失效规则。采访回答仅进入对应经历的可用来源，新增数字仍必须由建议中的确切个人原句支持，不以记忆替代依据。

兜底问题跳过已答／已拒绝维度，并经过同一问题检查；实际效果保留既有最多两次追问规则。改写被拦且无法继续时，显示本轮具体原因，保留 limited 结果，不将笼统失败提示伪装成采访问题。“继续”不自动放弃受阻段落，也不重复调用模型；用户明确回复“保留原文，继续下一段”后才记录保留决定并接续，当前稿不变，下一条建议仍须确认。存在 pending 建议时优先要求处理该建议，不借保留指令绕过确认。

### 唯一建议的措辞反馈（A 统一修复）

只有一个有效询问且只绑定一条有效建议时，“太冗余、太口语、精简些”等明确措辞反馈可直接映射为 revise，feedback 保留完整原话；仍进入既有版本、来源与消息消费事务，不采用或改写当前稿。多候选、条件、引用、假设或明确暂不修改不走直接映射。单条 revise 成功后，Host 在同一用户回合继续处理该建议对应段落，生成新的 pending 建议；当前段的最新版修改反馈与旧建议进入生成上下文，旧建议明确不作为事实来源。失败时保留反馈和当前稿，不伪称新版生成成功；请求重放不再次改写。默认生成规则强调合并重复信息、简洁要点与书面表达，此为提示约束，不代表真实模型精简质量已验收。

### 目标、事实与引导式提问的补充规则

有效确认消息按 suggestionId 去重；多个确认消息引用同一条建议时，仍是一项操作目标。存在其他类型的待答问题或多个不同建议时，保留原有澄清和显式回复目标检查。唯一目标下操作意图尚未明确时，询问采用或继续调整，不再要求重复指认“哪一条”；“保存”不被静默解释为采用。

save_facts 默认追加并存事实，不凭“未／不”等否定词替代同维度旧事实。NewFact 的可选 supersedes_fact_ids 仅在当前已保存用户消息明确更正且编号属于该经历同维度的有效事实时生效；普通补充不能替代旧事实。该规则防止未来误覆盖，不自动猜测修复历史事实的语义。采访回答的原始来源关联回事实，生成上下文优先提供有效事实所需来源，并展示版本定位；超出展示范围时允许通过 read_material 读取，不从记忆伪造引用。已明确描述的实际功能可以附带“例如”说明，单独假设／示例仍不自动成为个人事实，规则过滤仍非完整语义证明。

主问题与可选回答提示合计最多 120 字，仍恰好一个问号、一次一个点。兜底问题结合原文中的功能或任务提供回忆入口；有效模型问题在合适时附上同维度中性的回答角度，先选熟悉的一项，不要求逐项作答，不预设参与或效果，允许没有／不记得。问题及提示再次通过隐私、诱导、新数字和跨经历检查；重复性按问号前的主问题比较，不能靠更换提示重复追问。职责后优先具体动作及效果，效果最多两次的限制保持。提示内容不记录为个人事实。
