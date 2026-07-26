# Interview QA

这份文档用于准备“企业法务 Agent 执行工作台”的面试答辩。回答原则：

- 先讲业务问题，再讲技术方案。
- 先承认边界，再说明工程取舍。
- 不把系统吹成“替代律师”，而是定位为“企业合同初审与风险辅助工作台”。
- 指标要能解释来源：standard benchmark、human benchmark、RAG health、工具调用 trace。

## 60 秒项目介绍

我做的是一个面向企业合同审查场景的 Agent 执行工作台。它不是简单的 RAG 问答，而是把一次合同审查建模成一个可追踪的 `ReviewRun`：从合同上传、文档解析、条款切分、RAG 证据检索、规则引擎风险判断、条款修改建议、合规复核，到最终报告生成，每一步都有工具调用记录和审计 trace。

架构上采用 Supervisor-Worker 多 Agent 模式：主 Agent 负责任务编排，Parser、Evidence、Risk Reviewer、Clause Rewriter、Compliance Auditor、Report Writer、Memory Curator 等子 Agent 各自负责专职任务。RAG 使用 BM25 + 向量检索 + rerank，支持 Milvus + BGE，也保留本地 fallback。系统还设计了长期审查记忆、Skills、Permission Guard、任务队列、飞书 / MCP 企业连接和 benchmark 评测。

当前项目已经构建 100 份公开合同语料、1400+ 条知识索引（精确条数以 `legal-agent rag-status` 为准）、30 份人工标注格式合同和 120 条条款级风险标签。human benchmark 结果中，RAG + Rule 的 `risk_recall_at_10=1.0`，规则单独召回 `rule_recall=0.9`，说明隐性表达需要 RAG 证据补足。

## 项目定位

**Q：这个项目到底解决什么问题？**

A：解决企业合同初审中三个痛点：

- 合同长、类型多，人工逐条看风险耗时。
- Agent 长任务容易丢上下文，回答缺少来源，合规风险高。
- 企业内部标准、历史审查意见、飞书/Notion/OA 等系统分散，不能形成闭环。

所以我做的不是法律聊天机器人，而是一个可控执行的合同审查 Agent Runtime。

**Q：为什么选择企业法务场景？会不会很普通？**

A：合同审查是一个适合 Agent 工程化落地的场景，因为它天然包含长文档解析、知识检索、规则判断、多轮推理、人工复核、报告输出和审计追踪。普通的 RAG 问答只能回答“某条款是什么意思”，但企业真正需要的是“这份合同有哪些风险、依据是什么、怎么改、谁复核、能不能追溯”。

我的差异点在于把 OpenHarness 类 Agent Runtime 思想迁移到了法务工作流里，强调工具链编排、权限治理、记忆沉淀、评测和可观测性。

**Q：它能替代法务吗？**

A：不能，也不应该这么定位。它是合同初审和风险辅助系统。高风险条款、无充分来源的法律判断、绝对化结论都会进入人工复核。系统目标是提升初审效率和标准化程度，而不是替代律师或法务最终判断。

## 架构设计

**Q：整体架构怎么设计？**

A：核心是 `LegalAgentRuntime` + `LegalReviewSupervisor`。外部入口包括 Web、CLI、飞书事件和任务队列。一次审查会生成 `review_run_id`，主 Agent 推进状态：

```text
parse -> skill_plan -> retrieve -> risk_check -> rewrite -> compliance_review -> report -> memory_curate
```

每个阶段由专职子 Agent 完成，并通过 `ReviewRun` 共享结构化状态。所有工具调用写入 `ToolCallTrace`，报告和 session 会持久化到 `.lawbench/`。

**Q：为什么不用一个大 Agent 全部做完？**

A：单 Agent 容易出现职责混杂、上下文污染和结果不可追踪。合同审查里每个阶段的输入输出都比较明确，所以我用 Supervisor-Worker：

- Parser 只负责合同类型和条款切分。
- Evidence 只负责检索 RAG 和 Memory。
- Risk Reviewer 负责风险识别。
- Rewriter 负责修改建议。
- Compliance Auditor 负责权限和合规复核。
- Report Writer 负责结构化报告。

这样更容易做 trace、评测和失败定位。

**Q：Agent 之间怎么通信？**

A：不是自然语言互聊，而是通过共享的 `ReviewRun` 结构化状态通信。子 Agent 把结果写入 clauses、findings、memory_hits、tool_calls、reflection_checks 等字段。这样更稳定，也方便持久化和审计。

**Q：为什么 RAG 不单独做成一个 Agent？**

A：我把 RAG 定位为 Evidence Agent 的底层能力，而不是决策 Agent。RAG 只负责召回证据，不直接给最终法律结论。最终风险由 Risk Reviewer 结合规则和证据判断，再由 Compliance Auditor 复核。这样可以降低“检索到了就当结论”的风险。

## RAG

**Q：RAG 具体怎么做？**

A：知识库包含合同模板、风险条款库、历史审查意见、企业红线规则和常见法务问答。条款按合同类型、条款类别、风险类型、风险等级、来源等元数据组织。召回层采用 BM25 + 向量检索 + rerank：

- BM25 适合精确法律术语、条款关键词。
- 向量检索适合语义相近但表述不同的隐性风险。
- rerank 结合语义、风险等级、来源可信度和合同类型。

当前支持 Milvus + BGE，也支持本地 hashing / in-memory fallback，保证本地演示可运行。

**Q：为什么用 Milvus？**

A：Milvus 更适合向量检索生产化：支持大规模向量、collection 管理、索引、过滤、批量 upsert 和后续扩展。合同条款库会持续增长，后续可能有企业历史合同、制度文档、历史审查意见，单纯内存检索不适合生产。

本地版本保留 fallback 是为了开箱即用；生产环境用 Milvus 是为了容量、检索性能和可维护性。

**Q：为什么还要 BM25，不直接向量检索？**

A：法律合同里有大量精确术语，比如“自动续约”“责任上限”“预期利润损失”“管辖法院”。这些词一旦出现，BM25 很可靠。向量检索擅长隐性表达，但可能召回语义接近却不够精确的内容。所以混合召回比单一向量检索更稳。

**Q：BGE 有什么作用？**

A：BGE 用来生成中文语义向量，提升对隐性表达和长尾表达的召回。例如“不设赔偿责任上限”很容易规则命中，但“承担开放式补偿义务，补偿金额不设最高金额限制”更依赖语义检索。human benchmark 里特意加入了 12 条这种 hard paraphrase。

**Q：RAG 怎么避免幻觉？**

A：系统不是让 LLM 自由生成法律结论，而是要求每个风险发现带 evidence。Permission Guard 会拦截无来源判断；报告里也会标注证据来源。高风险建议会要求人工复核。

## 风险规则引擎

**Q：为什么有 RAG 了还要规则引擎？**

A：规则引擎适合高确定性风险，比如无限责任、自动续约缺少通知、付款未绑定验收、管辖地不利等。规则有三个作用：

- 对高频风险做稳定命中。
- 降低模型漏检。
- 给合规复核提供可解释依据。

RAG 补足规则覆盖不到的隐性表达和长尾场景。

**Q：规则覆盖哪些风险？**

A：当前覆盖无限责任、自动续约、数据安全、付款验收、付款周期、知识产权归属、SLA 补救、管辖地、保密范围、解除通知、不可抗力、押金返还、预付退款等。

**Q：规则会不会误报？**

A：会，所以规则结果不会直接变成最终结论。Risk Reviewer 会结合 RAG 证据，Compliance Auditor 会复核来源和风险等级。后续生产可以通过人工反馈和 benchmark 监控误报率。

## Memory

**Q：Memory 设计了什么？**

A：设计了四类：

- semantic：企业通用制度、风险定义。
- episodic：历史审查案例和处理方式。
- procedural：审查流程和检查清单。
- preference：企业偏好，比如责任上限、付款周期、管辖地倾向。

审查时根据合同类型、风险类型和条款语义召回记忆；审查完成后由 Memory Curator 沉淀可靠结果。

**Q：Memory 和 RAG 有什么区别？**

A：RAG 更像知识库，回答“条款风险依据是什么”。Memory 更像企业经验，回答“我们过去怎么处理类似条款、企业偏好是什么”。例如 RAG 告诉系统无限责任要设上限，Memory 会告诉系统“历史 SaaS 协议通常改成近 12 个月服务费上限”。

**Q：怎么避免错误记忆污染？**

A：写入 Memory 有门槛：

- blocked 的结果不写。
- 无来源证据不写。
- 高风险且需要人工复核的结论不会直接标记为人工批准。
- Memory 会保留来源 run_id、置信度和是否人工批准。

生产环境还应加入人工审核、过期策略和冲突检测。

**Q：Memory 召回指标怎么测？**

A：当前 standard benchmark 会计算 `memory_recall_at_5`，看相关 query 是否能召回历史经验。后续更真实的评测会构造“同类合同复审”任务，测试系统能否复用之前的审查偏好和改写模板。

## Skills

**Q：Skills 是什么？**

A：Skills 是不同合同类型的审查策略配置。比如 SaaS Skill 关注责任上限、数据安全、SLA 和自动续约；采购合同 Skill 关注付款、验收、交付成果和知识产权。Skill 不替代 Runtime，而是给同一套 Runtime 注入不同合同类型的检查重点。

**Q：为什么不用 prompt 里写死？**

A：写死会导致合同类型扩展困难。用 Skill 后，可以新增 NDA、采购、SaaS、劳动合同等策略，而不改 Runtime 主流程。

## Permission Guard 和合规

**Q：Permission Guard 做什么？**

A：控制三类风险：

- 无来源法律判断不允许输出。
- 高风险建议必须进入人工复核。
- 敏感信息导出、外部写入等高风险工具调用需要拦截或审批。

**Q：为什么强调合规？**

A：法务场景输出的不是普通闲聊内容，错误建议可能导致合同风险。企业系统必须能说明“为什么这样建议、依据是什么、是否需要人工复核”。

**Q：RBAC、脱敏、OCR 现在做到哪一步？**

A：当前项目已经有权限拦截和敏感配置脱敏展示，但还不是完整企业 RBAC。PDF/DOCX 文本抽取已支持，扫描件 OCR、复杂表格还需要增强。真实上线还需要 RBAC、数据脱敏、审批系统和审计平台接入。

## MCP 和飞书

**Q：MCP 在这个项目里有什么用？**

A：MCP 是企业系统连接层，不是上传按钮。它可以让 Agent 读取飞书文档、查询 Notion playbook、写回报告、创建审批任务、写审计日志。这样合同审查不只停留在本地页面，而是进入企业真实工作流。

**Q：飞书机器人和 Lark MCP 有什么区别？**

A：飞书机器人是用户交互入口，负责接收消息和附件。Lark MCP 是工具调用层，负责让 Agent 以标准方式发现和调用飞书开放平台工具，比如读文档、发消息、创建任务。一个偏交互入口，一个偏工具协议。

**Q：为什么本地还有 mock connector？**

A：为了保证本地可运行。真实企业凭证不稳定，也不适合写进项目。mock connector 保证 Runtime 和工具协议可演示；配置真实 App ID/Secret 后可以接飞书 OpenAPI 和 MCP。

## 任务队列

**Q：为什么需要任务队列？**

A：合同审查可能包含 PDF/DOCX 解析、OCR、RAG 检索、多 Agent 审查和报告生成，耗时不稳定。同步请求容易超时，用户体验差。任务队列用于把大文件、批量合同、飞书附件放到后台处理。

**Q：现在为什么用 file-backed queue？**

A：MVP 阶段优先保证本地可运行和可调试，所以用 `.lawbench/tasks.json` 存任务。它支持 pending/running/completed/failed、attempts、report_path 等基本状态，能解释异步流程。

**Q：为什么不直接上 Redis/RQ/Celery？**

A：Redis/RQ 是生产增强，不是 MVP 必需。当前 file queue 的价值是降低部署门槛；生产环境可以替换为 Redis + RQ，让多个 worker 消费任务、支持失败重试和并发执行。Celery 更强但配置更重，当前合同审查任务用 RQ 更容易解释。

**Q：Kafka 合适吗？**

A：不优先。Kafka 适合高吞吐事件流，合同审查更像后台任务队列，需要任务状态、重试和 worker 执行结果。Redis/RQ 或 Celery 更贴近场景。

## Benchmark 和指标

**Q：指标怎么来的？**

A：项目里有两类评测：

- standard benchmark：120 cases，验证基础 RAG、规则、Memory、工具链路。
- human benchmark：30 份合同、120 条条款级人工标注格式风险标签，其中 12 条是隐性表达。

当前 human benchmark：

```text
contracts: 30
annotated_risks: 120
risk_recall_at_10: 1.0
rule_recall: 0.9
source_coverage_at_10: 1.0
high_risk_recall: 1.0
human_review_capture_rate: 1.0
```

**Q：为什么 rule_recall 不是 1.0？**

A：因为 human benchmark 里故意加入了 12 条隐性表达，不完全依赖关键词。例如“开放式补偿义务，补偿金额不设最高金额限制”不一定命中规则，但 RAG 可以通过语义证据召回无限责任风险。这说明 RAG + Rule 比 Rule-only 更稳。

**Q：这些指标会不会太高？**

A：这是 v1 种子集，用于验证链路和回归，不等同真实生产评测。我不会把它包装成真实企业线上准确率。真实上线需要接企业历史合同、双人标注、一致性检查和争议仲裁流程，再持续监控误报和漏报。

**Q：为什么测 Recall@10？**

A：合同审查更怕漏掉高风险条款，所以召回率优先于精确率。Recall@10 表示在前 10 个证据或风险候选中能否覆盖人工标注风险。后续生产也会补 precision、false positive rate 和人工采纳率。

**Q：source_coverage 测什么？**

A：测风险判断是否有证据来源。法律场景不能只看“答得像不像”，还要看是否能追溯到制度、playbook 或历史审查依据。

## 前端和产品体验

**Q：前端工作台有哪些功能？**

A：支持合同上传、粘贴审查、合同库、任务队列、审查记录、报告查看、工作流、审计事件、RAG/Milvus 配置、Skills 配置、飞书/Lark MCP 配置和企业连接器查看。

**Q：为什么做 Web，不只做 CLI？**

A：法务和业务用户不会只用 CLI。Web 工作台能展示任务状态、风险数量、工具调用、Memory 命中和报告结果，更贴近企业真实使用场景。CLI 保留给开发和部署运维。

## 工程取舍

**Q：本地文件存储够吗？**

A：MVP 够，本地演示和面试很方便。但生产环境要替换为数据库和对象存储：

- PostgreSQL 存 run、task、annotation、permission、audit。
- 对象存储保存合同原文和报告。
- Redis/RQ 或 Celery 承接异步任务。
- Milvus 存向量索引。

**Q：为什么不是直接用 LangChain / LlamaIndex 全家桶？**

A：这个项目重点是展示 Agent Runtime 工程能力，所以我把核心流程、工具注册、trace、权限、Memory、任务队列和评测显式实现出来。RAG 可以接 LlamaIndex，但 Runtime 不依赖某个框架，方便解释和替换。

**Q：最大技术难点是什么？**

A：不是某个模型调用，而是长任务的可控执行。合同审查涉及长文档、多工具、多轮证据、多 Agent、权限和最终报告。如果没有 ReviewRun、ToolCallTrace、Permission Guard 和 benchmark，很难知道系统为什么这么答，也难以定位错误。

## 生产化边界

**Q：现在离真实上线还差什么？**

A：主要差：

- 企业真实合同和历史审查意见接入。
- 法务双人标注与一致性评估。
- RBAC、多租户、数据脱敏、审计平台。
- OCR、复杂 PDF 表格和版式还原。
- Redis/RQ 或 Celery 分布式任务队列。
- PostgreSQL / 对象存储替代本地文件。
- 线上模型 API、限流、监控和成本控制。

**Q：如果给你两周继续做，优先做什么？**

A：

1. 报告页 Markdown / 风险卡片渲染。
2. Redis + RQ task queue adapter，保留 file queue fallback。
3. RBAC + 脱敏审计。
4. 接真实企业合同样本，扩充 human benchmark。
5. OCR 和复杂 PDF 表格解析。

## 容易被问倒的点

**Q：你的准确率能代表真实企业合同吗？**

A：不能完全代表。当前是 v1 种子集和公开合同语料，能证明工程链路和评测框架成立。真实准确率必须基于企业历史合同、人工标注和持续回归评测。

**Q：你用了大模型吗？**

A：当前项目重点是 Agent Runtime、RAG、规则、Memory 和可观测链路，LLM 客户端是 OpenAI-compatible 的可替换接口。为了本地稳定演示，很多能力有 deterministic fallback。生产可以接真实模型 API。

**Q：为什么有些指标全是 1.0？**

A：standard benchmark 是确定性回归集，目标是防止功能退化，不是线上泛化评估。human benchmark 加了隐性表达，所以 rule-only 不是满分；后续应继续扩大真实标注集，让指标更接近真实分布。

**Q：项目是不是套壳 OpenHarness？**

A：不是直接套壳。迁移的是思想：Agent Runtime、Tool Registry、Permission、Memory、Skills、Session、Dashboard、MCP、Task Queue。代码已经按企业法务场景重构成独立项目，包名、领域模型、工具、评测和前端都围绕合同审查。

## 深挖拷打：LLM 决策点、评测区分度与 Web 层

**Q：你的系统哪里是模型在决策，哪里是写死的？为什么这样划分？**

A：明确分层。确定性的：审查流程骨架（Parser→Evidence→Reviewer→Rewriter→Auditor 的阶段顺序）、风险规则、权限拦截——这些是合规要求和企业配置，不该交给模型。模型决策的有两处：Skill Planner 根据条款标题分布决定检索深度和补充风险关注点；Evidence Agent 在证据不足时决定是否改写查询重试。关键设计是"模型给方向、代码守边界"：top_k 被强制夹在 [5,20]，风险类型只接受白名单，重试循环上界写死为 1 次——模型不能决定"再来多少轮"。测试里专门有一条：模型返回 top_k=99 和幻觉风险类型时，运行结果必须是 20 和白名单过滤后的集合。

**Q：模型挂了或者输出乱七八糟怎么办？**

A：所有决策走统一的 `decide()` 接口：输出先过 JSON 提取（容忍 markdown 代码块和前后缀废话），解析失败、网络超时、服务不可用都回落到确定性 fallback，并在决策结果里标记 `decision_source`（model / local_rules / fallback），trace 里可以看到每次决策实际是谁做的。审查主链路永远不因决策层退化而中断。

**Q：你的 benchmark 全是 1.00，是不是评测集和规则同源？**

A：这个问题我主动处理过。早期合成集确实饱和，所以我重建了它：加入一组隐式措辞 hard 样本——风险语义存在但刻意避开规则关键词（比如"补偿金额不受合同总额约束"代替"不设赔偿责任上限"），规则引擎全部漏检；再加一个"存续/延展"表达的自动续约样本，规则和检索都漏，作为已知失败案例保留。现在的梯度是 rule 0.48 / rag 0.91 / full 0.95：每一层的增益可测量，且系统没有满分。

**Q：full_system 比 rag_only 好在哪？0.91 到 0.95 的差从哪来的？**

A：来自一个具体机制：知识库对不同风险类型覆盖不均（保密条款只有个位数条目，付款条款有 450+），多风险合同里稀疏类型会被挤出 top-10，检索漏掉；但规则引擎按关键词兜底命中。反过来，隐式措辞规则漏、检索中。两者并集才是 0.95。而且规则兜底命中的风险没有检索证据，source coverage 仍是 0.91，这类结论会被 Permission Guard 标记强制人工复核——"有判断没证据"在这个系统里是显式状态，不是被掩盖的。

**Q：rerank 用的什么？**

A：两级。第一级是公式重排：检索分 × 0.55 + 语义重叠 × 8 + 来源可信度 boost + 合同类型匹配，零依赖、可解释、延迟微秒级。第二级可配置 bge-reranker cross-encoder，对 top-32 候选做 query-doc 联合编码精排，依赖缺失自动降级第一级并在 rag-status 暴露原因。选型逻辑：公式解决"分数可解释"，交叉编码解决"深层语义匹配"，按部署环境选择。

**Q：Web 层为什么用 FastAPI？异步体现在哪里？**

A：业务端点是阻塞型（审查是 CPU + IO 混合的同步链路），用 def 定义交给 FastAPI 的线程池，不阻塞事件循环；SSE 事件流是 async def，用 asyncio.to_thread 把文件型事件总线的读取放到线程池，每秒轮询增量推送，空闲发心跳。浏览器端 EventSource 订阅，审查过程中的每个 Agent 事件（检索完成、风险发现、复核通过）实时上屏。没有为了"全异步"把同步审查链路强行改成 async——那只会把线程池换个名字，我可以解释这个取舍。

## 后端拷打：Redis 任务总线与缓存

这一节的每个回答都有对应代码可指：任务总线在 `src/legalworkbench/mq/bus.py`，消费侧在 `src/legalworkbench/tasks/worker.py`，缓存在 `src/legalworkbench/cache.py`，测试在 `tests/test_mq.py`。

**Q：为什么用 Redis Streams 做消息队列，而不是 Kafka / RabbitMQ？**

A：按量级和语义选型。单实例场景下每天的合同审查任务量在千级以内，Kafka 的分区、副本、集群运维成本对这个量级是负资产。Redis Streams 提供了我需要的全部语义：consumer group、pending list、ACK、消息认领、MAXLEN 背压。同时我把投递语义收敛在 TaskBus 这一层接口后面（publish/consume/ack/fail），如果量级上来要换 Kafka，业务侧不用改。反问自己"这个量级为什么需要 Kafka"是我主动做过的决策，不是没考虑过。

**Q：消息会丢吗？从生产、存储、消费三段分析。**

A：生产侧：任务表先落盘、再 XADD 发布——任务表兼任本地消息表（outbox），发布失败时任务停在 pending 且带 `published=false` 标记，worker 空闲时做 outbox 补偿扫描重投，重投靠 task_id 级 SET NX 幂等去重，不会重复。存储侧：Redis 配置 AOF everysec，宕机最多丢 1 秒窗口，这是单机 demo 的明确取舍（appendfsync always 会拖慢 XADD，Kafka 多副本才是彻底答案）。消费侧：处理成功后才 XACK，worker 在 ACK 前崩溃，消息留在 consumer group 的 pending list，其他消费者按 idle 阈值用 XPENDING + XCLAIM 认领重投（Redis ≥6.2 可以用原子的 XAUTOCLAIM 等价替换）。

**Q：at-least-once 意味着消息会重复投递，怎么保证不重复执行？**

A：两层幂等。入队侧：task_id 和业务键（飞书 message_id）做 SET NX EX 去重，重复回调直接拒绝入队。消费侧：任务状态以任务表为准，收到已 completed 任务的重投消息时直接 ACK 跳过，不重跑审查。测试 `test_worker_skips_redelivered_completed_task` 专门验证这条路径。

**Q：消费一直失败怎么办？会不会无限重试？**

A：不会。消息带 attempts 计数，失败时 fail() 判定：未达 `max_attempts` 就自增重投，达到就写入 DLQ stream 并 ACK 原消息。DLQ 支持 `queue-dlq` 查看和人工检视后 `--requeue-all` 重投。另外认领路径取"消息自带 attempts"和"PEL times_delivered"的较大值，防止 worker 反复崩溃但 attempts 不涨的毒消息循环。

**Q：SET NX 之后、XADD 之前进程挂了，会发生什么？**

A：这是我处理过的具体双写窗口。若 XADD 抛异常，publish 会先删掉刚占用的去重键再向上抛，outbox 补偿就不会被自己的去重键挡住；若是硬崩溃来不及回滚，补偿重投会被去重键挡到 TTL（默认 24h）过期，之后自动恢复——最终一致，代价是延迟，这个取舍我可以说清楚。测试 `test_publish_rolls_back_dedup_key_when_xadd_fails` 覆盖了异常路径。

**Q：任务有优先级吗？Streams 不支持优先级怎么办？**

A：Streams 流内严格 FIFO，没有原生优先级。我用 high / normal 两条 stream 建模两级优先级，消费时先 drain high。这是够用的工程解；如果要 N 级优先级或延迟队列，会换 sorted set 做二级索引或直接换消息中间件。

**Q：Redis 挂了整个系统会怎样？**

A：三条链路各自降级：任务投递回退到文件队列轮询（工厂函数 ping 失败自动切换，queue-health 显示 fallback 原因）；LLM 缓存退化为进程内存缓存；飞书去重退回文件记录。审查主链路不依赖 Redis 存活。这和项目里 Milvus→内存向量库、BGE→哈希 embedding 的降级哲学一致：外部依赖不可用时功能降级，链路不断。

**Q：缓存这块，穿透 / 击穿 / 雪崩在你的场景怎么对应？**

A：我的缓存 key 是 prompt 内容哈希，不存在恶意构造不存在 key 的穿透面；同一合同重复评测时同 key 并发也就个位数，击穿风险低，真要处理就加 SET NX 单飞锁；TTL 是 7 天且 key 天然分散，无集中过期雪崩。我更愿意讲真实收益：评测批跑时相同条款的语义判断直接命中缓存，省的是真金白银的 token 成本，且 temperature=0.1 下缓存不改变结果分布。

**Q：为什么还保留文件任务表，不全放 Redis？**

A：职责分离：Redis Streams 只做投递通道，任务的状态机（pending/running/completed/failed）和审计信息落在任务表，dashboard 直接读它。消息可以被 trim、可以重投，任务状态必须持久且可回溯。这也是"MQ 传事件，DB 存状态"的常规架构，顺便让文件表兼任 outbox。

## 面试时不要这么说

- 不要说“替代律师”。
- 不要说“准确率已经线上 99%”。
- 不要说“真实企业合同已经大规模验证”，除非确实有数据。
- 不要说“用了 Milvus 所以效果好”，要说混合召回、元数据、rerank 和评测。
- 不要说“MCP 就是上传文件”，要说它是企业系统工具交互层。
- 不要说“Memory 就是聊天历史”，要说企业偏好、历史审查意见和修改模板沉淀。

## 结尾总结

这个项目的核心价值不是“做了一个法律问答机器人”，而是把合同审查做成了一个可执行、可追踪、可治理、可评测的 Agent 工作台。面试时应突出四个关键词：

- 可控执行：ReviewRun 状态机和 Supervisor-Worker Agent。
- 可追溯：RAG 来源、ToolCallTrace、Session Storage。
- 可治理：Permission Guard、人工复核、敏感外部操作拦截。
- 可评测：standard benchmark + human benchmark + 任务级指标。
