"""Baseline comparison evaluators for legal risk detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from legalworkbench.evals.human_benchmark import human_benchmark_dir, load_human_benchmark
from legalworkbench.governance import RiskRuleEngine
from legalworkbench.parser import parse_clauses
from legalworkbench.retrieval import HybridClauseRetriever
from legalworkbench.store import WorkbenchStore

BaselineDataset = Literal["synthetic", "human", "both"]
BASELINE_METHODS = ("rule_only", "rag_only", "full_system")


@dataclass(frozen=True)
class BaselineResultRow:
    dataset: str
    method: str
    cases: int
    expected_risks: int
    risk_recall_at_10: float
    source_coverage_at_10: float
    high_risk_recall: float | None = None
    human_review_capture_rate: float | None = None
    tool_success_rate: float = 1.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class BaselineEvaluator:
    """Compare rule-only, RAG-only, and full risk detection on one dataset."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.store = WorkbenchStore(self.cwd)
        self.rules = RiskRuleEngine()

    def run(self, *, dataset: BaselineDataset = "both") -> list[BaselineResultRow]:
        if dataset not in {"synthetic", "human", "both"}:
            raise ValueError(f"Unsupported dataset: {dataset}")
        rows: list[BaselineResultRow] = []
        if dataset in {"synthetic", "both"}:
            rows.extend(self._evaluate_synthetic())
        if dataset in {"human", "both"}:
            rows.extend(self._evaluate_human())
        return rows

    def _evaluate_synthetic(self) -> list[BaselineResultRow]:
        cases = self.store.load_benchmark()
        retriever = HybridClauseRetriever(self.store.load_knowledge())
        stats = {method: _empty_stats() for method in BASELINE_METHODS}

        for case in cases:
            expected = set(case.expected_risk_types)
            rule_risks = {hit.risk_type for hit in self.rules.evaluate(case.contract_text)}
            evidence = retriever.search(case.contract_text, contract_type=case.contract_type, top_k=10)
            evidence_risks = {item.risk_type for item in evidence}

            for method in BASELINE_METHODS:
                predicted = _predicted_risks(method, rule_risks, evidence_risks)
                stats[method]["cases"] += 1
                stats[method]["expected"] += len(expected)
                stats[method]["recall_hits"] += len(predicted & expected)
                if method != "rule_only":
                    stats[method]["source_hits"] += len(evidence_risks & expected)

        return [_row("synthetic", method, stats[method]) for method in BASELINE_METHODS]

    def _evaluate_human(self) -> list[BaselineResultRow]:
        contracts = load_human_benchmark(self.cwd)
        if not contracts:
            return []

        retriever = HybridClauseRetriever(self.store.load_knowledge())
        stats = {method: _empty_stats() for method in BASELINE_METHODS}

        for contract in contracts:
            contract_path = (human_benchmark_dir(self.cwd) / contract.file).resolve()
            text = contract_path.read_text(encoding="utf-8")
            clauses = {clause.clause_id: clause for clause in parse_clauses(text)}

            for annotation in contract.annotations:
                clause = clauses.get(annotation.clause_id)
                rule_hits = self.rules.evaluate(clause.text) if clause is not None else []
                evidence = (
                    retriever.search(clause.text, contract_type=contract.contract_type, top_k=10)
                    if clause is not None
                    else []
                )
                rule_risks = {hit.risk_type for hit in rule_hits}
                evidence_risks = {item.risk_type for item in evidence}

                for method in BASELINE_METHODS:
                    predicted = _predicted_risks(method, rule_risks, evidence_risks)
                    stats[method]["cases"] = len(contracts)
                    stats[method]["expected"] += 1
                    if annotation.risk_type in predicted:
                        stats[method]["recall_hits"] += 1
                    if method != "rule_only" and annotation.risk_type in evidence_risks:
                        stats[method]["source_hits"] += 1
                    if annotation.risk_level == "high":
                        stats[method]["high_expected"] += 1
                        if annotation.risk_type in predicted:
                            stats[method]["high_hits"] += 1
                    if annotation.requires_human_review:
                        stats[method]["human_expected"] += 1
                        if _captures_human_review(method, annotation.risk_type, annotation.risk_level, rule_hits, evidence):
                            stats[method]["human_hits"] += 1

        return [_row("human", method, stats[method], include_human_metrics=True) for method in BASELINE_METHODS]


def format_baseline_table(rows: list[BaselineResultRow]) -> str:
    headers = [
        "dataset",
        "method",
        "cases",
        "expected",
        "recall@10",
        "source@10",
        "high_recall",
        "human_review",
        "tool_success",
    ]
    data = [
        [
            row.dataset,
            row.method,
            str(row.cases),
            str(row.expected_risks),
            _fmt(row.risk_recall_at_10),
            _fmt(row.source_coverage_at_10),
            _fmt(row.high_risk_recall),
            _fmt(row.human_review_capture_rate),
            _fmt(row.tool_success_rate),
        ]
        for row in rows
    ]
    widths = [len(header) for header in headers]
    for item in data:
        widths = [max(width, len(value)) for width, value in zip(widths, item)]
    header_line = "  ".join(header.ljust(width) for header, width in zip(headers, widths))
    sep = "  ".join("-" * width for width in widths)
    body = ["  ".join(value.ljust(width) for value, width in zip(item, widths)) for item in data]
    return "\n".join([header_line, sep, *body])


def _empty_stats() -> dict[str, int]:
    return {
        "cases": 0,
        "expected": 0,
        "recall_hits": 0,
        "source_hits": 0,
        "high_expected": 0,
        "high_hits": 0,
        "human_expected": 0,
        "human_hits": 0,
    }


def _predicted_risks(method: str, rule_risks: set[str], evidence_risks: set[str]) -> set[str]:
    if method == "rule_only":
        return rule_risks
    if method == "rag_only":
        return evidence_risks
    return rule_risks | evidence_risks


def _captures_human_review(method: str, risk_type: str, risk_level: str, rule_hits: list[object], evidence: list[object]) -> bool:
    rule_review = any(getattr(hit, "risk_type", "") == risk_type and getattr(hit, "requires_human_review", False) for hit in rule_hits)
    evidence_review = any(getattr(item, "risk_type", "") == risk_type and getattr(item, "risk_level", "") == "high" for item in evidence)
    if method == "rule_only":
        return rule_review or (risk_level == "high" and any(getattr(hit, "risk_type", "") == risk_type for hit in rule_hits))
    if method == "rag_only":
        return evidence_review or (risk_level == "high" and any(getattr(item, "risk_type", "") == risk_type for item in evidence))
    predicted = any(getattr(hit, "risk_type", "") == risk_type for hit in rule_hits) or any(getattr(item, "risk_type", "") == risk_type for item in evidence)
    return predicted and (risk_level == "high" or rule_review or evidence_review)


def _row(dataset: str, method: str, stats: dict[str, int], *, include_human_metrics: bool = False) -> BaselineResultRow:
    expected = stats["expected"]
    high_expected = stats["high_expected"]
    human_expected = stats["human_expected"]
    return BaselineResultRow(
        dataset=dataset,
        method=method,
        cases=stats["cases"],
        expected_risks=expected,
        risk_recall_at_10=round(stats["recall_hits"] / max(1, expected), 4),
        source_coverage_at_10=round(stats["source_hits"] / max(1, expected), 4),
        high_risk_recall=round(stats["high_hits"] / max(1, high_expected), 4) if include_human_metrics else None,
        human_review_capture_rate=round(stats["human_hits"] / max(1, human_expected), 4) if include_human_metrics else None,
    )


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.4f}"
