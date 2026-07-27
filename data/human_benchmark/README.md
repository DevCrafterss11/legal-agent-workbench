# Curated Template Benchmark（模板构造回归集）

> 命名澄清：目录名沿用 `human_benchmark`（避免破坏既有命令），但这是**模板构造**
> 的评测集，标注随构造自动产生——**不是人工标注数据集**。
> 真实合同评测集见 `data/real_benchmark/`，模板回归集说明见 `docs/benchmarks/TEMPLATE_BENCHMARK.md`。

- Contracts: 30（13 类风险条款模板拼装）
- Clause-level risk annotations: 120
- 用途：秒级确定性回归护栏（CI）；分数接近饱和，不作为能力证明
- Evaluation command: `legal-agent eval --human`
