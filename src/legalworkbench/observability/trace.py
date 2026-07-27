"""Execution trace helpers."""

from __future__ import annotations

import time

from legalworkbench.models import ReviewRun


def compute_run_metrics(run: ReviewRun, *, elapsed: float) -> dict[str, float]:
    evidence_findings = sum(1 for finding in run.findings if finding.evidence)
    blocked = sum(1 for finding in run.findings if finding.blocked)
    review_required = sum(1 for finding in run.findings if finding.requires_human_review)
    return {
        "risk_findings": float(len(run.findings)),
        "source_coverage": round(evidence_findings / max(1, len(run.findings)), 4),
        "tool_success_rate": round(sum(1 for trace in run.tool_calls if trace.status == "success") / max(1, len(run.tool_calls)), 4),
        "memory_recall_at_5": 1.0 if run.memory_hits else 0.0,
        "reflection_pass_rate": round(sum(1 for check in run.reflection_checks if check.status == "pass") / max(1, len(run.reflection_checks)), 4),
        "human_review_rate": round(review_required / max(1, len(run.findings)), 4),
        "hallucination_block_rate": round(blocked / max(1, len(run.findings)), 4),
        "context_retention_rate": run.compact_snapshot.retention_rate if run.compact_snapshot else 1.0,
        "elapsed_seconds": round(elapsed, 3),
        "generated_at": round(time.time(), 3),
    }
