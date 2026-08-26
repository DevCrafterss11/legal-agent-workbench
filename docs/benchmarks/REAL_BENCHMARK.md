# 真实合同评测（Real Contract Benchmark）

这份文档回答三个问题：评测集怎么来的、指标怎么定义、**评的到底是不是 Agent 本身**。

## 一、评测对象：就是 Agent

`eval-real` 的主指标行是 `full_agent`：对每份合同**真实执行一遍
`LegalAgentRuntime.review()` 完整 supervisor-worker 管线**（解析、技能规划、
检索、规则、LLM 语义判断、治理拦截、反思复核），对最终 `run.findings` 打分。
其余三行是消融对照，用来解释 Agent 各组件的贡献：

| 方法 | 内容 | 作用 |
| --- | --- | --- |
| `rule_only` | 仅确定性规则引擎 | 下界：无语义能力时的表现 |
| `rag_only` | 仅混合检索 + 生产同款证据判定门 | 检索单独的贡献 |
| `rule_plus_rag` | 规则 ∪ 检索 | 组件级并集 |
| `full_agent` | 完整 Agent 管线（真实 LLM 调用） | **被评测的系统本身** |

评测运行会快照并还原 `memory.json`，防止评测合同经 Memory Curator 沉淀记忆、
把前一份合同的结论泄漏给后一份（评测污染）。

## 二、数据集：真实合同 + 已知答案注入 + 负例

来源与构成见 `data/real_benchmark/README.md`。设计要点：

1. **真实文本**：全部合同来自国家市场监督管理总局示范文本库（可溯源到原始 URL），
   条款切分用项目自己的解析器（支持"第X条"），与线上处理路径一致。
2. **负例是第一等公民**：示范文本大多均衡，未被标注的真实条款全部计为负例。
   这让 Precision / 误报率第一次可测——老合成集每个条款都是风险，Precision 恒为 1，无意义。
3. **两类正样本**：
   - 真实条款风险（LLM 逐条款标注，待人工复核）：数量少而分散，符合真实分布；
   - 红线注入（`*_redline.md` 变体）：在真实合同上改写/追加不利条款，位置与
     预期风险构造时记录，**答案不依赖任何标注者**，含刻意绕开规则关键词的
     paraphrase 措辞。
4. **标注诚实性**：标注者身份（`llm:claude-fable-5` / `script:glm-4-flash`）与
   `review_status` 写进数据集与评测输出。人工复核走
   `scripts/review_annotations.py`，被拒标注不进 gold。复核前对外口径是
   "LLM 标注"，复核后才是"LLM 标注 + 人工复核"，**任何情况下都不叫"人工标注数据集"**。

## 三、指标定义

预测与 gold 按 `(clause_id, risk_type)` 精确匹配：

- `precision = TP / (TP + FP)`：报出来的风险有多少是真的（误报控制）
- `recall = TP / (TP + FN)`：真风险漏了多少
- `F1`：两者调和平均，主对比指标
- `high_recall`：高危标注的召回（漏报高危代价最大）
- `inject_recall`：红线注入子集的召回（答案由构造保证，不受标注质量影响，
  是最硬的已知答案指标）
- `real_recall`：真实条款风险子集的召回
- `FP/contract`：平均每份合同误报条数（法务的实际阅读负担）

`blocked`（治理拦截）的 finding 不计入预测——被系统自己拦下的结论不应得分。

## 四、当前结果

数据集：98 份合同（65 真实原件 + 33 红线变体）、138 条 gold 风险（125 注入 +
13 真实条款风险）、1724 个负例条款。复现：`legal-agent eval-real`。

### 这套 benchmark 抓出的第一个真实 bug 及修复

旧版规则引擎与检索证据门控是**话题关键词检测**（条款提到"付款/保密/不可抗力"
就触发）。合成集（全是风险条款、没有负例）完全暴露不了这个问题——recall 0.95
看起来很好；换到真实合同 + 负例上立即现形：

| 方法（修复前） | precision | recall | F1 | FP/合同 |
| --- | ---: | ---: | ---: | ---: |
| rule_only | 0.137 | 0.441 | 0.209 | 4.56 |
| rag_only | 0.063 | 0.492 | 0.112 | 11.97 |
| full_agent（3 份冒烟） | 0.000 | 0.000 | 0.000 | 22.67 |

修复：把规则引擎与证据门控重写为**不利模式正向匹配**（governance/rules.py
`match_adverse`，两层共用同一模式库），只有条款出现真正的不利语言
（"概不退还""不设上限""由乙方视情况确定"……）才触发。修复后（98 份全量）：

| 方法（修复后） | precision | recall | F1 | high_recall | inject_recall | real_recall | FP/合同 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rule_only | 0.842 | 0.928 | 0.883 | 0.943 | 0.984 | 0.385 | 0.24 |
| rag_only | 0.879 | 0.739 | 0.803 | 0.943 | 0.808 | 0.077 | 0.14 |
| rule_plus_rag | 0.842 | 0.928 | 0.883 | 0.943 | 0.984 | 0.385 | 0.24 |
| **full_agent**（12 份均衡子集，语义候选改造前基线） | **0.889** | **1.000** | **0.941** | 1.000 | 1.000 | 1.000 | 0.25 |

full_agent 口径说明：12 份均衡采样（6 真实原件 + 6 红线变体，gold=24、负例 252），
真实执行完整管线（llm=glm-4-flash），耗时 2535s（约 3.5 分钟/份，几十次真实
LLM 调用/份）。值得注意的是该子集内 full_agent 的 recall（含真实条款风险）高于
rule_plus_rag——检索证据与 LLM 语义层在规则漏检的条款上补回了召回。全量 98 份
的端到端评测约需数小时：
`legal-agent eval-real --methods full_agent --save eval_agent_full.json`。

分风险类型短板（rule_only，诚实标注改进方向）：data_security p=0.40、
prepaid_refund p=0.43/r=0.43、ip_ownership p=0.57、termination_notice 与
force_majeure 各有 5-6 个 FP——这些是下一轮迭代目标。

### LLM 独立语义候选层

针对 `real_recall=0.385`，`RiskReviewerAgent` 已将候选来源从“规则/RAG 先提名，
LLM 只评分”改为三路并行：规则候选、RAG 候选、LLM 独立语义候选。LLM 可以在
`match_adverse` 未命中时提出新候选，但不能直接输出结论，必须依次通过：

1. `risk_type` 属于现有 13 类白名单，模型最多返回 3 个候选；
2. `evidence_quote` 能在原合同条款中逐字或仅忽略空白后定位；
3. 针对该风险类型重新检索并取得同类 RAG 证据；
4. 在条款 + 证据条件下进行第二次语义核验，分数不低于 0.60；
5. 进入既有 Permission Guard / Reflection，且独立候选强制人工复核。

受控回归测试覆盖两条关键路径：规则零命中的陌生责任措辞能由独立候选生成
finding；同一候选在二次核验低于阈值时被拒绝。冻结 held-out 清单位于
`annotations_heldout.json`，可通过 `eval-real --dataset heldout` 运行；它沿用冻结标注，
仍不应参与规则或阈值调参。
本表：2026-07-27 对 3 条未参与原 benchmark 的真实自动延续条款进行盲测时，
`glm-4-flash` 均发生读取超时并按设计降级为空候选。网络/模型恢复后应在冻结
held-out 正负样本上同时报告 Precision、`real_recall`、FP/合同、耗时和调用成本；
在使用独立新标注前不得声称 `0.385` 已经提升。

### 必须诚实交代的两个口径

1. **inject_recall 0.98 偏乐观**：红线注入文本与不利模式库出自同一次设计，
   存在同源性；真正代表"新措辞泛化能力"的是 real_recall 0.385（13 条真实条款
   风险只抓到 5 条）。LLM 独立语义候选层已实现；冻结 held-out 清单用于后续盲测，
   在使用独立新标注前仍不宣称泛化能力提升。
2. **precision 0.84 是可信的**：1724 个负例全部来自真实合同的真实条款，
   模式库对均衡文本的沉默能力是实测的（并有专门的均衡条款回归测试守护）。

## 五、如何复现与扩展

```bash
# 全量评测（full_agent 会真实调用配置的 LLM，耗时与合同数成正比）
python -c "from legalworkbench.cli import app; app()" eval-real
# 只跑快速消融
python -c "from legalworkbench.cli import app; app()" eval-real --methods rule_only,rag_only,rule_plus_rag
# Agent 端到端（可用 --limit 控制份数）
python -c "from legalworkbench.cli import app; app()" eval-real --methods full_agent --limit 36 --save eval_agent.json
# 冻结 held-out 消融
python -c "from legalworkbench.cli import app; app()" eval-real --dataset heldout --methods rule_only,rag_only,rule_plus_rag

# 扩到 500 份真实合同（需能访问 htsfwb.samr.gov.cn；断点续传）
python scripts/build_common_contract_corpus.py --limit 500 --resume
python scripts/build_real_benchmark.py prepare --extend --limit 500
python scripts/build_real_benchmark.py inject --ratio 0.5
python scripts/annotate_contracts.py --only-missing            # 程序化 LLM 标注
python scripts/build_real_benchmark.py assemble --annotations data/real_benchmark/llm_annotations
python scripts/review_annotations.py --reviewer 你的名字        # 人工复核收口
```

## 六、已知边界（面试时主动讲）

- 标注模型与被评系统的语义判断模型存在同源风险（annotator circularity）：
  24 份由独立更强模型（Claude）标注，其余由 `glm-4-flash` 程序化标注并强制
  `requires_human_review`；红线注入子集完全不受此影响。
- 示范文本偏均衡，真实条款正样本稀疏；召回压力主要由红线注入子集提供。
- `full_agent` 结果依赖所配置的模型与网络状态，评测输出会记录 `llm=` 口径。
