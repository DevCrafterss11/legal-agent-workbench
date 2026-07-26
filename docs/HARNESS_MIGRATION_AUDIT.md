# Harness 能力迁移自查

本项目不是 OpenHarness 的二次包装，而是把通用 Agent Harness 思想迁移到企业法务场景后的独立工程实现。

## 已迁移并场景化

| Harness 能力 | 法务项目实现 | 当前状态 |
| --- | --- | --- |
| Agent Runtime / Loop | `ReviewRun` 状态机，执行 parse、retrieve、risk_check、rewrite、compliance、report | 已实现 |
| Tool Registry | `legalworkbench.tools.ToolRegistry`，统一 trace 工具调用 | 已实现 |
| Permission Checker | 敏感合同、高风险建议、无来源判断、外部导出治理 | 已实现 |
| Memory | semantic、episodic、procedural、preference 审查记忆 | 已实现 |
| Skills | SaaS、采购、NDA 审查技能，支持 JSON 与 `SKILL.md` frontmatter 加载 | 已实现 |
| MCP | 飞书/Notion/OA/合同库 connector contract + mock MCP discovery | 半实现，真实 SDK client 可接入 |
| Session Storage | 每次审查保存 session 快照、latest、run JSON、报告 | 已实现 |
| Dashboard | Web 工作台 + dashboard JSON/HTML | 已实现 |
| Benchmark | 风险召回、来源覆盖、工具成功、记忆召回、拦截率 | 已实现 |
| Hooks | review/tool/risk 事件写入 `events.jsonl` | 已实现 |
| Compact | 长合同状态感知压缩快照，保留条款、风险、来源和状态 | 已实现 |
| Token Estimation | 合同、证据、记忆、compact token 估算 | 已实现 |
| Reflection | 对来源缺失、高风险建议、绝对化判断二次复核 | 已实现 |

## 仍可继续迁移

| 可迁能力 | 迁移方式 | 优先级 |
| --- | --- | --- |
| MCP Tool Invocation | 当前已支持 SDK catalog discovery，后续补 call_tool/read_resource 业务调用 | 高 |
| Plugin Loader | 支持 `.lawbench/plugins/*/plugin.json`，动态加载企业内部合同审查包 | 中 |
| Async Task Manager | 当前支持 worker 执行 pending task，后续补取消、重试、并发调度 | 中 |
| Cost Tracker | 真实 LLM token/cost 汇总，而不是估算值 | 中 |
| Hook Executor | Hook 不只写日志，还能触发写飞书、发通知、创建审批 | 中 |
| Auth / Secret Redaction | 企业 API token 管理、敏感配置脱敏显示 | 高 |
| Worktree/Sandbox 思想 | 对合同处理脚本和外部工具执行做隔离，不直接影响生产数据 | 低 |

## 不建议直接迁移

- OpenHarness 的通用 coding CLI、模型 provider、代码编辑工具，不符合企业法务工作台主线。
- Autopilot 的 GitHub issue/PR 自动化，不应照搬；更适合改造成“合同审查任务队列”。
- Shell/sandbox 相关能力只保留思想即可，法务系统里优先关注外部系统写入权限和数据审计。

## 当前真实实现度

- 简历里 Runtime、Tool Registry、RAG、规则、Memory、Skills、Workflow、Permission、Session、Dashboard、Benchmark 已有代码支撑。
- `BM25 + 向量相似度` 当前实现为 BM25 + deterministic semantic overlap proxy，适合本地演示；上线版可替换为 BGE/Milvus。
- `LLM 语义判断` 当前实现为可解释 semantic score 和 confidence，尚未接真实 LLM。
- `MCP` 已支持官方 SDK stdio/http catalog discovery；真实飞书/Notion 账号仍需配置企业凭证。
- `Skills` 已支持 `.lawbench/skills/*/SKILL.md` frontmatter 加载，也保留 `skills.json` 兼容入口。
- `RAG` 已抽象为 `LegalRagService`，支持 local vector store 与 Milvus adapter；本地无 Milvus 时自动 fallback。
- `LLM` 已抽象为 OpenAI-compatible `LlmClient`，未配置外部模型时使用 deterministic fallback。
- `Web` 已支持合同上传、合同库审查、Skill 管理、MCP 配置、RAG/Milvus 配置、任务队列、worker 执行与审计事件查看。
- `80+ 合同 / 300+ 风险条款 benchmark` 可通过 `legal-agent eval --scaled` 生成确定性 benchmark；真实业务数据仍需后续补充。
