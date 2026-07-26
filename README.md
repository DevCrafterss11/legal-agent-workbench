# 企业法务 Agent 执行工作台

这是一个独立的企业法务 Agent 执行平台，面向企业合同审查场景提供可运行、可追踪、可复核、可评测的审查工作台。

项目核心是一个可运行、可追踪、可复核、可评测的 Supervisor-Worker Agent Runtime：

```text
合同输入
-> Task Queue / ReviewRun 状态机
-> Legal Review Supervisor 主 Agent
-> Parser / Skill Planner / Evidence / Risk Reviewer 子 Agent
-> Rewriter / Compliance Auditor / Report Writer / Memory Curator 子 Agent
-> Permission Guard 输出治理
-> 报告生成与记忆沉淀
-> Session/Trace/Dashboard/Eval
```

## 工程模块

- `runtime/`：ReviewRun 执行门面，创建 Supervisor 并对外保持稳定 API
- `mq/`：Redis Streams 任务总线（消费组、ACK、重试、死信队列、幂等入队），文件队列降级
- `cache.py`：Redis cache-aside 缓存与幂等去重（LLM 响应、飞书事件），进程内存降级
- `agents/`：Supervisor-Worker 多 Agent 编排；RAG 作为 Evidence Agent 的工具能力
- `tools/`：合同解析、RAG、风险规则、改写、权限、报告等工具注册表
- `rag/`：合同条款知识库混合检索
- `memory/`：长期审查记忆全生命周期（写入门槛、冲突强化、使用反馈、时间衰减、容量驱逐与归档）
- `privacy.py`：PII 识别与可逆脱敏（身份证/手机/邮箱/银行卡，含校验码与 Luhn 验证）
- `governance/`：权限策略、风险规则、合规拦截与 prompt injection 三层防御
- `skills/`：SKILL.md（frontmatter）+ JSON 双来源技能，优先级合并为审查画像
- `workflow/`：Parser、Risk Reviewer、Rewriter、Auditor、Report Writer 多角色流程
- `connectors/`：飞书、Notion、OA、合同库等企业系统连接层
- `storage/`：审查 session 快照和任务状态
- `evals/`：benchmark 指标评测
- `reflection/`：二次复核与报告审计
- `compact/`：长合同上下文压缩快照
- `hooks/`：runtime 事件总线
- `documents/`：合同上传与文本抽取
- `llm/`：OpenAI-compatible / Ollama 模型接口、结构化决策（decide）与确定性本地 fallback
- `feishu_stream.py`：飞书机器人长连接事件监听
- `web.py`：FastAPI 交互式工作台，SSE 实时推送审查事件流

## 快速运行

```bash
python -m pip install -e ".[dev]"
legal-agent init
legal-agent review .lawbench/contracts/sample_saas_contract.md
legal-agent eval
legal-agent eval --human
legal-agent eval-baseline --dataset both
legal-agent serve --port 5180
```

打开：

```text
http://127.0.0.1:5180/
```

## Baseline 对比评测

项目提供 rule-only、RAG-only、full-system 三组可复现 baseline，用来解释规则引擎、证据检索和完整 Agent Runtime 分别带来的增益：

```bash
legal-agent eval-baseline --dataset both
python scripts/run_baseline_eval.py --dataset both
```

当前本地结果（合成集含隐式措辞 hard 样本与 1 个双漏的已知失败样本，刻意防止评测饱和）：

```text
synthetic  rule_only    recall@10=0.4833  source@10=0.0000
synthetic  rag_only     recall@10=0.9067  source@10=0.9067
synthetic  full_system  recall@10=0.9533  source@10=0.9067
human      rule_only    recall@10=0.9000  source@10=0.0000
human      full_system  recall@10=1.0000  source@10=1.0000
```

三组数字的解读：规则引擎对隐式措辞风险大量漏检（0.48）；语义检索补足大部分（0.91），
但会漏掉知识库覆盖稀疏的风险类型（如保密条款）；full_system 用规则∪检索互补到 0.95，
其中规则兜底命中的风险没有检索证据（source 仍为 0.91），会被 Permission Guard 标记人工复核。
剩余 4.7% 是措辞完全脱离词面的已知失败样本，保留在 benchmark 中标记改进方向。

## 接入真实 LLM（Ollama / OpenAI-compatible）

Runtime 内有两个真实的 LLM 决策点：Skill Planner 决定检索深度与风险关注点
（输出被夹在 [5,20] 与风险类型白名单内），Evidence Agent 在证据不足时决定是否
改写查询重试一次（循环上界写死）。默认 local provider 走确定性规则，接入真实模型：

```bash
# 本地 Ollama（无需 API key）
ollama pull qwen2.5:7b
legal-agent llm-config --provider ollama --model qwen2.5:7b

# 或任意 OpenAI-compatible API（api_key 存入 secrets.json，不进 settings）
legal-agent llm-config --provider openai_compatible \
  --model deepseek-chat --base-url https://api.deepseek.com/v1 --api-key sk-xxx
```

模型输出解析失败、网络超时或服务不可用时，决策点自动回落确定性规则，主链路不中断。

## 隐私边界与 PII 脱敏

合同明文只存在于本地信任边界内。两条出境路径各有闸门：

- **远端 LLM**：发送前 PII 可逆脱敏（同值同占位符），映射表只留进程内存，模型回复本地回填；LLM 响应缓存的 key 与值均基于脱敏文本，**PII 不落 Redis**
- **飞书回发**：单向脱敏，群聊消息不出现身份证/手机号明文

识别采用确定性正则 + 校验（身份证校验码、银行卡 Luhn），隐私层自身不依赖模型、无幻觉；
合同入口处 PII 扫描计入 trace 并打 sensitive 标记。扫描件 PDF 支持本地 OCR
（`pip install -e ".[ocr]"`，刻意不用云端 OCR——同一信任边界原则）。

检索融合支持两种模式：`--fusion score`（加权分数融合，可解释）与
`--fusion rrf`（reciprocal rank fusion，免疫 BM25 与向量分的量纲差异）。

## Cross-Encoder 重排

召回默认走"BM25/向量分 + 语义重叠 + metadata"的公式重排；可切换两级重排，
top-32 候选再过 bge-reranker 交叉编码精排（依赖缺失时自动降级公式并在 rag-status 暴露原因）：

```bash
python -m pip install -e ".[dev,bge]"
legal-agent rag-config --rerank-provider cross_encoder --rerank-model BAAI/bge-reranker-base
```

## Milvus 与 BGE

本项目默认可以用本地 hashing embedding + in-memory vector store 演示；面试或准生产环境建议切到 Milvus + BGE。当前项目已构建 `data/common_contracts/` 语料目录，包含 100 份可解析公开合同示范文本，并生成 1400+ 条合同条款知识写入 RAG（精确条数随语料重建浮动，以 `legal-agent rag-status` 为准）：

```bash
python -m pip install -e ".[dev,bge]"
docker compose -f docker-compose.milvus.yml up -d
legal-agent rag-config \
  --vector-backend milvus \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-provider bge \
  --embedding-model BAAI/bge-small-zh-v1.5
legal-agent rag-health
```

重新构建公开合同语料：

```bash
.venv/bin/python scripts/build_common_contract_corpus.py --limit 100
```

如果 Docker Desktop 未启动或 BGE 依赖未安装，系统会明确显示 fallback 状态：

- Milvus 不可用时降级到 in-memory vector store
- BGE 不可用时降级到 deterministic hashing embedding
- `legal-agent rag-health` 会同时检查 Docker、Milvus 端口、向量库连接和 embedding provider

关闭 Milvus：

```bash
docker compose -f docker-compose.milvus.yml down
```

## Redis 任务总线与缓存

审查任务的投递默认走文件队列轮询；生产形态切换到 Redis Streams 任务总线，文件任务表退化为任务状态存储 + 本地消息表（outbox）：

```bash
python -m pip install -e ".[dev,redis]"
docker compose -f docker-compose.redis.yml up -d
legal-agent queue-config --backend redis --redis-url redis://127.0.0.1:6379/0
legal-agent queue-health
legal-agent tasks "审查供应商 SaaS 服务协议"
legal-agent worker
legal-agent queue-dlq            # 查看死信
legal-agent queue-dlq --requeue-all
```

投递语义（`src/legalworkbench/mq/bus.py`）：

- **at-least-once + 幂等消费**：业务处理成功后才 XACK；重复投递以任务表状态为准跳过，入队按 `task_id` / 业务键（如飞书 message_id）做 SET NX 去重
- **有限重试 + 死信队列**：失败重投并自增 attempts，超过 `max_attempts` 进入 DLQ stream，支持人工检视后重投
- **崩溃恢复**：worker 在 ACK 前崩溃，消息留在 pending list，其他消费者按 `claim_idle_ms` 通过 XPENDING + XCLAIM 认领
- **双优先级**：Streams 流内 FIFO，用 `high` / `normal` 两条 stream 建模优先级
- **双写一致性**：任务表先落盘再发布，发布失败由 worker 空闲时的 outbox 补偿扫描重投
- **降级**：Redis 不可用时自动回退文件队列轮询，`queue-health` 显示 fallback 原因

Redis 同时承担缓存层（`src/legalworkbench/cache.py`）：LLM 响应 cache-aside 缓存（key 为 prompt 哈希、带 TTL）、飞书事件去重（SET NX EX）。Redis 掉线时缓存退化为进程内存，链路不中断。

## MCP 的作用

MCP 在本项目里是企业系统连接层，不是简单上传按钮。它可以让 Agent 通过标准 connector 与飞书、Notion、OA、合同库、CRM 交互：

- 从飞书/Notion 读取合同、模板、制度文档
- 将审查报告写回飞书文档或 Notion database
- 创建 OA 审批任务
- 查询历史合同与供应商/客户背景
- 写入合规审计日志

本地 MVP 不强依赖外部 MCP 服务；配置后可通过：

```bash
legal-agent mcp-context --connect
```

发现外部工具和资源。

### 接入真实飞书 / Lark MCP

项目支持官方 `@larksuiteoapi/lark-mcp`，用于把飞书开放平台 API 包装成 Agent 可调用工具。飞书桌面端只负责人工查看结果；Agent 真正调用飞书需要开放平台应用凭证。

准备条件：

- Node.js >= 20，且 `npx` 可用
- 飞书开放平台自建应用的 `APP_ID` / `APP_SECRET`
- 为应用开通云文档、知识库、消息、任务等权限；如果要在机器人里直接发送 PDF/DOCX 附件，需要开通消息资源文件读取/下载相关权限
- 如果要以用户身份搜索/读取个人可见文档，先配置 OAuth redirect URL：`http://localhost:3000/callback`

配置应用身份：

```bash
legal-agent lark-mcp \
  --app-id cli_xxxx \
  --app-secret your_app_secret \
  --tools docx.v1.document.rawContent,docx.builtin.import,docx.builtin.search,wiki.v2.space.getNode,task.v2.task.create,im.v1.message.create
```

配置用户 OAuth：

```bash
legal-agent lark-mcp --app-id cli_xxxx --app-secret your_app_secret --oauth
npx -y @larksuiteoapi/lark-mcp login -a cli_xxxx -s your_app_secret --scope "offline_access docx:document wiki:wiki im:message task:task"
```

检查连接：

```bash
legal-agent lark-mcp --connect
legal-agent mcp-context --connect
```

凭证安全：

- `.lawbench/settings.json` 保存连接器、工具范围和非敏感配置
- `.lawbench/secrets.json` 保存 `APP_SECRET` / `USER_ACCESS_TOKEN`
- `.lawbench/` 已加入 `.gitignore`，不要提交本地密钥

### 飞书机器人入口

如果希望“在飞书里给机器人发合同/文档链接/PDF 或 DOCX 附件，机器人返回审查结果”，推荐本地开发先用长连接订阅事件：

```bash
legal-agent feishu-listen --status
legal-agent feishu-listen
```

长连接模式不需要公网域名。它的链路是：

```text
本地监听进程主动连接飞书
-> 飞书通过 WebSocket 推送 im.message.receive_v1
-> 提取合同正文、docx token 或文件附件 file_key
-> 文档链接走 Lark MCP 读取飞书文档；PDF/DOCX 附件走飞书 OpenAPI 下载消息资源文件
-> Legal Agent Runtime 审查
-> Lark MCP 调用 im.v1.message.create 回发结果
```

这适合本地调试、面试演示、内网环境验证。正式部署时也可以使用 HTTP Callback：

```bash
legal-agent serve --port 5181
legal-agent feishu-event --setup-guide
```

在飞书开放平台配置：

```text
事件订阅请求地址：https://<公网域名>/api/feishu/events
订阅事件：im.message.receive_v1
```

本地开发可以用 ngrok 或 cloudflared 把 `http://127.0.0.1:5181` 暴露到公网。飞书会先发送 URL verification，服务会返回 `challenge` 完成校验。

本地模拟机器人消息：

```bash
legal-agent feishu-event --text "## 赔偿责任\n乙方承担全部损失且不设赔偿责任上限。"
```

真实链路：

```text
飞书机器人消息
-> /api/feishu/events
-> 提取合同正文、docx token 或文件附件 file_key
-> 文档链接走 Lark MCP 读取飞书文档；PDF/DOCX 附件走飞书 OpenAPI 下载消息资源文件
-> Legal Agent Runtime 审查
-> Lark MCP 调用 im.v1.message.create 回发结果
```

## 常用命令

```bash
legal-agent tools
legal-agent workflow
legal-agent sessions
legal-agent memory
legal-agent events
legal-agent rag-status
legal-agent tasks "审查供应商 SaaS 服务协议"
legal-agent eval --scaled
legal-agent eval --human
legal-agent eval-baseline --dataset both
```

更多设计说明见 `docs/ARCHITECTURE.md`，人工标注 benchmark 见 `docs/HUMAN_BENCHMARK.md`，面试答辩准备见 `docs/INTERVIEW_QA.md`，产品化自查见 `docs/PRODUCT_READINESS.md`，迁移自查见 `docs/HARNESS_MIGRATION_AUDIT.md`，简历表达见 `docs/RESUME_PROJECT.md`。
