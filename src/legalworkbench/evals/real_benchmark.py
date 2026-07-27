"""Real-contract benchmark evaluation: precision / recall / F1, incl. full-agent mode.

数据集：``data/real_benchmark``——真实政府示范文本 + 红线注入变体。
正样本 = 条款级风险标注（LLM 标注待人工复核 + 注入记录），
负样本 = 同一批合同中所有未被标注的真实条款，因此可以计算 Precision 与误报率，
而不是只报 Recall。

四种方法对照：

- ``rule_only``      仅确定性规则引擎
- ``rag_only``       仅混合检索 + 生产环境同款证据判定门（evidence_implies_risk）
- ``rule_plus_rag``  规则 ∪ 检索（组件级上限）
- ``full_agent``     真实跑一遍 LegalAgentRuntime.review() 完整 supervisor-worker
                     管线（含 LLM 决策点、治理、反思），对 run.findings 打分——
                     这是对 Agent 本身的端到端评测口径

匹配口径：预测与标注按 (clause_id, risk_type) 精确匹配。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from legalworkbench.agents.risk_reviewer import evidence_implies_risk
from legalworkbench.governance import RiskRuleEngine
from legalworkbench.llm import LlmClient
from legalworkbench.parser import parse_clauses
from legalworkbench.paths import memory_path
from legalworkbench.retrieval import HybridClauseRetriever
from legalworkbench.store import WorkbenchStore

RISK_TAXONOMY = frozenset(
    {
        "auto_renewal",
        "unlimited_liability",
        "data_security",
        "payment_acceptance",
        "payment_cycle",
        "ip_ownership",
        "sla_remedy",
        "jurisdiction",
        "confidentiality",
        "termination_notice",
        "force_majeure",
        "deposit_return",
        "prepaid_refund",
    }
)

REAL_BENCHMARK_METHODS = ("rule_only", "rag_only", "rule_plus_rag", "full_agent")


def real_benchmark_dir(cwd: str | Path | None = None) -> Path:
    return Path(cwd or Path.cwd()).resolve() / "data" / "real_benchmark"


def load_real_benchmark(cwd: str | Path | None = None) -> dict:
    path = real_benchmark_dir(cwd) / "annotations.json"
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return {}
    # 人工复核中被拒绝的标注不进 gold（复核痕迹保留在文件里）
    for contract in parsed.get("contracts", []):
        contract["annotations"] = [
            ann
            for ann in contract.get("annotations", [])
            if (ann.get("review") or {}).get("verdict") != "rejected"
        ]
    return parsed


@dataclass(frozen=True)
class RealBenchmarkMethodResult:
    method: str
    contracts: int
    gold_risks: int
    predicted: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    high_risk_recall: float
    injected_recall: float
    real_clause_recall: float
    fp_per_contract: float
    duration_seconds: float
    per_risk_type: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RealBenchmarkReport:
    dataset: str
    version: str
    annotator: str
    review_status: str
    llm_mode: str
    contracts: int
    gold_risks: int
    negative_clauses: int
    methods: list[RealBenchmarkMethodResult]
    evaluated_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "methods"},
            "methods": [m.to_dict() for m in self.methods],
        }


class RealBenchmarkEvaluator:
    """Score component ablations and the real agent pipeline on real contracts."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.store = WorkbenchStore(self.cwd)
        self.rules = RiskRuleEngine()

    def run(
        self,
        *,
        methods: tuple[str, ...] = REAL_BENCHMARK_METHODS,
        limit: int = 0,
    ) -> RealBenchmarkReport:
        payload = load_real_benchmark(self.cwd)
        contracts = payload.get("contracts", [])
        if limit:
            # 均衡取样：一半真实原件（负例为主）+ 一半红线变体（已知答案正样本），
            # 避免 limit 只取到清单前段的原件导致 gold 过稀
            originals = [c for c in contracts if c.get("scenario") != "injected_redline_variant"]
            variants = [c for c in contracts if c.get("scenario") == "injected_redline_variant"]
            half = (limit + 1) // 2
            contracts = originals[:half] + variants[: limit - half]
        provenance = payload.get("annotation_provenance", {})
        results: list[RealBenchmarkMethodResult] = []
        for method in methods:
            if method not in REAL_BENCHMARK_METHODS:
                raise ValueError(f"Unsupported method: {method}")
            started = time.time()
            per_contract = [
                (contract, self._predict(method, contract))
                for contract in contracts
            ]
            results.append(self._score(method, per_contract, time.time() - started))
        llm = LlmClient(cwd=self.cwd)
        endpoint = llm.remote_endpoint()
        llm_mode = f"{llm.config.provider}:{llm.config.model}" if endpoint else "local_deterministic_fallback"
        return RealBenchmarkReport(
            dataset=str(payload.get("name", "real_contract_benchmark")),
            version=str(payload.get("version", "")),
            annotator=str(provenance.get("annotator", "unknown")),
            review_status=str(provenance.get("review_status", "unknown")),
            llm_mode=llm_mode,
            contracts=len(contracts),
            gold_risks=sum(len(c.get("annotations", [])) for c in contracts),
            negative_clauses=sum(len(c.get("negative_clause_ids", [])) for c in contracts),
            methods=results,
            evaluated_at=time.time(),
        )

    # ------------------------------------------------------------------
    # prediction
    # ------------------------------------------------------------------

    def _clauses(self, contract: dict) -> list:
        path = (real_benchmark_dir(self.cwd) / contract["file"]).resolve()
        return parse_clauses(path.read_text(encoding="utf-8"))

    def _predict(self, method: str, contract: dict) -> set[tuple[str, str]]:
        if method == "full_agent":
            return self._predict_full_agent(contract)
        retriever = None
        if method in {"rag_only", "rule_plus_rag"}:
            retriever = HybridClauseRetriever(self.store.load_knowledge())
        predicted: set[tuple[str, str]] = set()
        for clause in self._clauses(contract):
            if method in {"rule_only", "rule_plus_rag"}:
                for hit in self.rules.evaluate(clause.text):
                    if hit.risk_type in RISK_TAXONOMY:
                        predicted.add((clause.clause_id, hit.risk_type))
            if retriever is not None:
                evidence = retriever.search(
                    clause.text, contract_type=contract.get("contract_type", "general"), top_k=10
                )
                # 与 RiskReviewerAgent 相同的证据判定门，保证 ablation 对照的是生产逻辑
                for item in evidence[:3]:
                    if item.risk_type in RISK_TAXONOMY and evidence_implies_risk(
                        item.risk_type, clause.text, item.score
                    ):
                        predicted.add((clause.clause_id, item.risk_type))
        return predicted

    def _predict_full_agent(self, contract: dict) -> set[tuple[str, str]]:
        # 延迟导入避免 evals -> runtime -> evals 环
        from legalworkbench.runtime import LegalAgentRuntime

        contract_path = (real_benchmark_dir(self.cwd) / contract["file"]).resolve()
        # 评测运行不应污染长期记忆（memory_curator 会从每次 run 沉淀记忆，
        # 还会把先前评测合同的结论泄漏给后面的合同）——快照并还原 memory.json
        mem_path = memory_path(self.cwd)
        backup = mem_path.with_suffix(".eval_backup")
        had_memory = mem_path.exists()
        if had_memory:
            shutil.copy2(mem_path, backup)
        try:
            runtime = LegalAgentRuntime(self.cwd)
            run = runtime.review(contract_path)
        finally:
            if had_memory and backup.exists():
                shutil.move(backup, mem_path)
        predicted: set[tuple[str, str]] = set()
        for finding in run.findings:
            if finding.blocked:
                continue
            if finding.risk_type in RISK_TAXONOMY:
                predicted.add((finding.clause_id, finding.risk_type))
        return predicted

    # ------------------------------------------------------------------
    # scoring
    # ------------------------------------------------------------------

    def _score(
        self,
        method: str,
        per_contract: list[tuple[dict, set[tuple[str, str]]]],
        duration: float,
    ) -> RealBenchmarkMethodResult:
        tp = fp = fn = 0
        high_expected = high_hits = 0
        injected_expected = injected_hits = 0
        real_expected = real_hits = 0
        per_risk: dict[str, dict[str, int]] = {}
        for contract, predicted in per_contract:
            gold: dict[tuple[str, str], dict] = {
                (ann["clause_id"], ann["risk_type"]): ann
                for ann in contract.get("annotations", [])
            }
            tp += len(predicted & set(gold))
            fp += len(predicted - set(gold))
            fn += len(set(gold) - predicted)
            for key, ann in gold.items():
                risk_type = ann["risk_type"]
                bucket = per_risk.setdefault(risk_type, {"gold": 0, "tp": 0, "fp": 0})
                bucket["gold"] += 1
                hit = key in predicted
                if hit:
                    bucket["tp"] += 1
                if ann.get("risk_level") == "high":
                    high_expected += 1
                    high_hits += int(hit)
                if ann.get("evidence_source") == "injected_redline":
                    injected_expected += 1
                    injected_hits += int(hit)
                else:
                    real_expected += 1
                    real_hits += int(hit)
            for key in predicted - set(gold):
                per_risk.setdefault(key[1], {"gold": 0, "tp": 0, "fp": 0})["fp"] += 1
        predicted_total = tp + fp
        precision = tp / predicted_total if predicted_total else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        contracts = len(per_contract)
        per_risk_out = {
            risk_type: {
                "gold": float(bucket["gold"]),
                "tp": float(bucket["tp"]),
                "fp": float(bucket["fp"]),
                "precision": round(bucket["tp"] / max(1, bucket["tp"] + bucket["fp"]), 4),
                "recall": round(bucket["tp"] / max(1, bucket["gold"]), 4),
            }
            for risk_type, bucket in sorted(per_risk.items())
        }
        return RealBenchmarkMethodResult(
            method=method,
            contracts=contracts,
            gold_risks=tp + fn,
            predicted=predicted_total,
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
            high_risk_recall=round(high_hits / max(1, high_expected), 4),
            injected_recall=round(injected_hits / max(1, injected_expected), 4),
            real_clause_recall=round(real_hits / max(1, real_expected), 4),
            fp_per_contract=round(fp / max(1, contracts), 4),
            duration_seconds=round(duration, 2),
            per_risk_type=per_risk_out,
        )


def format_real_benchmark_table(report: RealBenchmarkReport) -> str:
    headers = [
        "method",
        "gold",
        "pred",
        "TP",
        "FP",
        "FN",
        "precision",
        "recall",
        "F1",
        "high_recall",
        "inject_recall",
        "real_recall",
        "FP/contract",
        "secs",
    ]
    rows = [
        [
            m.method,
            str(m.gold_risks),
            str(m.predicted),
            str(m.true_positives),
            str(m.false_positives),
            str(m.false_negatives),
            f"{m.precision:.4f}",
            f"{m.recall:.4f}",
            f"{m.f1:.4f}",
            f"{m.high_risk_recall:.4f}",
            f"{m.injected_recall:.4f}",
            f"{m.real_clause_recall:.4f}",
            f"{m.fp_per_contract:.2f}",
            f"{m.duration_seconds:.1f}",
        ]
        for m in report.methods
    ]
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(v)) for w, v in zip(widths, row)]
    lines = [
        "  ".join(h.ljust(w) for h, w in zip(headers, widths)),
        "  ".join("-" * w for w in widths),
        *["  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in rows],
    ]
    meta = (
        f"dataset={report.dataset} {report.version} | contracts={report.contracts} "
        f"gold={report.gold_risks} negatives={report.negative_clauses}\n"
        f"annotator={report.annotator} | review_status={report.review_status} | llm={report.llm_mode}"
    )
    return meta + "\n" + "\n".join(lines)
