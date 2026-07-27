"""Human-annotated benchmark loading and evaluation."""

from __future__ import annotations

import json
import time
from pathlib import Path

from legalworkbench.governance import RiskRuleEngine
from legalworkbench.models import HumanBenchmarkContract, HumanBenchmarkResult
from legalworkbench.parser import parse_clauses
from legalworkbench.retrieval import HybridClauseRetriever
from legalworkbench.store import WorkbenchStore


def human_benchmark_dir(cwd: str | Path | None = None) -> Path:
    return Path(cwd or Path.cwd()).resolve() / "data" / "human_benchmark"


def human_benchmark_annotations_path(cwd: str | Path | None = None) -> Path:
    return human_benchmark_dir(cwd) / "annotations.json"


def load_human_benchmark(cwd: str | Path | None = None) -> list[HumanBenchmarkContract]:
    path = human_benchmark_annotations_path(cwd)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    contracts = raw.get("contracts", raw) if isinstance(raw, dict) else raw
    if not isinstance(contracts, list):
        return []
    return [HumanBenchmarkContract.model_validate(item) for item in contracts]


class HumanBenchmarkRunner:
    """Evaluate clause-level predictions against curated legal annotations."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.rules = RiskRuleEngine()
        self.store = WorkbenchStore(self.cwd)

    def run(self) -> HumanBenchmarkResult:
        contracts = load_human_benchmark(self.cwd)
        retriever = HybridClauseRetriever(self.store.load_knowledge())
        annotated = 0
        recall_hits = 0
        rule_hits = 0
        source_hits = 0
        high_expected = 0
        high_hits = 0
        human_review_expected = 0
        human_review_hits = 0
        for contract in contracts:
            contract_path = (human_benchmark_dir(self.cwd) / contract.file).resolve()
            text = contract_path.read_text(encoding="utf-8")
            clauses = {clause.clause_id: clause for clause in parse_clauses(text)}
            for annotation in contract.annotations:
                annotated += 1
                clause = clauses.get(annotation.clause_id)
                if clause is None:
                    continue
                rule_risks = {hit.risk_type for hit in self.rules.evaluate(clause.text)}
                evidence = retriever.search(clause.text, contract_type=contract.contract_type, top_k=10)
                evidence_risks = {item.risk_type for item in evidence}
                predicted = rule_risks | evidence_risks
                if annotation.risk_type in predicted:
                    recall_hits += 1
                if annotation.risk_type in rule_risks:
                    rule_hits += 1
                if annotation.risk_type in evidence_risks:
                    source_hits += 1
                if annotation.risk_level == "high":
                    high_expected += 1
                    if annotation.risk_type in predicted:
                        high_hits += 1
                if annotation.requires_human_review:
                    human_review_expected += 1
                    if annotation.risk_type in rule_risks or annotation.risk_level == "high":
                        human_review_hits += 1
        return HumanBenchmarkResult(
            contracts=len(contracts),
            annotated_risks=annotated,
            risk_recall_at_10=round(recall_hits / max(1, annotated), 4),
            rule_recall=round(rule_hits / max(1, annotated), 4),
            source_coverage_at_10=round(source_hits / max(1, annotated), 4),
            high_risk_recall=round(high_hits / max(1, high_expected), 4),
            human_review_capture_rate=round(human_review_hits / max(1, human_review_expected), 4),
            evaluated_at=time.time(),
        )
