"""Risk Reviewer Agent: convert evidence bundles into draft findings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from legalworkbench.agents.base import LegalReviewAgent, ReviewAgentContext
from legalworkbench.agents.evidence import EvidenceBundle
from legalworkbench.governance.rules import KNOWN_RISK_TYPES, match_adverse
from legalworkbench.models import LegalMemory, RetrievedEvidence
from legalworkbench.retrieval import semantic_overlap_score

SEMANTIC_CANDIDATE_MIN_CONFIDENCE = 0.65
SEMANTIC_VERIFICATION_MIN_SCORE = 0.6
SEMANTIC_CANDIDATE_LIMIT = 3
VALID_RISK_LEVELS = {"high", "medium", "low"}


@dataclass(frozen=True)
class SemanticRiskCandidate:
    risk_type: str
    risk_level: str
    adverse_party: str
    evidence_quote: str
    rationale: str
    confidence: float


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
            if skill_implies_risk(
                item.risk_type,
                bundle.query,
                item.score,
                item.rerank_score,
                skill_risk_focus,
            ):
                risk_types.add(item.risk_type)
        nonsemantic_risk_types = set(risk_types)

        if rule_hits:
            # 已有确定性不利模式命中时，结论、风险类型和证据都有锚点；再做
            # 独立发现会重复支付一次远端调用。无规则命中的隐式措辞仍走 LLM
            # 独立发现，因此不会牺牲专门用于补规则盲区的语义路径。
            semantic_candidates = []
            decision_source = "skipped_rule_grounded"
        else:
            semantic_candidates, decision_source = self._discover_semantic_candidates(
                ctx, bundle
            )
        semantic_by_type = {
            candidate.risk_type: candidate for candidate in semantic_candidates
        }
        risk_types.update(semantic_by_type)
        self.emit(
            ctx,
            "semantic_candidates",
            {
                "clause_id": bundle.clause.clause_id,
                "count": len(semantic_candidates),
                "risk_types": sorted(semantic_by_type),
                "decision_source": decision_source,
            },
        )

        drafts: list[DraftRiskFinding] = []
        for risk_type in sorted(risk_types):
            matching_rules = [hit for hit in rule_hits if hit.risk_type == risk_type]
            primary = matching_rules[0] if matching_rules else None
            semantic_candidate = semantic_by_type.get(risk_type)
            matched_evidence = [
                item for item in bundle.evidence if item.risk_type == risk_type
            ]
            if semantic_candidate is not None and not matched_evidence:
                matched_evidence = self._retrieve_semantic_evidence(
                    ctx, bundle, semantic_candidate
                )
            if not matched_evidence and semantic_candidate is None:
                matched_evidence = bundle.evidence[:2]
            risk_level = (
                primary.risk_level
                if primary
                else semantic_candidate.risk_level
                if semantic_candidate is not None
                else matched_evidence[0].risk_level
                if matched_evidence
                else "medium"
            )
            summary = (
                primary.summary
                if primary
                else semantic_candidate.rationale
                if semantic_candidate is not None
                else f"条款与 {risk_type} 风险证据相似，需要复核。"
            )
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
            if semantic_candidate is not None:
                semantic_score = max(semantic_score, semantic_candidate.confidence)
            if primary is not None:
                llm_score = 1.0
            else:
                llm_judgment = ctx.llm.semantic_judgment(
                    clause=bundle.query,
                    risk_type=risk_type,
                    evidence="\n".join(
                        item.body_preview for item in matched_evidence[:3]
                    ),
                )
                llm_score = _safe_score(llm_judgment.get("score"))
            if (
                semantic_candidate is not None
                and risk_type not in nonsemantic_risk_types
                and (
                    not matched_evidence or llm_score < SEMANTIC_VERIFICATION_MIN_SCORE
                )
            ):
                self.emit(
                    ctx,
                    "semantic_candidate_rejected",
                    {
                        "clause_id": bundle.clause.clause_id,
                        "risk_type": risk_type,
                        "reason": (
                            "missing_rag_evidence"
                            if not matched_evidence
                            else "semantic_verification_below_threshold"
                        ),
                        "verification_score": llm_score,
                    },
                )
                continue
            if semantic_candidate is not None:
                llm_score = max(llm_score, semantic_candidate.confidence)
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
                        *(
                            ["llm_semantic_candidate"]
                            if semantic_candidate is not None
                            else []
                        ),
                    ],
                    requires_human_review=bool(
                        primary.requires_human_review
                        if primary
                        else semantic_candidate is not None or risk_level == "high"
                    ),
                )
            )
        self.emit(
            ctx,
            "completed",
            {"clause_id": bundle.clause.clause_id, "drafts": len(drafts)},
        )
        return drafts

    def _discover_semantic_candidates(
        self,
        ctx: ReviewAgentContext,
        bundle: EvidenceBundle,
    ) -> tuple[list[SemanticRiskCandidate], str]:
        decision = ctx.llm.discover_risk_candidates(
            clause=bundle.clause.text,
            contract_type=ctx.run.contract_type,
            allowed_risk_types=list(KNOWN_RISK_TYPES),
        )
        raw_candidates = decision.get("candidates")
        if not isinstance(raw_candidates, list):
            return [], str(decision.get("decision_source") or "")

        validated: dict[str, SemanticRiskCandidate] = {}
        for raw in raw_candidates:
            candidate = _validate_semantic_candidate(raw, bundle.clause.text)
            if candidate is None:
                continue
            existing = validated.get(candidate.risk_type)
            if existing is None or candidate.confidence > existing.confidence:
                validated[candidate.risk_type] = candidate
        candidates = sorted(
            validated.values(), key=lambda item: item.confidence, reverse=True
        )
        return candidates[:SEMANTIC_CANDIDATE_LIMIT], str(
            decision.get("decision_source") or ""
        )

    def _retrieve_semantic_evidence(
        self,
        ctx: ReviewAgentContext,
        bundle: EvidenceBundle,
        candidate: SemanticRiskCandidate,
    ) -> list[RetrievedEvidence]:
        result = self.execute_tool(
            ctx,
            "clause_retriever",
            {
                "query": (
                    f"{bundle.clause.title}\n{candidate.evidence_quote}\n"
                    f"风险类型 {candidate.risk_type}：{candidate.rationale}"
                ),
                "contract_type": ctx.run.contract_type,
                "top_k": 5,
            },
        )
        if result.is_error or not isinstance(result.output, dict):
            return []
        evidence = [
            item
            for item in result.output.get("evidence", [])
            if isinstance(item, RetrievedEvidence)
            and item.risk_type == candidate.risk_type
        ][:5]
        ctx.evidence_total += len(evidence)
        return evidence


def _validate_semantic_candidate(
    raw: Any, clause_text: str
) -> SemanticRiskCandidate | None:
    if not isinstance(raw, dict):
        return None
    risk_type = str(raw.get("risk_type") or "").strip()
    risk_level = str(raw.get("risk_level") or "medium").strip().lower()
    adverse_party = str(raw.get("adverse_party") or "").strip()
    evidence_quote = str(raw.get("evidence_quote") or "").strip()
    rationale = str(raw.get("rationale") or "").strip()
    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None
    if risk_type not in KNOWN_RISK_TYPES or risk_level not in VALID_RISK_LEVELS:
        return None
    if not adverse_party or not rationale or len(evidence_quote) < 4:
        return None
    if confidence < SEMANTIC_CANDIDATE_MIN_CONFIDENCE or confidence > 1.0:
        return None
    if not _quote_is_grounded(evidence_quote, clause_text):
        return None
    return SemanticRiskCandidate(
        risk_type=risk_type,
        risk_level=risk_level,
        adverse_party=adverse_party,
        evidence_quote=evidence_quote,
        rationale=rationale,
        confidence=confidence,
    )


def _quote_is_grounded(quote: str, clause_text: str) -> bool:
    if quote in clause_text:
        return True
    normalized_quote = "".join(quote.split())
    normalized_clause = "".join(clause_text.split())
    return bool(normalized_quote) and normalized_quote in normalized_clause


def _safe_score(value: Any) -> float:
    try:
        score = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, score))


def evidence_implies_risk(risk_type: str, query: str, score: float) -> bool:
    """检索证据升级为风险预测的门控。

    旧版按话题关键词放行（条款提到"付款"就算 payment 风险），在真实均衡合同上
    造成误报洪水（real benchmark 实测 rag_only precision 0.06）。现在与规则引擎
    共用不利模式库：条款必须出现真正的不利语言，检索证据才能升级为风险。
    """

    if risk_type == "general":
        return False
    return match_adverse(risk_type, query) and score >= 1.0


def skill_implies_risk(
    risk_type: str,
    query: str,
    score: float,
    rerank_score: float,
    skill_risk_focus: set[str],
) -> bool:
    if risk_type == "general" or risk_type not in skill_risk_focus:
        return False
    # 技能画像只放宽检索分数门槛，不放宽不利模式要求
    return match_adverse(risk_type, query) and (score >= 0.8 or rerank_score >= 1.2)
