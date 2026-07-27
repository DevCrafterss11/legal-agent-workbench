# Real Contract Benchmark（真实合同评测集）

真实公开合同 + 条款级风险标注 + 已知答案红线注入，用于计算 Precision / Recall / F1
（老的合成集只能算 Recall）。

## 构成

- `contracts/real_XXX.md`：真实合同正文，全部来自国家市场监督管理总局合同示范文本库
  （来源 URL、发布部门、年份见 `tasks/*.clauses.json` 的 `source` 字段）。
- `contracts/real_XXX_redline.md`：红线变体——在真实合同上改写/追加对甲方不利条款，
  注入位置与预期风险全部记录在 `injections.json`，是有已知答案的正样本。
- `tasks/*.clauses.json`：按项目解析器切分的条款清单（标注任务输入）。
- `llm_annotations/`：条款级 LLM 标注原始输出（`annotator` 字段记录标注模型）。
- `annotations.json`：合并后的评测数据集。**未被标注的条款一律是负例**，
  这是 Precision 与误报率的来源。

## 标注口径（诚实声明）

- 正样本两个来源：① LLM 逐条款标注（`annotation_notes: llm_real_clause / llm_script`）；
  ② 确定性红线注入（`evidence_source: injected_redline`，答案由构造保证）。
- **这不是人工标注**。`review_status: pending_human_review` 表示待人工复核；
  用 `python scripts/review_annotations.py --reviewer 你的名字` 逐条确认后，
  数据集口径才升级为"LLM 标注 + 人工复核"。被拒绝的标注保留复核痕迹但不进 gold。

## 复现与扩展

```bash
# 评测（含真实跑完整 Agent 管线的 full_agent 模式）
python -c "from legalworkbench.cli import app; app()" eval-real
python -c "from legalworkbench.cli import app; app()" eval-real --methods full_agent --limit 36

# 扩语料到 500 份（需可访问 htsfwb.samr.gov.cn 的网络）
python scripts/build_common_contract_corpus.py --limit 500 --resume
python scripts/build_real_benchmark.py prepare --extend --limit 500
python scripts/build_real_benchmark.py inject --ratio 0.5
python scripts/annotate_contracts.py --only-missing
python scripts/build_real_benchmark.py assemble --annotations data/real_benchmark/llm_annotations
```
