"""Real-contract benchmark evaluator: P/R/F1 math and review filtering."""

from __future__ import annotations

import json
from pathlib import Path

from legalworkbench.evals import RealBenchmarkEvaluator


def _write_dataset(tmp_path: Path) -> None:
    bench = tmp_path / "data" / "real_benchmark"
    (bench / "contracts").mkdir(parents=True)
    contract = "\n".join(
        [
            "# 测试服务合同",
            "",
            "第一条 赔偿责任",
            "乙方应赔偿甲方全部损失，包括间接损失、预期利润损失，且不设赔偿责任上限。",
            "第二条 交付安排",
            "乙方应当按照约定的时间和地点向甲方交付相关工作成果。",
            "",
        ]
    )
    (bench / "contracts" / "t1.md").write_text(contract, encoding="utf-8")
    payload = {
        "name": "test_real_benchmark",
        "version": "test",
        "annotation_provenance": {"annotator": "test", "review_status": "pending_human_review"},
        "contracts": [
            {
                "contract_id": "t1",
                "title": "测试服务合同",
                "contract_type": "general",
                "scenario": "real_model_contract",
                "file": "contracts/t1.md",
                "annotator": "test",
                "review_status": "pending_human_review",
                "negative_clause_ids": ["C002"],
                "annotations": [
                    {
                        "risk_id": "RB0001",
                        "clause_id": "C001",
                        "clause_title": "第一条 赔偿责任",
                        "risk_type": "unlimited_liability",
                        "risk_level": "high",
                        "rationale": "无上限赔偿",
                        "expected_suggestion": "设置责任上限",
                        "evidence_source": "test",
                        "requires_human_review": True,
                        "annotation_notes": "llm_real_clause",
                    },
                    {
                        # 人工复核已拒绝：不得进入 gold
                        "risk_id": "RB0002",
                        "clause_id": "C002",
                        "clause_title": "第二条 交付安排",
                        "risk_type": "jurisdiction",
                        "risk_level": "medium",
                        "rationale": "误标",
                        "expected_suggestion": "",
                        "evidence_source": "test",
                        "requires_human_review": True,
                        "annotation_notes": "llm_script",
                        "review": {"verdict": "rejected", "reviewer": "tester", "reviewed_at": 0.0},
                    },
                ],
            }
        ],
    }
    (bench / "annotations.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_rule_only_precision_recall_and_review_filtering(tmp_path: Path) -> None:
    _write_dataset(tmp_path)
    report = RealBenchmarkEvaluator(tmp_path).run(methods=("rule_only",))
    assert report.contracts == 1
    # 被拒标注剔除后 gold 只剩 1 条
    assert report.gold_risks == 1
    row = report.methods[0]
    assert row.method == "rule_only"
    assert row.gold_risks == 1
    # 规则引擎必须命中显式无上限赔偿条款
    assert row.true_positives == 1
    assert row.recall == 1.0
    assert row.high_risk_recall == 1.0
    assert 0.0 < row.precision <= 1.0
    assert row.false_negatives == 0
    # F1 与 P/R 自洽
    expected_f1 = round(2 * row.precision * row.recall / (row.precision + row.recall), 4)
    assert abs(row.f1 - expected_f1) < 1e-6


def test_limit_balances_originals_and_variants(tmp_path: Path) -> None:
    _write_dataset(tmp_path)
    # 追加一份变体合同，验证 limit 均衡采样两类合同
    bench = tmp_path / "data" / "real_benchmark"
    payload = json.loads((bench / "annotations.json").read_text(encoding="utf-8"))
    variant = json.loads(json.dumps(payload["contracts"][0]))
    variant["contract_id"] = "t1_redline"
    variant["scenario"] = "injected_redline_variant"
    payload["contracts"].append(variant)
    (bench / "annotations.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = RealBenchmarkEvaluator(tmp_path).run(methods=("rule_only",), limit=2)
    assert report.contracts == 2
