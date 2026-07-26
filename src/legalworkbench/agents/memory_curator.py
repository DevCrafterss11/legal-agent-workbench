"""Memory Curator Agent: consolidate reviewed findings into long-term memory."""

from __future__ import annotations

from legalworkbench.agents.base import LegalReviewAgent, ReviewAgentContext


class MemoryCuratorAgent(LegalReviewAgent):
    name = "memory_curator_agent"
    role = "long_term_memory"

    def consolidate(self, ctx: ReviewAgentContext) -> None:
        self.emit(ctx, "started", {"findings": len(ctx.run.findings)})
        created = ctx.memory_store.consolidate_from_run(ctx.run)
        self.emit(ctx, "completed", {"created_memories": len(created)})
