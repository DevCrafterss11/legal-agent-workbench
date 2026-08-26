"""Clause rewrite tool."""

from __future__ import annotations

from typing import Any

from legalworkbench.models import LegalMemory, RetrievedEvidence
from legalworkbench.governance import ToolAccess, ToolPolicy
from legalworkbench.tools.base import ToolContext, ToolResult


class ClauseRewriterTool:
    name = "clause_rewriter"
    description = "Generate source-grounded rewrite suggestions for risky clauses."
    policy = ToolPolicy("legal.compute", ToolAccess.COMPUTE)

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        risk_type = str(arguments.get("risk_type") or "general")
        rule_suggestion = str(arguments.get("rule_suggestion") or "")
        memories = list(arguments.get("memories") or [])
        evidence = list(arguments.get("evidence") or [])
        suggestion = (
            _suggest_from_memory(memories, risk_type)
            or _suggest_from_evidence(evidence, risk_type)
            or _suggest_from_risk_type(risk_type)
            or rule_suggestion
        )
        return ToolResult(output=suggestion, summary=suggestion[:160])


def _suggest_from_memory(memories: list[Any], risk_type: str) -> str:
    candidates: list[LegalMemory] = []
    for memory in memories:
        if isinstance(memory, LegalMemory) and memory.risk_type == risk_type and memory.approved_advice:
            candidates.append(memory)
    if not candidates:
        return ""
    best = max(candidates, key=lambda memory: (_specificity_score(memory.approved_advice), memory.confidence, memory.approved_by_human))
    if _specificity_score(best.approved_advice) >= 2 or not _suggest_from_risk_type(risk_type):
        return best.approved_advice
    return ""


def _suggest_from_evidence(evidence: list[Any], risk_type: str) -> str:
    for item in evidence:
        if isinstance(item, RetrievedEvidence) and item.risk_type == risk_type:
            suggestion = _suggest_from_risk_type(risk_type)
            if suggestion:
                return suggestion
    return ""


def _suggest_from_risk_type(risk_type: str) -> str:
    suggestions = {
        "unlimited_liability": (
            "建议改为：乙方仅对因其违约造成的直接损失承担赔偿责任，累计赔偿总额不超过事故发生前十二个月甲方已实际支付的服务费用；"
            "间接损失、预期利润损失、商誉损失、业务中断损失及惩罚性赔偿不纳入赔偿范围。"
        ),
        "auto_renewal": (
            "建议改为：协议期满前至少三十日，任一方可通过书面通知明确不续约；未完成有效通知的，续约期限、价格和服务范围应另行书面确认。"
        ),
        "data_security": (
            "建议补充：乙方仅在履行本协议所必需范围内处理甲方数据，应采取不低于行业通行标准的安全措施；"
            "发生数据安全事件时，乙方应在二十四小时内通知甲方并配合处置，双方按过错和责任边界承担相应责任。"
        ),
        "payment_acceptance": (
            "建议改为：甲方付款义务以乙方完成对应交付、甲方验收通过并收到合法有效发票为前提；未验收或存在重大缺陷的，甲方有权暂缓支付相应款项。"
        ),
        "payment_cycle": (
            "建议改为：合同款项按交付进度分期支付，预付款比例不超过合同总金额的 30%，尾款在最终验收通过并收到合法有效发票后支付。"
        ),
        "ip_ownership": (
            "建议明确：为甲方定制形成的交付成果及相关知识产权归甲方所有；乙方保留其既有背景知识产权，但仅在履约必要范围内授予甲方永久、不可撤销的使用许可。"
        ),
        "sla_remedy": (
            "建议补充：服务可用性、故障等级、响应时限、恢复时限及服务抵扣标准；连续或重大 SLA 未达标时，甲方有权要求整改、服务抵扣或解除合同。"
        ),
        "jurisdiction": (
            "建议改为：因本协议产生的争议，双方应先友好协商；协商不成的，提交甲方所在地有管辖权的人民法院，或提交双方认可的中立仲裁机构解决。"
        ),
    }
    return suggestions.get(risk_type, "")


def _specificity_score(text: str) -> int:
    score = 0
    score += 1 if len(text) >= 45 else 0
    score += 1 if any(term in text for term in ("建议改为", "建议补充", "建议明确")) else 0
    score += 1 if any(term in text for term in ("不超过", "至少", "不低于", "验收通过", "二十四小时", "三十日", "30")) else 0
    score += 1 if any(term in text for term in ("直接损失", "间接损失", "预期利润", "责任边界", "通知时限", "交付成果")) else 0
    return score
