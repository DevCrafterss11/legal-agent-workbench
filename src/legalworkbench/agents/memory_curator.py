"""Memory Curator Agent: consolidate reviewed findings into long-term memory."""

from __future__ import annotations

from legalworkbench.agents.base import LegalReviewAgent, ReviewAgentContext


class MemoryCuratorAgent(LegalReviewAgent):
    name = "memory_curator_agent"
    role = "long_term_memory"

    def consolidate(self, ctx: ReviewAgentContext) -> None:
        self.emit(ctx, "started", {"findings": len(ctx.run.findings)})
        # 召回强化：本次审查实际命中的记忆记录使用反馈，影响后续召回排序与驱逐
        used = ctx.memory_store.mark_used(list(ctx.memory_hits.keys()))
        created = ctx.memory_store.consolidate_from_run(ctx.run)
        self.emit(ctx, "completed", {"created_memories": len(created), "reinforced_memories": used})
