"""Benchmark runner for RAG, rules, memory, and governance."""

from __future__ import annotations

from pathlib import Path

from legalworkbench.governance import RiskRuleEngine
from legalworkbench.models import BenchmarkResult
from legalworkbench.retrieval import HybridClauseRetriever, retrieve_memories
from legalworkbench.store import WorkbenchStore


class BenchmarkRunner:
    """Run deterministic benchmark cases stored in the workspace."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.store = WorkbenchStore(cwd)
        self.rules = RiskRuleEngine()

    def run(self) -> BenchmarkResult:
        cases = self.store.load_benchmark()
        retriever = HybridClauseRetriever(self.store.load_knowledge())
        memories = self.store.load_memory()
        recall_hits = 0
        source_hits = 0
        expected_total = 0
        memory_hits = 0
        for case in cases:
            evidence = retriever.search(case.contract_text, contract_type=case.contract_type)
            rules = self.rules.evaluate(case.contract_text)
            found = {item.risk_type for item in evidence} | {hit.risk_type for hit in rules}
            expected = set(case.expected_risk_types)
            recall_hits += len(found & expected)
            source_hits += len([risk for risk in expected if any(item.risk_type == risk for item in evidence)])
            expected_total += len(expected)
            memory_hits += 1 if retrieve_memories(memories, case.contract_text, contract_type=case.contract_type) else 0
        return BenchmarkResult(
            cases=len(cases),
            risk_recall_at_10=round(recall_hits / max(1, expected_total), 4),
            source_coverage=round(source_hits / max(1, expected_total), 4),
            memory_recall_at_5=round(memory_hits / max(1, len(cases)), 4),
        )
