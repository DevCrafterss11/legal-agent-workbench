"""Evidence Agent: query planning, RAG retrieval, and bounded LLM-driven re-query.

检索是一个有界的观察-决策-行动循环：首轮检索后评估证据质量，证据不足时由
LLM 决策是否改写查询并重试（最多一次），两轮证据按 entry_id 去重合并。
决策失败或 local provider 下按确定性规则回落（仅空证据时重试），循环上界
写死在代码里，模型只能决定"是否/如何改写查询"，不能决定"再来多少轮"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from legalworkbench.agents.base import LegalReviewAgent, ReviewAgentContext
from legalworkbench.models import ContractClause, LegalMemory, RetrievedEvidence

WEAK_EVIDENCE_COUNT = 2
WEAK_RERANK_SCORE = 6.0


@dataclass
class EvidenceBundle:
    clause: ContractClause
    query: str
    evidence: list[RetrievedEvidence]
    memories: list[LegalMemory]
    refined_query: str = field(default="")


class EvidenceAgent(LegalReviewAgent):
    name = "evidence_agent"
    role = "rag_evidence"

    def retrieve_clause(self, ctx: ReviewAgentContext, clause: ContractClause, *, top_k: int) -> EvidenceBundle | None:
        ctx.run.status = "retrieving"
        query = f"{clause.title}\n{clause.text}"
        self.emit(ctx, "started", {"clause_id": clause.clause_id, "top_k": top_k})
        result = self.execute_tool(
            ctx,
            "clause_retriever",
            {"query": query, "contract_type": ctx.run.contract_type, "top_k": top_k},
        )
        if result.is_error:
            return None
        evidence = list(result.output["evidence"])
        memories = list(result.output["memories"])

        refined_query = ""
        if self._is_weak(evidence):
            refined_query, evidence = self._refine_once(ctx, clause, evidence, top_k=top_k)

        ctx.evidence_total += len(evidence)
        for memory in memories:
            ctx.memory_hits[memory.memory_id] = memory
        self.emit(
            ctx,
            "completed",
            {
                "clause_id": clause.clause_id,
                "evidence": len(evidence),
                "memory_hits": len(memories),
                "refined": bool(refined_query),
            },
        )
        return EvidenceBundle(clause=clause, query=query, evidence=evidence, memories=memories, refined_query=refined_query)

    def _is_weak(self, evidence: list[RetrievedEvidence]) -> bool:
        if not evidence:
            return True
        top_score = max(item.rerank_score for item in evidence)
        return len(evidence) < WEAK_EVIDENCE_COUNT or top_score < WEAK_RERANK_SCORE

    def _refine_once(
        self,
        ctx: ReviewAgentContext,
        clause: ContractClause,
        evidence: list[RetrievedEvidence],
        *,
        top_k: int,
    ) -> tuple[str, list[RetrievedEvidence]]:
        decision = ctx.llm.decide(
            task="refine_query",
            payload={
                "clause_title": clause.title,
                "clause_text": clause.text[:400],
                "contract_type": ctx.run.contract_type,
                "evidence_count": len(evidence),
                "top_rerank_score": max((item.rerank_score for item in evidence), default=0.0),
                "instruction": (
                    "证据不足。判断是否用更聚焦的检索式重试一次。"
                    '返回 {"refine": bool, "query": str, "reason": str}。'
                ),
            },
            fallback={"refine": False},
        )
        refined_query = str(decision.get("query") or "").strip()
        if not decision.get("refine") or not refined_query:
            return "", evidence
        self.emit(
            ctx,
            "query_refined",
            {
                "clause_id": clause.clause_id,
                "query": refined_query[:120],
                "decision_source": decision.get("decision_source", ""),
            },
        )
        retry = self.execute_tool(
            ctx,
            "clause_retriever",
            {"query": refined_query, "contract_type": ctx.run.contract_type, "top_k": top_k},
        )
        if retry.is_error:
            return refined_query, evidence
        merged: dict[str, RetrievedEvidence] = {item.entry_id: item for item in evidence}
        for item in retry.output["evidence"]:
            existing = merged.get(item.entry_id)
            if existing is None or item.rerank_score > existing.rerank_score:
                merged[item.entry_id] = item
        combined = sorted(merged.values(), key=lambda item: (item.rerank_score, item.score), reverse=True)
        return refined_query, combined[: max(top_k, len(evidence))]
