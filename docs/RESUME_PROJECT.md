# 简历项目 v3

原则：简历上出现的每一个数字都必须能在本仓库里一键复现，每一个技术词都必须有对应代码可指。拷打时护不住的表述一律不写。

## 项目条目（可直接粘贴进简历）

**企业法务 Agent 执行工作台**（独立设计开发）

技术栈：Python · Supervisor-Worker 多 Agent · RAG（BM25 + 向量混合检索，Milvus / BGE / bge-reranker）· Redis Streams · FastAPI / SSE · MCP · 飞书开放平台 · Ollama / OpenAI-compatible · pytest / GitHub Actions

面向企业合同审查场景，独立设计并实现"可运行、可追踪、可复核、可评测"的 Agent 执行平台：自研 ReviewRun 执行引擎与 Supervisor-Worker 多 Agent 编排，将合同解析、条款检索、风险判断、条款改写、合规复核、报告生成抽象为可插拔工具链，全链路 ToolCallTrace 审计；构建含隐式措辞对抗样本的分层 benchmark，rule-only / rag-only / full-system 三组 baseline 对照可一键复现。

- **Supervisor-Worker Agent Runtime 与受控 LLM 决策**：自研 ReviewRun 状态机，Supervisor 编排 Parser、Skill Planner、Evidence、Risk Reviewer、Clause Rewriter、Compliance Auditor、Report Writer、Memory Curator 八类子 Agent；在流程骨架确定性的前提下设置两个受控模型决策点——Skill Planner 动态决定检索深度与风险关注点、Evidence Agent 证据不足时改写查询重试（有界一次），决策输出经白名单与数值边界校验，解析失败/超时自动回落确定性规则并在 trace 中标记决策来源；支持 Ollama 与任意 OpenAI-compatible 模型，响应走 cache-aside 缓存。
- **合同条款混合检索 RAG 与两级重排**：BM25 + 语义相似度 + metadata boost 混合召回，公式重排（可解释、零依赖）之上可配置 bge-reranker cross-encoder 对 top-32 候选精排；支持 Milvus + BGE 向量后端，依赖缺失时逐级降级（Milvus→内存向量库、BGE→哈希 embedding、cross-encoder→公式重排）；RAG 定位为 Evidence Agent 的检索工具而非决策 Agent，检索结果作为风险结论的可追溯证据。
- **Redis Streams 异步任务总线**：基于 consumer group 投递审查任务，实现 at-least-once + 两层幂等消费（入队 SET NX 去重、消费按任务状态跳过重投）、有限重试 + 死信队列、崩溃消息按 idle 阈值认领重投、双 stream 两级优先级；任务表兼任本地消息表（outbox）先落盘再发布、发布失败补偿重投，解决双写不一致；Redis 不可用时投递、缓存、去重三条链路分别降级，主流程不中断。
- **权限治理与长期记忆**：Permission Guard 拦截无来源法律结论、绝对化判断与敏感外部导出，规则兜底命中但缺检索证据的风险强制标记人工复核，"有判断没证据"是显式状态；四类审查记忆（semantic / episodic / procedural / preference）按合同类型与风险语义召回历史经验并按复核状态沉淀。
- **FastAPI 工作台与企业系统集成**：FastAPI + uvicorn 服务层，阻塞审查链路走线程池、SSE 事件流用 asyncio 实时推送 Agent 审查进度；接入飞书开放平台（WebSocket 长连接 + HTTP 回调双模式），机器人可接收合同文本、文档链接及 PDF / DOCX 附件并回发审查报告，事件按 message_id 幂等去重；MCP connector 层对接飞书文档、Notion、OA 等企业系统。
- **对抗性评测体系与工程质量**：重建合成 benchmark 加入隐式措辞对抗样本（规则关键词全部失效）与已知失败样本（规则与检索双漏，保留标记改进方向），配合 30 份人工标注合同（120 条条款级风险标签）形成分层评测：风险条款 Recall@10 呈 rule-only 0.48 → rag-only 0.91 → full-system 0.95 梯度，量化规则引擎与语义检索的互补增益，全部结果一键复现；47 个自动化测试覆盖 Agent Runtime、决策边界、检索、消息投递语义与故障降级路径，GitHub Actions CI 含 baseline 顺序性断言。

## 指标口径（面试前自己先跑一遍）

| 简历中的数字 | 出处 | 复现方式 |
| --- | --- | --- |
| 100 份公开合同语料 | `data/common_contracts/` | `python scripts/build_common_contract_corpus.py --limit 100` |
| 30 份人工标注合同、120 条风险标签 | human benchmark | `legal-agent eval --human` |
| Recall@10：rule 0.48 → rag 0.91 → full 0.95（合成集） | baseline 对照 | `legal-agent eval --scaled && legal-agent eval-baseline --dataset both` |
| 人工标注集 rule 0.90 → full 1.00 | baseline 对照 | 同上 |
| 47 个自动化测试 | `tests/` | `python -m pytest tests/ -q` |

条款知识数量随语料重建浮动（当前 1400+），简历不写精确值。

## 已删除的旧表述与原因

- ~~"Recall@10 从 58% 提升至 87%"~~：与实际 eval 输出冲突。
- ~~"报告来源覆盖率 95%+"、"工具调用成功率 98%+"~~：无测量口径。
- ~~"初审时间由 30-60 分钟缩短至 3-5 分钟"~~：无对照实验，口头可用"分钟级"定性表达并说明是估算。
- ~~合成集 recall 1.00~~：已通过对抗样本重建为 0.95，附一个规则与检索双漏的已知失败样本——"系统知道自己哪里不行"比满分更可信。

## 面试注意

- 人工标注集 full=1.00 仍会被问：答"该集合覆盖的风险表达较规整，合成对抗集才是压力测试；下一步是往人工集补充隐式措辞标注"。
- 每个 bullet 的深挖题与答案见 `docs/INTERVIEW_QA.md`：新增"深挖拷打：LLM 决策点、评测区分度与 Web 层"与"后端拷打：Redis 任务总线与缓存"两节。
