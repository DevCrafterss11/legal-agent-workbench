# 产品化交付自查

## 员工可用工作流

- 上传合同：Web 支持 `.txt/.md/.markdown/.pdf/.docx` 上传，飞书机器人支持文本、文档链接和 PDF/DOCX 附件。
- 合同库审查：上传后进入合同库，可一键发起审查。
- 粘贴审查：支持直接粘贴合同文本进行审查。
- 报告查看：审查完成后可在网页查看 Markdown 报告。
- Skill 管理：网页可新增合同审查 Skill，后端支持 `skills.json` 与 `SKILL.md`。
- MCP 配置：网页可新增 http/stdio connector 配置（真实连接，失败如实报错，无 mock 类型）。
- RAG 配置：网页可在 local 与 Milvus 后端之间切换；当前已接入 BGE + Milvus，并索引公开合同语料与 curated 风险知识。
- 审计追踪：Dashboard 展示工具调用、Reflection、Compact、token、events。

## 生产架构边界

| 能力 | 当前实现 | 上线替换 |
| --- | --- | --- |
| 合同语料 | `data/common_contracts/` 已处理 500 份内容唯一的公开合同示范文本，生成 8388 条 RAG 条款知识；9 个官方重复来源另行溯源 | 扩展到企业历史合同、制度、判例和人工标注风险集 |
| 向量检索 | `LegalRagService` + BGE + Milvus，hashing/local 作为 fallback | 增加线上 embedding 服务、召回监控和定期重建索引 |
| Milvus | `MilvusVectorStore` 已支持 create/upsert/search；复用索引时会 hydrate 本地 fallback | 增加分区、别名、备份和灰度重建 |
| LLM | OpenAI-compatible/Ollama、本地 fallback、长连接池；ReviewRun 级 LLMCallTrace 与 token/cache/retry/cost 汇总；按任务模型路由 | 配置真实模型与每百万 token 单价；后续增加动态质量/成本路由 |
| MCP | 官方 MCP SDK stdio/http catalog discovery；连接失败如实报 failed，无 mock fallback | 配置真实 MCP server |
| 文档解析 | txt/md/pdf/docx 解析 | 增加 OCR 和版式还原 |
| 权限治理 | 本地策略与输出 guard；JWT Principal + viewer/reviewer/operator/admin RBAC | 接企业 IdP/OIDC、审批和集中审计系统 |
| 租户隔离 | 核心文档、任务、Run、Session、Event、Trace、Memory 按 `tenant_id` 隔离，Web → Worker 全链路传播身份；RAG 召回时做共享知识 + 当前租户预过滤 | PostgreSQL RLS、隔离渗透测试 |
| Tool 权限 / HITL | Registry 统一 fail-closed 策略门；scope/读写/角色检查；外部写入使用跨租户不可重放、参数绑定、单次消费审批 | 对接企业审批流、职责分离目录和集中审计告警 |
| 数据安全 | PII 递归脱敏；姓名/地址本地实体识别；上传件、任务表和密钥 AES-256-GCM 信封加密；macOS Keychain/AWS KMS 托管主密钥 | 生产选 AWS KMS 并配置 IAM 最小权限、轮换和审计 |

## 面试官可能追问

- 为什么不用纯 RAG？  
  本项目用规则、RAG 证据、语义评分、Reflection、Permission Guard 交叉验证，降低漏检和幻觉。

- Milvus 怎么接？  
  `legalworkbench.rag.vector_store.MilvusVectorStore` 已实现 create/upsert/search，Web 可切换 `vector_backend=milvus`；真实环境启动 Milvus 并配置 URI/collection 即可。

- Memory 怎么避免污染？  
  `LegalMemoryStore` 使用 Proposed → Approved → Active 生命周期。高风险、Prompt Injection 命中和 LLM-only 结论只形成 proposed 候选，召回只读取 approved / active；本地法务可通过 CLI 完成批准、激活或拒绝。

- MCP 有什么用？  
  MCP 是企业系统交互层，不只是上传。它用于读飞书/Notion 合同与制度、写回报告、创建 OA 审批、记录审计日志。

- 真实员工怎么用？  
  打开 Web，上传合同，选择是否连接 MCP，运行审查，查看风险报告；管理员可配置 Skill、MCP server、RAG backend。

## 当前指标快照

- 合同语料：500 份内容唯一的可解析公开合同示范文本，来源为国家市场监督管理总局合同示范文本库；另记录 9 个精确内容重复来源。
- RAG 本地知识源：8401 条，其中 13 条 curated 风险知识，8388 条公开合同条款知识。
- Skills：10 类，覆盖 SaaS、采购、NDA、销售、租赁、服务、建设、消费/预付式、劳动、投资。
- 规则引擎：覆盖无限责任、自动续约、数据安全、付款/验收、付款周期、知识产权、SLA、管辖、保密、解除通知、不可抗力、押金返还、预付退款等风险。
- Benchmark：120 cases；Risk Recall@10=1.0；Source Coverage=1.0；Tool Success Rate=1.0；Memory Recall@5=1.0。
- Human Benchmark：30 份合同、120 条条款级人工标注格式风险标签；包含 12 条隐性表达样本；RAG + Rule Risk Recall@10=1.0，Rule Recall=0.9，Source Coverage@10=1.0。
- Baseline Comparison：`legal-agent eval-baseline --dataset both` 可比较 rule-only、RAG-only、full-system；合成集 rule-only Recall@10=0.70、full-system=1.00，人工标注集 rule-only Recall@10=0.90、full-system=1.00，来源覆盖率由规则基线的 0 提升至 1.00。
- Held-out：`data/real_benchmark/annotations_heldout.json` 按原始合同/红线变体分层稳定哈希冻结，使用 `legal-agent eval-real --dataset heldout` 评测；`--memory on|off` 可做长期记忆消融。
- RAG Health：本地 JSON 知识源已完整重建；当前 Docker Desktop 未运行，Milvus collection 需在启动后通过 `legal-agent rag-health` 复核与重建。

## 仍需上线增强

- 500 份合同来自公开示范文本，适合面试和本地 RAG 语料；`data/human_benchmark/` 已提供首版人工标注格式 benchmark，真实上线还需要接企业历史合同并由企业法务复核标签。
- 当前 human benchmark 是 v1 种子集，可验证回归和链路；真实生产评测还应加入真实合同抽样、双人标注、一致性检查和争议仲裁流程。
- Web API 与 Agent Worker 已完成进程级分离；本地使用文件 TaskBus 降级，生产模式使用 Redis Streams Consumer Group。可通过 `storage.backend=postgres` + `LEGAL_WORKBENCH_POSTGRES_DSN` 将 Run/Task/Memory/Session/Event 切换到 PostgreSQL JSONB 事务存储；生产仍应补连接池、迁移工具和 RLS 策略。开发数据库见 `docker-compose.postgres.yml`。
- 权限治理已覆盖输出拦截、数据脱敏、本地/KMS 密文存储，以及 JWT、角色授权和核心业务对象租户过滤；RAG 召回已执行共享/租户预过滤；企业上线还应接企业 IdP/OIDC、PostgreSQL RLS、审批系统和集中审计平台。
- PDF/DOCX 文本抽取已可用，但扫描件/OCR、复杂表格和版式还原仍需增强。

## 生产启动建议

```bash
python -m pip install -e ".[dev]"
legal-agent init --force
# 终端一：API
legal-agent serve --port 5181
# 终端二：独立 Worker
legal-agent worker
```

可选真实模型：

```bash
export LEGAL_WORKBENCH_LLM_PROVIDER=openai_compatible
export LEGAL_WORKBENCH_LLM_BASE_URL=https://your-model-gateway/v1
export LEGAL_WORKBENCH_LLM_API_KEY=...
export LEGAL_WORKBENCH_LLM_MODEL=...
```
