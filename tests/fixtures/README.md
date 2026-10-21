# A-T02 最小构造样例

当前目录只有构造数据，不是真实简历、真实招聘 JD、模型输出或测试报告。没有 API 调用或个人信息。JSON 中固定编号和时间只是测试输入。

- `minimal-resume-case.json`：已分类的一个会话快照、原文、用户补充、JD、来源、初始稿、一条 pending 建议、一个未解决能力缺口与人工预期。
- `minimal-resume-events.json`：与上述快照对应的 5 条 mock 事件，供后续页面显示；没有真正调用 search_evidence 或 MCP。
- `minimal-tool-exchanges.json`：五组读取／查询的预期请求与返回，包含命中、空结果、空关键词、读取单条和来源缺失；只是交接样例，不是接口实测。
- 契约说明：[A/B 接口草案](../../docs/superpowers/contracts/2026-10-19-a-b-contract.md)。

人工事实基准：仅参与三人团队、负责需求整理、整理 8 条反馈归纳 5 项需求、协助页面开发；未做 API 集成、无上线数据。允许的改写应保持这些边界，不补造主导职责或收益。

每个交互场景从原始快照独立开始，不把“接受→拒绝→手改”当作同一建议的连续合法状态：

1. 未接受：正文为 suggestion.before，三个版本均为 1。
2. 接受：正文变为 suggestion.after，建议为 accepted；stateRevision、draftRevision、blockRevision 均变为 2，来源仍可查。
3. 拒绝：正文与稿件／块版本保持原值，建议为 rejected；stateRevision 增至 2，因为用户决定影响后续 Agent，旧运行应失效，不能自动反复提议。
4. 要求调整：旧建议失效并记录用户反馈，B 生成新建议；A 不在业务接口自行润色。
5. 先编辑：旧建议失效或返回 stale_state，不能覆盖用户新稿。

本目录文件没有导入数据库，没有执行产品行为。当前只检查 JSON 可读、编号一致、引用可定位、模拟事件顺序与预期相容。待 A-T03 起建立工程，再编写有意义的运行时校验和业务测试。
