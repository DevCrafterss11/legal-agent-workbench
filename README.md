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
- `privacy.py`：PII 识别与可逆脱敏（身份证/手机/邮箱/银行卡 + 本地姓名/地址实体识别）
- `secure_storage.py`：AES-256-GCM 信封加密，支持 macOS Keychain 与 AWS KMS 的外部主密钥
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

macOS 一键启动（会检查虚拟环境、Milvus、Web 和已安装的飞书 LaunchAgent，并自动打开浏览器）：

```bash
./start.command
```

需要强制重启 Web 时：

```bash
./start.command --restart
```

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

## 评测体系：真实合同 P/R/F1 + Agent 端到端

主评测集是 **`data/real_benchmark/`：65 份真实合同（国家市场监督管理总局示范文本库，
可溯源）+ 33 份红线注入变体（125 个已知答案风险）+ 1724 个真实负例条款**。
未标注条款全部计为负例，因此可以计算 Precision / Recall / F1 与误报率，
而不是只报 Recall；主指标行 `full_agent` 真实执行完整 supervisor-worker Agent
管线（含 LLM 决策点）后对 `run.findings` 打分——**评的是 Agent 本身**，
rule_only / rag_only / rule_plus_rag 是组件消融对照：

```bash
legal-agent eval-real                                   # 全部方法（full_agent 走真实 LLM）
legal-agent eval-real --methods rule_only,rag_only,rule_plus_rag   # 快速消融
legal-agent eval-real --methods full_agent --limit 12   # Agent 端到端限量评测
```

当前结果（`rule_only` 修复前 → 修复后）：precision 0.14 → **0.84**、F1 0.21 → **0.88**、
误报 4.6 → **0.24 条/合同**——这套 benchmark 抓出的第一个真实问题就是旧规则层的
"话题词误报洪水"，并驱动了不利模式规则重写（`governance/rules.py`）。
`full_agent` 端到端（12 份均衡子集、真实 LLM，独立语义候选改造前基线）：
precision 0.889 / recall 1.0 / F1 0.941。当前 LLM 已能绕过规则硬门控独立提出候选，
但必须通过风险类型白名单、原文锚定、同类 RAG 证据和二次语义核验；新的真实
held-out 指标完成前仍保留上述基线，不提前宣称提升。
数据来源、标注口径（LLM 标注 + 人工复核流程，非"人工标注"）、指标定义与完整
结果见 `docs/benchmarks/REAL_BENCHMARK.md`。

旧的合成回归集仍保留为 CI 护栏（秒级、确定性，含隐式措辞 hard 样本与已知失败样本）：

```bash
legal-agent eval-baseline --dataset both
python scripts/run_baseline_eval.py --dataset both
```

```text
synthetic  rule_only    recall@10=0.4833
synthetic  rag_only     recall@10=0.9067
synthetic  full_system  recall@10=0.9533
```

注意：合成集与模板构造集只能算 Recall（没有负例），分数接近饱和是构造使然，
不作为能力证明——真实能力口径以 `eval-real` 为准。

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

合同明文只在本地审查进程的内存中出现。上传原件、抽取文本、粘贴合同、任务表和
`secrets.json` 均使用 AES-256-GCM 信封加密后落盘，每次写入使用独立数据密钥；主密钥位于
macOS Keychain 或 AWS KMS，不与密文同盘。两条出境路径各有闸门：

- **远端 LLM**：发送前 PII 可逆脱敏（同值同占位符），映射表只留进程内存，模型回复本地回填；LLM 响应缓存的 key 与值均基于脱敏文本，**PII 不落 Redis**
- **飞书回发**：单向脱敏，群聊消息不出现 PII 明文

识别采用确定性正则 + 校验（身份证校验码、银行卡 Luhn），并在“姓名/联系人/法定代表人”、
“联系地址/住所地/送达地址”等法务字段上执行本地上下文实体识别；不向远程 NER 服务发送合同。
合同入口处 PII 扫描计入 trace 并打 sensitive 标记。扫描件 PDF 支持本地 OCR
（`pip install -e ".[ocr]"`，刻意不用云端 OCR——同一信任边界原则）。

本机首次启用密文存储并迁移历史数据：

```bash
legal-agent encryption-init --provider macos-keychain
```

生产环境使用 AWS KMS（需安装 `pip install -e ".[aws-kms]"` 并配置 AWS 凭证）：

```bash
legal-agent encryption-init --provider aws-kms \
  --aws-kms-key-id arn:aws:kms:REGION:ACCOUNT:key/KEY_ID --aws-region REGION
```

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

本项目默认可以用本地 hashing embedding + in-memory vector store 演示；面试或准生产环境建议切到 Milvus + BGE。当前 `data/common_contracts/` 包含 **500 份内容唯一**的公开合同示范文本（另有 9 个官方重复来源在 manifest 中保留溯源、不计入有效语料），已自动提取正文并生成 8388 条合同条款知识；叠加 13 条 curated 风险知识后，本地 RAG 知识源共 8401 条：

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
.venv/bin/python scripts/build_common_contract_corpus.py --limit 500 --resume
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

文档入口见 `docs/README.md`。
