"""Risk Reviewer Agent: convert evidence bundles into draft findings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from legalworkbench.agents.base import LegalReviewAgent, ReviewAgentContext
from legalworkbench.agents.evidence import EvidenceBundle
from legalworkbench.models import LegalMemory, RetrievedEvidence
from legalworkbench.retrieval import semantic_overlap_score


@dataclass
class DraftRiskFinding:
    bundle: EvidenceBundle
    risk_type: str
    risk_level: str
    summary: str
    matched_evidence: list[RetrievedEvidence]
    clause_memories: list[LegalMemory]
    matching_rules: list[Any]
    primary_rule: Any | None
    semantic_score: float
    source_coverage: float
    confidence: float
    rule_hits: list[str]
    requires_human_review: bool


class RiskReviewerAgent(LegalReviewAgent):
    name = "risk_reviewer_agent"
    role = "risk_detection"

    def draft_findings(
        self,
        ctx: ReviewAgentContext,
        bundle: EvidenceBundle,
        *,
        skill_risk_focus: set[str],
    ) -> list[DraftRiskFinding]:
        ctx.run.status = "risk_checking"
        self.emit(ctx, "started", {"clause_id": bundle.clause.clause_id})
        risk_result = self.execute_tool(ctx, "risk_rule", {"text": bundle.query})
        rule_hits = [] if risk_result.is_error else list(risk_result.output)
        risk_types = {hit.risk_type for hit in rule_hits}
        for item in bundle.evidence[:3]:
            if evidence_implies_risk(item.risk_type, bundle.query, item.score):
                risk_types.add(item.risk_type)
        for item in bundle.evidence[:5]:
            if skill_implies_risk(item.risk_type, bundle.query, item.score, item.rerank_score, skill_risk_focus):
                risk_types.add(item.risk_type)

        drafts: list[DraftRiskFinding] = []
        for risk_type in sorted(risk_types):
            matching_rules = [hit for hit in rule_hits if hit.risk_type == risk_type]
            primary = matching_rules[0] if matching_rules else None
            matched_evidence = [item for item in bundle.evidence if item.risk_type == risk_type] or bundle.evidence[:2]
            risk_level = primary.risk_level if primary else (matched_evidence[0].risk_level if matched_evidence else "medium")
            summary = primary.summary if primary else f"条款与 {risk_type} 风险证据相似，需要复核。"
            semantic_score = max(
                [
                    semantic_overlap_score(
                        bundle.query,
                        item.title + item.body_preview + item.risk_type,
                    )
                    for item in matched_evidence
                ],
                default=0.0,
            )
            llm_judgment = ctx.llm.semantic_judgment(
                clause=bundle.query,
                risk_type=risk_type,
                evidence="\n".join(item.body_preview for item in matched_evidence[:3]),
            )
            llm_score = float(llm_judgment.get("score") or 0.0)
            source_coverage = min(1.0, len(matched_evidence) / 2)
            rule_confidence = 0.3 if primary else 0.0
            skill_confidence = 0.08 if risk_type in skill_risk_focus else 0.0
            confidence = min(
                0.99,
                semantic_score * 0.3
                + llm_score * 0.2
                + source_coverage * 0.2
                + rule_confidence
                + skill_confidence
                + (0.1 if bundle.memories else 0.0),
            )
            drafts.append(
                DraftRiskFinding(
                    bundle=bundle,
                    risk_type=risk_type,
                    risk_level=risk_level,
                    summary=summary,
                    matched_evidence=matched_evidence,
                    clause_memories=bundle.memories,
                    matching_rules=matching_rules,
                    primary_rule=primary,
                    semantic_score=semantic_score,
                    source_coverage=source_coverage,
                    confidence=confidence,
                    rule_hits=[
                        *[hit.rule_id for hit in matching_rules],
                        *(["skill_focus"] if risk_type in skill_risk_focus else []),
                    ],
                    requires_human_review=bool(primary.requires_human_review if primary else risk_level == "high"),
                )
            )
        self.emit(ctx, "completed", {"clause_id": bundle.clause.clause_id, "drafts": len(drafts)})
        return drafts


def evidence_implies_risk(risk_type: str, query: str, score: float) -> bool:
    keywords = {
        "auto_renewal": ("自动续约", "续约", "renewal"),
        "unlimited_liability": ("无限", "不设", "全部损失", "间接损失", "预期利润", "赔偿", "liability"),
        "data_security": ("数据", "个人信息", "泄露", "安全措施", "data"),
        "jurisdiction": ("管辖", "法院", "仲裁", "争议", "jurisdiction"),
        "payment_acceptance": ("付款", "支付", "验收", "交付", "payment"),
        "payment_cycle": ("付款", "支付", "预付", "一次性", "验收", "交付", "payment"),
        "ip_ownership": ("知识产权", "成果", "归属", "授权", "许可", "ip"),
        "sla_remedy": ("sla", "服务可用性", "响应", "故障", "补救", "抵扣"),
    }
    if risk_type == "general":
        return False
    if not any(token in query for token in keywords.get(risk_type, ())):
        return False
    return score >= 2.0


def skill_implies_risk(risk_type: str, query: str, score: float, rerank_score: float, skill_risk_focus: set[str]) -> bool:
    if risk_type == "general" or risk_type not in skill_risk_focus:
        return False
    if evidence_implies_risk(risk_type, query, score):
        return True
    return score >= 1.2 or rerank_score >= 1.8 or semantic_overlap_score(query, risk_type) >= 0.08
