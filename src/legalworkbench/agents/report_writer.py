"""Report Writer Agent: final metrics, report rendering, and persistence."""

from __future__ import annotations

import time

from legalworkbench.agents.base import AgentExecutionError, LegalReviewAgent, ReviewAgentContext
from legalworkbench.observability import compute_run_metrics, estimate_messages_tokens, estimate_tokens
from legalworkbench.paths import runs_dir


class ReportWriterAgent(LegalReviewAgent):
    name = "report_writer_agent"
    role = "report_generation"

    def finalize_context(self, ctx: ReviewAgentContext) -> None:
        self.emit(ctx, "context_finalize_started", {"findings": len(ctx.run.findings)})
        ctx.run.compact_snapshot = ctx.compactor.compact(ctx.run, original_text=ctx.contract_text)
        ctx.run.mcp_context.update(ctx.connectors.context(connect_mcp=ctx.connect_mcp))
        blocked = sum(1 for finding in ctx.run.findings if finding.blocked)
        ctx.run.status = "blocked" if blocked else "completed"
        ctx.run.token_usage = {
            "contract_tokens": estimate_tokens(ctx.contract_text),
            "evidence_tokens": estimate_messages_tokens(
                [item.body_preview for finding in ctx.run.findings for item in finding.evidence]
            ),
            "memory_tokens": estimate_messages_tokens(
                [memory.summary + memory.approved_advice for memory in ctx.run.memory_hits]
            ),
            "compact_tokens": ctx.run.compact_snapshot.retained_tokens if ctx.run.compact_snapshot else 0,
        }
        ctx.run.metrics = compute_run_metrics(ctx.run, elapsed=time.time() - ctx.started_at)
        ctx.run.metrics["retrieved_evidence"] = float(ctx.evidence_total)
        self.emit(ctx, "context_finalize_completed", {"status": ctx.run.status})

    def write_report(self, ctx: ReviewAgentContext) -> None:
        report_path = runs_dir(ctx.cwd) / f"{ctx.run.review_run_id}.md"
        result = self.execute_tool(ctx, "report_export", {"run": ctx.run, "path": report_path})
        if result.is_error:
            raise AgentExecutionError(result.summary)
        self.emit(ctx, "completed", {"report_path": ctx.run.report_path})
