"""State-aware compact snapshots for long contract review."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from legalworkbench.models import CompactSnapshot, ReviewRun
from legalworkbench.observability.tokens import estimate_tokens


@dataclass(frozen=True)
class CompactPolicy:
    max_clause_chars: int = 240
    min_contract_tokens: int = 1200
    retain_high_risk: bool = True
    retain_sources: bool = True


class LegalContextCompactor:
    """Preserve review-critical state while reducing long contract context."""

    def __init__(self, policy: CompactPolicy | None = None) -> None:
        self.policy = policy or CompactPolicy()

    def compact(self, run: ReviewRun, *, original_text: str) -> CompactSnapshot:
        retained_clause_ids: list[str] = []
        retained_risk_types: list[str] = []
        parts: list[str] = []
        finding_by_clause = {finding.clause_id: finding for finding in run.findings}
        for clause in run.clauses:
            finding = finding_by_clause.get(clause.clause_id)
            if finding is None and len(parts) >= 8:
                continue
            if finding is not None:
                retained_risk_types.append(finding.risk_type)
            retained_clause_ids.append(clause.clause_id)
            text = clause.text[: self.policy.max_clause_chars]
            risk = f" risk={finding.risk_type}/{finding.risk_level}" if finding else ""
            source = ""
            if finding and finding.evidence:
                source = f" source={finding.evidence[0].source}"
            parts.append(f"{clause.clause_id} {clause.title}{risk}{source}: {text}")
        summary = "\n".join(parts)
        source_tokens = estimate_tokens(original_text)
        retained_tokens = estimate_tokens(summary)
        if source_tokens < self.policy.min_contract_tokens:
            retained_tokens = source_tokens
            summary = "合同未超过压缩阈值，保留完整上下文；关键条款和风险状态仍写入快照。\n" + summary
        return CompactSnapshot(
            snapshot_id=f"cmp_{uuid4().hex[:10]}",
            source_tokens=source_tokens,
            retained_tokens=retained_tokens,
            retention_rate=round(min(1.0, retained_tokens / max(1, source_tokens)), 4),
            retained_clause_ids=retained_clause_ids,
            retained_risk_types=sorted(set(retained_risk_types)),
            summary=summary,
        )
