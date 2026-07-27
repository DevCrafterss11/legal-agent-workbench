"""Clause Rewriter Agent: generate contract-safe rewrite suggestions."""

from __future__ import annotations

from legalworkbench.agents.base import LegalReviewAgent, ReviewAgentContext
from legalworkbench.agents.risk_reviewer import DraftRiskFinding


class ClauseRewriterAgent(LegalReviewAgent):
    name = "clause_rewriter_agent"
    role = "rewrite_generation"

    def rewrite(self, ctx: ReviewAgentContext, draft: DraftRiskFinding) -> str:
        ctx.run.status = "rewriting"
        self.emit(
            ctx,
            "started",
            {"clause_id": draft.bundle.clause.clause_id, "risk_type": draft.risk_type},
        )
        result = self.execute_tool(
            ctx,
            "clause_rewriter",
            {
                "risk_type": draft.risk_type,
                "rule_suggestion": draft.primary_rule.suggestion if draft.primary_rule else "",
                "memories": draft.clause_memories,
                "evidence": draft.matched_evidence,
            },
        )
        suggestion = str(result.output or "")
        self.emit(
            ctx,
            "completed",
            {"clause_id": draft.bundle.clause.clause_id, "risk_type": draft.risk_type},
        )
        return suggestion
