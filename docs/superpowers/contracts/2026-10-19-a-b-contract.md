# A-T02:A/B接口与最小样例契约

版本：v0.4 · 更新：2026-10-20

语言变更：用户决定整个后端与 Agent 统一使用 Python，前端仍为 TypeScript。A 的 Repository／业务路由与 B 的 Agent 路由在同一个 Python 后端内直接协作；接口 JSON 字段、错误结构、版本、权限及最小样例含义不因语言改变。FastAPI 为修订技术方案的建议框架，尚无产品实现或 B/C 回执。

本次按 PRD v0.4 同步有效来源与新版替换边界；既有最小 JSON 仍表示未发生替换的初始场景，未覆盖新版字段或替换流程，后续共享类型与 FX-013 需补齐。本地文档核对不等于收到 B/C 确认。

## 1. 合作边界

A 提供数据库、业务接口、材料工具和界面；B 提供 Agent Router、模型调用、记忆装配、工具选择及预算；C 提供／核对人工事实基准和执行记录。当前构造样例由助手按既有测试设计准备，未收到 C 的实际样例或 B 的反馈，不能记录成全组已认可。

一次交接：A 保存用户输入 → B 获取当前会话 → B 请求检索 → A 返回原文及编号 → B 提出建议 → A 校验并保存为待确认 → 界面显示 → 用户接受 → A 事务写入正式稿。

模型、B 的工具循环都不能调用“接受建议／直接编辑／删除／调整预算”作为模型工具。共享服务函数的 context 由程序注入，不能让模型通过参数声称自己是用户或切换会话。

## 2. 最小对象

所有编号由程序生成。JSON 样例使用固定构造编号以便复查，不代表模型能生成合法业务编号。时间使用带时区的 ISO 字符串；文件位置、来源定位与业务编号不混用。

| 对象 | 共同字段 | 用途 |
| --- | --- | --- |
| Session | sessionId、direction、stateRevision、createdAt、updatedAt | direction 必填，JD 可选；一个会话一个求职方向／目标岗位 |
| Message | messageId、sessionId、role、content、createdAt | role 为 user／assistant；用户补充只是用户陈述，不是外部核实事实 |
| SourceBlock | sourceId、sessionId、sourceRevision、role、origin、text | role 为 resume／jd／supplement／unknown；origin 记录 material 或 message 编号及页码／段落／行号 |
| SourceRef | sourceId、sourceRevision、quote | 引用确切版本及可定位原文；引用存在不等于支持任意推断 |
| Draft | sessionId、draftRevision、blocks | blocks 中每块含 blockId、blockRevision、markdown、sourceRefs；全文按块顺序拼接，不混入建议 |
| Suggestion | suggestionId、sessionId、operation、target、before、after、reason、sourceRefs、gaps、status | operation 为 replace／insert_after／delete；target 包含 blockId 和 expectedBlockRevision；status 为 pending／accepted／rejected／invalidated |
| Issue | issueId、sessionId、kind、summary、sourceRefs、affectedBlockIds、status | status 为 open／resolved／skipped；事实冲突与未解决缺口不能因导出就变成 resolved |
| RunEvent | schemaVersion、sessionId、runId、generation、sequence、type、mode、payload | sequence 从 1 递增；mode 区分 mock／live，防止把模拟演示当真实模型成果 |

origin 中仅放定位与来源身份，不放任意文件系统路径。业务模型可扩展展示字段，但不能删掉会话、版本、来源及状态边界。后续如修改字段，同步样例、共享类型与 C 的基准，不各改各的。

### 2.1 版本规则

- stateRevision 是用户改变本会话上下文的版本：保存新用户输入／材料修正、接受／拒绝／要求调整建议、手改稿件等会递增；这些决定影响 Agent 下一步，旧运行需失效。拒绝或要求调整不改变正式稿的 draftRevision／blockRevision。
- 同一运行保存诊断、待确认建议、工具进度或结束事件，不递增用户上下文版本，不能让 Agent 因保存自己的建议而把自己判为过期。
- blockRevision 在对应正式稿块改变时递增；draftRevision 在正式稿改变时递增。只生成 pending 建议不改这两个版本。
- sourceRevision 对应不可变来源快照；用户修正后产生新版本，旧依据卡片仍指向旧快照，工具默认读最新有效来源。
- generation 由 B 的服务端运行控制生成，新的／被取消的运行不能沿用旧代号。事件写入要核对会话、运行代号及上下文版本，不依赖模型口头声明。
- 来源编号存在但属于另一会话、版本不存在或 quote 与快照不符时拒绝引用。来源真实且同会话也仍需检查语义是否支持改写。

### 2.2 同会话新版替换的增量边界

以 PRD 第 3.3.1 节及技术方案第 3.4 节为准：一个当前稿允许多个材料，补充／参考不自动改稿；只有用户明确授权才可整体替换，同人同岗位外需新会话。材料用途未明时不进入事实检索。替换操作属于用户业务路径，不提供给模型直接调用。

A/B 在共享类型实现前需对齐当前简历代次、来源／用户补充的有效范围、替换记录及用户授权引用；当前简历代次不同于单次运行 generation。替换事务同时更换当前稿、递增版本、失效旧 pending／运行、归档旧问题并调整有效材料／问答范围；历史已决建议及引用保留，旧优化与经历不自动迁入。失败不改变原状态，重复 requestId 不重做。

下表 getSessionContext 的有效来源／问答、readMaterial 的运行读取、searchEvidence 的查询，以及 saveSuggestion 的 sourceRefs 校验，均只允许当前有效范围；同会话不等于当前有效。待澄清或替换前来源不得通过直接指定编号绕过限制，新建议引用返回 stale_state 或明确的不可用来源错误。历史依据卡片通过用户只读路径读取确切快照并标示历史，不作为当前 Agent 的事实检索入口。用户明确指定沿用后才重新纳入，冲突仍澄清。

## 3. A 提供给 B 的服务与工具

以下是将来要实现的 Python 服务方法约定，当前不是可调用 API。文中的 readMaterial／searchEvidence 等旧驼峰名称保留作契约逻辑名称，Python 函数建议使用 read_material／search_evidence／save_suggestion／get_session_context；明确映射后调用，不据命名推断权限。JSON 仍保留 sessionId／sourceId 等既有字段，Python 内部命名不同须显式序列化映射；共享类型实现时同步构造样例与 C 的基准。

所有内部方法接收由 Host 构建的 context，至少包含 sessionId；Agent 运行中另绑定 runId、generation、预期 stateRevision。模型可见工具参数不包含自由切换的 sessionId，不包含 SQL、路径、权限身份或来源快照。

| 方法 | 模型／业务输入 | 返回给 B 的结果 | 失败和权限 |
| --- | --- | --- | --- |
| getSessionContext(context) | 当前绑定会话，无自由 SQL | Session、已保存问答、有效来源清单、当前稿、有效建议和未解决问题；不无条件传原附件字节 | session_not_found；只能取当前会话，不能把另一会话当记忆 |
| readMaterial(context, {sourceIds}) | 非空来源编号列表，每项为非空字符串 | data 为 SourceBlock 列表，即使只有一条也使用列表，含原文、定位及当前版本 | 任一编号缺失或跨会话则整次返回 source_not_found／scope_violation，不静默漏项；旧建议通过其保存版本读历史，不冒充最新来源 |
| searchEvidence(context, {query, limit}) | query 是去首尾空白后非空的字符串；limit 缺省为 5，若提供须为 1—10 的整数 | data 为 SourceBlock 列表；只查同会话且 role 为 resume／supplement 的已有来源，带原文／编号／角色／位置／版本；初版按 query 做普通子串匹配 | 空 query 或错误 limit 为 invalid_input；成功无匹配为 data=[]；unknown／JD 不当个人经历依据，不能用非 JD 就全部放行 |
| saveSuggestion(context, proposal) | operation、target、before／after、reason、sourceRefs、gaps | A 生成 suggestionId，存 pending，返回完整 Suggestion | stale_state／source_not_found／invalid_input；旧文与目标版本不一致就拒绝，不能更新正式稿 |
| saveIssues(context, issues) | 类型、说明、受影响块和来源 | A 生成编号，保存并返回 Issue | 来源与块须属本会话；记录问题不等于自动改正文 |

成功采用 `{ok: true, data: ...}`；失败采用 `{ok: false, error: {code, message, recoverable}}`。code 至少区分 invalid_input、session_not_found、source_not_found、scope_violation、stale_state、storage_failed、cancelled。错误可供 Agent 决定澄清或停止，但不允许以无限重试恢复。

searchEvidence 的初版目的只是找回用户已经给出的文字，不判断经历是否真实、不检索其他用户、不联网。需要解释 JD 时读 JD 来源，不把查询命中当作已匹配岗位的结论。

### 3.1 名称映射与返回结构

| 模型可见工具名 | B 负责调度到的 A 方法 | 说明 |
| --- | --- | --- |
| read_material | readMaterial | 下划线工具名与程序驼峰方法名是两层命名，明确映射，不要求模型猜 |
| search_evidence | searchEvidence | 绑定运行 context 后调用；模型只提供 query／limit，不自由指定会话 |
| propose_change | saveSuggestion | B 检查模型提议后交 A 保存 pending，不等于模型接受或更新正文 |

B 汇总映射，A 维护对应服务方法，二者变更要同步。单一名称不一致与这类显式映射不同；不能一方改名后让另一方自行猜测。

readMaterial／searchEvidence 成功返回 `data: SourceBlock[]`，getSessionContext 返回上下文对象，saveSuggestion 返回保存后的 Suggestion 对象，saveIssues 返回 Issue 列表。查询结果次序初版按 sourceId 升序截取 limit，以构造样例可重复检查；不是模型相关度评分。query 只作字符串数据，不解释为 SQL 或命令。错误只表示请求未按规则完成，不编造 data，也不把失败伪装为空列表。

## 4. 仅用户操作可触发的写入

| 用户动作 | A 的业务方法／接口 | 数据结果 |
| --- | --- | --- |
| 创建会话、发送消息 | createSession／saveMessage | 保存消息后增加上下文版本，B 才把它当新反馈 |
| 接受建议 | applySuggestionDecision({decision: accept, suggestionId, expectedStateRevision, expectedBlockRevision, requestId}) | 核对 pending 与版本，在同一事务里改稿、关联依据、改建议状态和版本，随后取消／失效旧运行 |
| 拒绝建议 | decision=reject | 不改正文，只记录拒绝；作为下一次 Agent 的反馈，不自动反复提同一建议 |
| 要求调整 | decision=revise，附非空 feedback | 记录用户反馈，使旧建议失效；B 稍后生成新建议，A 不在业务层替用户写新内容 |
| 手改完整稿 | updateDraft({markdown, expectedStateRevision, requestId}) | 改稿与旧建议失效同事务完成；标为 user_edit，不伪造来源 |
| 删除会话 | deleteSession，确认来自用户操作 | 取消运行，关联删除材料／消息／建议／稿件；迟到事件不重建会话；独立费用账本不删除 |

重复 requestId 的相同操作应返回已完成结果，不重复插入；同一 requestId 携带不同内容应拒绝。版本过期返回 stale_state，界面刷新最新稿后由用户决定，不能悄悄覆盖。

## 5. B 提供给 A 的运行能力

B 在同一个 Python 后端挂载 Agent 路由模块（建议 FastAPI APIRouter），直接调用 A 的受限 Python 服务方法；不另建 Agent 服务或第二套业务数据库。

- startAgentRun：输入会话、预期版本、requestId、触发事件；服务端从 SQLite 装配上下文，返回 runId 与事件流。
- cancelAgentRun：尽力取消指定运行，使旧 generation 失效；已发出的模型请求不保证免费。
- getBudgetStatus：只返回可展示的费用状态；A 不替 B 新开模型请求，预算阻断后仍可手改／导出。

事件 type 初版约定：progress、tool_progress、diagnosis、question、suggestion、conflict、done、error。每类 payload 要有明确结构；B 的模型输出先校验、保存，再给 A 正式事件；A 不显示未收全 JSON 为可接受建议。

| type | payload 最少包含 |
| --- | --- |
| progress | stage、message |
| tool_progress | toolName、phase（requested／completed／failed）、说明；不泄露密钥或任意数据 |
| diagnosis | issues，最多 3 项重要问题及理由 |
| question | messageId、content、relatedSourceIds；本轮等待用户时只问关键问题 |
| suggestion | suggestionId；完整对象可以一并返回，但必须对应数据库已存的 pending 建议 |
| conflict | issueId、affectedBlockIds、summary |
| done | outcome（waiting_user／completed／limited）、summary、unresolvedIssueIds；生成建议不等于用户已接受 |
| error | code、message、recoverable；已有成果保留 |

mode=mock 只供不付费联调；mode=live 只在实际模型运行时使用，不能通过改标签把构造响应变成真实结果。模拟工具进度也不能当作真实 MCP 调用证据。

## 6. 最小样例与预期行为

样例位置：`tests/fixtures/minimal-resume-case.json`；模拟事件：`tests/fixtures/minimal-resume-events.json`。使用说明与人工基准见同目录 README。以上都是构造数据，不是用户个人履历、真实 JD 或实测结果。

构造原文：三人团队开发校园报名工具，我负责需求整理，协助页面开发。\
构造补充：我整理了 8 条同学反馈，把重复意见合并成 5 项需求；未做 API 集成，没有上线数据。\
构造 JD：要求需求沟通、基础开发和 API 集成。

预期：允许依据补充写出反馈与需求数字，保留“协助开发”；API 集成仍是能力缺口；不增加“主导团队”、上线或收益。初始已分类测试状态只是方便测接口，不代表产品要求用户预分类。

样例的 search query 为“8 条同学反馈”，应只命中 supplement 来源，而非 JD。已备一条 pending 替换建议；正式稿在接受前必须仍为原文，接受后版本递增且依据可回看。拒绝不改稿；先手改再接受旧建议应被拒绝或失效。

## 7. 核对与状态

| 核对对象 | 需要确认 | 当前状态 |
| --- | --- | --- |
| A | 原文／来源／稿件／建议的区别；用户接受路径与模型工具的区别 | 已有字段与依据定位等学习证据，用户已授权本轮核对；助手完成本地检查，结果可供用户审阅，非接口实现 |
| B | 对象字段、工具输入输出、事件与版本规则能否供 Agent 使用 | 尚未收到 B 的确认；未自动联系 B |
| C | 允许事实、禁止编造、版本预期及样例是否可作人工基准 | 尚未收到 C 的确认；由助手先备构造材料 |
| 程序 | JSON、编号、引用、角色和模拟事件是否相互一致 | 本轮 Node 静态检查通过：5 组预期往返、3 个来源、5 条 mock 事件及 pending／拒绝预期一致；未执行产品、数据库或模型测试 |

草案和样例的本地基础核对可独立完成；团队回执与真实调用另记，不提前标为全组联调通过。

## 8. 本轮 A 侧核对记录（2026-10-20）

| 核对项 | 本地结论与处理 |
| --- | --- |
| 名称 | 补明确的模型工具名→程序方法映射，避免 snake_case／camelCase 造成误接 |
| 输入 | 明确 query 非空、limit 的类型与范围；当前会话由程序绑定 |
| 正常返回 | 两种读取／检索均返回来源列表；原文／编号／定位齐全，版本保留供后续实现 |
| 无结果与失败 | 正常空结果、参数错误、来源不存在和作用域错误有不同预期，见交接样例 |
| 权限 | 查询只读且会话受限；unknown 先澄清，JD 不当个人经历；保存建议不改正式稿 |
| 状态 | 样例保持 pending，当前稿=before；拒绝不改稿但应改变用户上下文版本，补齐对应人工预期 |
