"""Compliance Auditor Agent: permission guard and reflection review."""

from __future__ import annotations

from legalworkbench.agents.base import LegalReviewAgent, ReviewAgentContext
from legalworkbench.agents.risk_reviewer import DraftRiskFinding
from legalworkbench.models import RiskFinding


class ComplianceAuditorAgent(LegalReviewAgent):
    name = "compliance_auditor_agent"
    role = "permission_and_reflection"

    def approve_finding(
        self,
        ctx: ReviewAgentContext,
        draft: DraftRiskFinding,
        *,
        suggestion: str,
        finding_id: str,
    ) -> RiskFinding:
        self.emit(
            ctx,
            "started",
            {"finding_id": finding_id, "risk_type": draft.risk_type, "risk_level": draft.risk_level},
        )
        result = self.execute_tool(
            ctx,
            "permission_guard",
            {
                "risk_level": draft.risk_level,
                "evidence_count": len(draft.matched_evidence),
                "suggestion": suggestion,
                "requires_human_review": draft.requires_human_review,
            },
        )
        guard = result.output if isinstance(result.output, dict) else {"ok": False, "reason": result.summary}
        finding = RiskFinding(
            finding_id=finding_id,
            clause_id=draft.bundle.clause.clause_id,
            clause_title=draft.bundle.clause.title,
            risk_type=draft.risk_type,
            risk_level=draft.risk_level,
            summary=draft.summary,
            evidence=draft.matched_evidence[:5],
            rule_hits=draft.rule_hits,
            semantic_score=draft.semantic_score,
            confidence=round(draft.confidence, 4),
            source_coverage=round(draft.source_coverage, 4),
            suggestion=suggestion,
            requires_human_review=guard.get("reason") == "human_review_required",
            blocked=not bool(guard.get("ok")),
            block_reason="" if guard.get("ok") else str(guard.get("reason") or "blocked"),
        )
        self.emit(
            ctx,
            "completed",
            {
                "finding_id": finding.finding_id,
                "blocked": finding.blocked,
                "requires_human_review": finding.requires_human_review,
            },
        )
        return finding

    def reflect(self, ctx: ReviewAgentContext) -> None:
        ctx.run.status = "compliance_reviewing"
        self.emit(ctx, "reflection_started", {"findings": len(ctx.run.findings)})
        ctx.reflection.apply(ctx.run)
        self.emit(ctx, "reflection_completed", {"checks": len(ctx.run.reflection_checks)})
