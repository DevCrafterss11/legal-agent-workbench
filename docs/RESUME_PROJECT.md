# 简历项目 v5

原则：简历上出现的每一个数字都必须能在本仓库里一键复现，每一个技术词都必须有对应代码可指。拷打时护不住的表述一律不写。

## 项目条目（可直接粘贴进简历）

**企业法务 Agent 执行工作台**（独立设计开发）

技术栈：Python · Supervisor-Worker 多 Agent · RAG（BM25 + 向量混合检索，Milvus / BGE / bge-reranker）· Redis Streams · FastAPI / SSE · MCP · 飞书开放平台 · Ollama / OpenAI-compatible · pytest / GitHub Actions

面向企业合同审查场景，独立设计并实现"可运行、可追踪、可复核、可评测"的 Agent 执行平台：自研 ReviewRun 执行引擎与 Supervisor-Worker 多 Agent 编排，将合同解析、条款检索、风险判断、条款改写、合规复核、报告生成抽象为可插拔工具链，全链路 ToolCallTrace 审计；构建含隐式措辞对抗样本的分层 benchmark，rule-only / rag-only / full-system 三组 baseline 对照可一键复现。

- **Supervisor-Worker Agent Runtime 与受控 LLM 决策**：自研 ReviewRun 状态机，Supervisor 编排 Parser、Skill Planner、Evidence、Risk Reviewer、Clause Rewriter、Compliance Auditor、Report Writer、Memory Curator 八类子 Agent；在流程骨架确定性的前提下设置两个受控模型决策点——Skill Planner 动态决定检索深度与风险关注点、Evidence Agent 证据不足时改写查询重试（有界一次），决策输出经白名单与数值边界校验，解析失败/超时自动回落确定性规则并在 trace 中标记决策来源；支持 Ollama 与任意 OpenAI-compatible 模型，响应走 cache-aside 缓存。
- **合同条款混合检索 RAG 与两级重排**：BM25 + 向量多路召回，支持加权分数融合与 RRF（reciprocal rank fusion）两种融合策略；公式重排（可解释、零依赖）之上可配置 bge-reranker cross-encoder 对 top-32 候选精排；支持 Milvus + BGE 向量后端与扫描件 PDF 本地 OCR 接入，依赖缺失时逐级降级（Milvus→内存向量库、BGE→哈希 embedding、cross-encoder→公式重排、OCR→needs_ocr 标注）；RAG 定位为 Evidence Agent 的检索工具而非决策 Agent，检索结果作为风险结论的可追溯证据。
- **Redis Streams 异步任务总线**：基于 consumer group 投递审查任务，实现 at-least-once + 两层幂等消费（入队 SET NX 去重、消费按任务状态跳过重投）、有限重试 + 死信队列、崩溃消息按 idle 阈值认领重投、双 stream 两级优先级；任务表兼任本地消息表（outbox）先落盘再发布、发布失败补偿重投，解决双写不一致；Redis 不可用时投递、缓存、去重三条链路分别降级，主流程不中断。
- **三层记忆架构**：按生命周期与信任等级分层——工作记忆（ReviewRun 结构化共享状态，子 Agent 间传状态不传对话历史，token 成本不随步骤膨胀）、短期记忆（会话级阶段快照 + 长合同状态感知压缩：含风险条款保留细节、无风险条款截断限流，压缩率进 trace）、长期记忆（跨会话 LegalMemoryStore，完整生命周期：写入门槛、冲突强化、召回回写 use_count、180 天半衰期时间衰减、保留分容量驱逐与归档、run 级溯源）；只有带证据、过复核门槛的结论才能跨会话存活。
- **隐私边界、注入防御与权限治理**：合同明文仅存于本地信任边界，远端 LLM 调用前 PII 可逆脱敏（身份证校验码 / 银行卡 Luhn 确定性识别，映射仅留进程内存，响应本地回填），LLM 缓存 key 与值均基于脱敏文本、PII 不落 Redis，飞书回发单向脱敏；将合同建模为不可信输入实现 prompt injection 三层防御——入口模式检测（命中即审计 + 全部结论强制人工复核）、LLM 数据/指令隔离声明、无来源结论治理拦截兜底；Permission Guard 拦截绝对化判断与无证据结论。
- **FastAPI 工作台与企业系统集成**：FastAPI + uvicorn 服务层，阻塞审查链路走线程池、SSE 事件流用 asyncio 实时推送 Agent 审查进度；接入飞书开放平台（WebSocket 长连接 + HTTP 回调双模式），机器人可接收合同文本、文档链接及 PDF / DOCX 附件并回发审查报告，事件按 message_id 幂等去重；MCP connector 层对接飞书文档、Notion、OA 等企业系统。
- **对抗性评测体系与工程质量**：重建合成 benchmark 加入隐式措辞对抗样本（规则关键词全部失效）与已知失败样本（规则与检索双漏，保留标记改进方向），配合 30 份人工标注合同（120 条条款级风险标签）形成分层评测：风险条款 Recall@10 呈 rule-only 0.48 → rag-only 0.91 → full-system 0.95 梯度，量化规则引擎与语义检索的互补增益，全部结果一键复现；61 个自动化测试覆盖 Agent Runtime、决策边界、三层记忆、PII 脱敏回路、注入防御、检索融合、消息投递语义与故障降级路径，GitHub Actions CI 含 baseline 顺序性断言。

## 指标口径（面试前自己先跑一遍）

| 简历中的数字 | 出处 | 复现方式 |
| --- | --- | --- |
| 100 份公开合同语料 | `data/common_contracts/` | `python scripts/build_common_contract_corpus.py --limit 100` |
| 30 份人工标注合同、120 条风险标签 | human benchmark | `legal-agent eval --human` |
| Recall@10：rule 0.48 → rag 0.91 → full 0.95（合成集） | baseline 对照 | `legal-agent eval --scaled && legal-agent eval-baseline --dataset both` |
| 人工标注集 rule 0.90 → full 1.00 | baseline 对照 | 同上 |
| 61 个自动化测试 | `tests/` | `python -m pytest tests/ -q` |

条款知识数量随语料重建浮动（当前 1400+），简历不写精确值。

## 已删除的旧表述与原因

- ~~"Recall@10 从 58% 提升至 87%"~~：与实际 eval 输出冲突。
- ~~"报告来源覆盖率 95%+"、"工具调用成功率 98%+"~~：无测量口径。
- ~~"初审时间由 30-60 分钟缩短至 3-5 分钟"~~：无对照实验，口头可用"分钟级"定性表达并说明是估算。
- ~~合成集 recall 1.00~~：已通过对抗样本重建为 0.95，附一个规则与检索双漏的已知失败样本——"系统知道自己哪里不行"比满分更可信。

## 面试注意

- 人工标注集 full=1.00 仍会被问：答"该集合覆盖的风险表达较规整，合成对抗集才是压力测试；下一步是往人工集补充隐式措辞标注"。
- 每个 bullet 的深挖题与答案见 `docs/INTERVIEW_QA.md`，重点章节：三层记忆、Skill 机制、Prompt Injection、隐私边界、长合同/成本/失败恢复、飞书 MCP、LLM 决策点、Redis 任务总线、harness 概念对照。
- 讲记忆先讲三层划分（工作/短期/长期 + 信任边界），再讲长期记忆五环节生命周期——这是当前面试官最认的叙述顺序。
- 讲 Skill 主动提 SKILL.md frontmatter 与主流 harness 同构，这是"跟得上最新技术"的直接信号。
