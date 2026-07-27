"""Parser Agent: contract structure extraction."""

from __future__ import annotations

from legalworkbench.agents.base import AgentExecutionError, LegalReviewAgent, ReviewAgentContext


class ParserAgent(LegalReviewAgent):
    name = "parser_agent"
    role = "contract_structure"

    def run(self, ctx: ReviewAgentContext) -> None:
        ctx.run.status = "parsing"
        self.emit(ctx, "started", {"stage": ctx.run.status})
        result = self.execute_tool(ctx, "contract_parser", {"text": ctx.contract_text})
        if result.is_error:
            raise AgentExecutionError(result.summary)
        parsed = result.output
        ctx.run.contract_type = parsed["contract_type"]
        ctx.run.clauses = parsed["clauses"]
        self.emit(
            ctx,
            "completed",
            {"contract_type": ctx.run.contract_type, "clauses": len(ctx.run.clauses)},
        )
