# Curated Template Benchmark（模板构造评测集）

> **命名澄清（重要）**：`data/human_benchmark/` 是**模板构造**的评测集——30 份合同由
> 13 类风险条款模板拼装而成，标注随构造自动产生，格式对齐人工标注 schema，
> 但**它不是人工标注数据集**，对外表述不允许写"30 份真实人工标注合同"。
> 真实合同评测（含 Precision/F1 与 Agent 端到端口径）见 `docs/benchmarks/REAL_BENCHMARK.md`。

## 定位

- 快速回归集：确定性构造、秒级跑完，适合 CI 与本地回归（`legal-agent eval --human`）。
- 覆盖 13 类风险 taxonomy、含 12 条改写措辞 hard 样本，用于解释规则与检索的互补性。
- 局限：每份合同只有 4 个风险条款 + 少量样板文字，**没有真实负例**，
  所以只能算 Recall，Precision 在这个集上无意义——这正是建设
  `data/real_benchmark/`（65 份真实示范文本 + 1724 个负例条款）的原因。

## 指标

`legal-agent eval --human` 输出 recall 类指标（risk_recall_at_10 / rule_recall /
source_coverage_at_10 / high_risk_recall / human_review_capture_rate）。
该集上分数接近饱和（构造使然），不作为能力证明，只作回归护栏。

## 与真实集的关系

| | 模板构造集（本文档） | 真实合同集（REAL_BENCHMARK） |
| --- | --- | --- |
| 文本来源 | 风险条款模板拼装 | 市场监管总局示范文本库真实合同 |
| 标注来源 | 构造即标注 | LLM 逐条款标注（待人工复核）+ 红线注入（构造即答案） |
| 负例 | 无 | 1724 个真实条款负例 |
| 可算指标 | Recall 类 | Precision / Recall / F1 / 误报率 |
| 评测对象 | 规则 + 检索组件 | **含 full_agent：完整 Agent 管线端到端** |
