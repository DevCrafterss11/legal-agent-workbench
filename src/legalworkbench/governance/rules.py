"""Risk rules and answer-level permission guard."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    risk_type: str
    risk_level: str
    summary: str
    suggestion: str
    requires_human_review: bool = False


KNOWN_RISK_TYPES: tuple[str, ...] = (
    "auto_renewal",
    "unlimited_liability",
    "data_security",
    "jurisdiction",
    "payment_acceptance",
    "payment_cycle",
    "ip_ownership",
    "sla_remedy",
    "confidentiality",
    "termination_notice",
    "force_majeure",
    "deposit_return",
    "prepaid_refund",
)


class RiskRuleEngine:
    """Deterministic risk rules that complement RAG evidence."""

    def evaluate(self, text: str) -> list[RuleHit]:
        hits: list[RuleHit] = []
        if any(term in text for term in ("不设赔偿责任上限", "无限责任", "全部损失")):
            hits.append(
                RuleHit(
                    rule_id="rule_unlimited_liability",
                    risk_type="unlimited_liability",
                    risk_level="high",
                    summary="条款可能要求承担无限责任或过宽赔偿范围。",
                    suggestion="建议设置赔偿责任上限，并排除间接损失、预期利润损失等扩张责任。",
                    requires_human_review=True,
                )
            )
        if "自动续约" in text and (
            not any(term in text for term in ("提前", "通知", "取消"))
            or any(term in text for term in ("未约定提前", "未约定通知", "缺少明确提前通知", "未明确取消", "未约定取消"))
        ):
            hits.append(
                RuleHit(
                    rule_id="rule_auto_renewal_notice",
                    risk_type="auto_renewal",
                    risk_level="medium",
                    summary="自动续约条款缺少明确提前通知或取消路径。",
                    suggestion="建议增加提前 30 天书面通知和明确取消方式。",
                )
            )
        if any(term in text for term in ("客户数据", "个人信息", "数据泄露")) and (
            not any(term in text for term in ("通知时限", "安全措施", "保存期限"))
            or any(term in text for term in ("未约定数据泄露", "未约定通知时限", "未约定安全措施", "未明确安全措施", "责任边界不清"))
        ):
            hits.append(
                RuleHit(
                    rule_id="rule_data_security_boundary",
                    risk_type="data_security",
                    risk_level="high",
                    summary="数据安全条款责任边界不清。",
                    suggestion="建议明确处理目的、保存期限、安全措施、泄露通知时限和责任分担。",
                    requires_human_review=True,
                )
            )
        if any(term in text for term in ("付款", "支付")) and not any(term in text for term in ("验收", "交付", "发票")):
            hits.append(
                RuleHit(
                    rule_id="rule_payment_acceptance",
                    risk_type="payment_acceptance",
                    risk_level="medium",
                    summary="付款节点可能未绑定交付、验收或发票条件。",
                    suggestion="建议将付款条件与交付、验收或发票开具绑定。",
                )
            )
        if any(term in text for term in ("合同签署后 5 日", "签署后5日", "预付全部", "一次性支付全部", "提前支付全部")):
            hits.append(
                RuleHit(
                    rule_id="rule_abnormal_payment_cycle",
                    risk_type="payment_cycle",
                    risk_level="medium",
                    summary="付款周期可能过于前置，未与交付、验收或服务进度匹配。",
                    suggestion="建议拆分付款节点，并与交付成果、验收通过或服务周期绑定。",
                )
            )
        if any(term in text for term in ("知识产权归乙方所有", "成果归乙方", "交付成果的知识产权归乙方所有", "衍生成果", "改进成果")) and not any(term in text for term in ("甲方所有", "授权范围", "使用许可")):
            hits.append(
                RuleHit(
                    rule_id="rule_ip_ownership_ambiguous",
                    risk_type="ip_ownership",
                    risk_level="high",
                    summary="知识产权或交付成果归属可能不清，影响后续使用和商业化。",
                    suggestion="建议明确交付成果、背景知识产权、改进成果和使用许可边界。",
                    requires_human_review=True,
                )
            )
        if any(term in text for term in ("服务可用性", "SLA", "响应时间")) and not any(term in text for term in ("赔偿", "服务抵扣", "故障等级", "响应时限")):
            hits.append(
                RuleHit(
                    rule_id="rule_sla_remedy_missing",
                    risk_type="sla_remedy",
                    risk_level="medium",
                    summary="SLA 条款缺少故障等级、响应时限或违约补救。",
                    suggestion="建议补充服务可用性指标、故障响应时限和服务抵扣/赔偿机制。",
                )
            )
        if "所在地人民法院" in text or "异地" in text:
            hits.append(
                RuleHit(
                    rule_id="rule_jurisdiction",
                    risk_type="jurisdiction",
                    risk_level="medium",
                    summary="争议解决管辖地可能对己方不利。",
                    suggestion="建议评估是否改为公司所在地法院或双方认可的中立机构。",
                )
            )
        if "保密" in text and (
            not any(term in text for term in ("保密期限", "保密范围", "例外", "解除保密"))
            or any(term in text for term in ("未约定保密期限", "未明确保密范围", "永久保密"))
        ):
            hits.append(
                RuleHit(
                    rule_id="rule_confidentiality_scope",
                    risk_type="confidentiality",
                    risk_level="medium",
                    summary="保密条款可能缺少保密范围、期限或例外情形。",
                    suggestion="建议明确保密信息范围、保密期限、例外情形和违约责任边界。",
                )
            )
        if any(term in text for term in ("单方解除", "任意解除", "提前终止", "终止本合同")) and not any(term in text for term in ("提前通知", "书面通知", "整改期", "过渡期")):
            hits.append(
                RuleHit(
                    rule_id="rule_termination_notice",
                    risk_type="termination_notice",
                    risk_level="medium",
                    summary="解除或终止条款可能缺少提前通知、整改期或过渡安排。",
                    suggestion="建议补充提前书面通知期限、违约整改期和服务/交接过渡安排。",
                )
            )
        if "不可抗力" in text and not any(term in text for term in ("及时通知", "证明", "减损", "合理期限")):
            hits.append(
                RuleHit(
                    rule_id="rule_force_majeure_notice",
                    risk_type="force_majeure",
                    risk_level="low",
                    summary="不可抗力条款可能缺少通知、证明和减损义务。",
                    suggestion="建议补充不可抗力发生后的通知时限、证明材料和减损义务。",
                )
            )
        if any(term in text for term in ("押金", "保证金")) and not any(term in text for term in ("返还", "扣除", "退还期限", "无息返还")):
            hits.append(
                RuleHit(
                    rule_id="rule_deposit_return",
                    risk_type="deposit_return",
                    risk_level="medium",
                    summary="押金或保证金条款可能缺少返还条件、扣除范围或退还期限。",
                    suggestion="建议明确押金/保证金金额、扣除范围、返还期限和逾期返还责任。",
                )
            )
        if any(term in text for term in ("预付式", "储值", "充值", "预付款")) and not any(term in text for term in ("退款", "退费", "余额", "未消费", "解除后返还")):
            hits.append(
                RuleHit(
                    rule_id="rule_prepaid_refund",
                    risk_type="prepaid_refund",
                    risk_level="medium",
                    summary="预付式消费或预付款条款可能缺少退款、余额处理或解除后返还机制。",
                    suggestion="建议明确未消费余额退还、服务终止后的退款路径和手续费边界。",
                )
            )
        return hits


class PermissionGuard:
    """Answer-level guardrails for legal output."""

    def check(self, *, risk_level: str, evidence_count: int, suggestion: str, requires_human_review: bool) -> tuple[bool, str]:
        if evidence_count == 0:
            return False, "blocked: no source evidence"
        if any(term in suggestion for term in ("一定违法", "必然胜诉", "绝对合法")):
            return False, "blocked: absolute legal conclusion"
        if risk_level == "high" and requires_human_review:
            return True, "human_review_required"
        return True, ""
