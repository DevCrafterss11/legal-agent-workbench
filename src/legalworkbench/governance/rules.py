"""Risk rules and answer-level permission guard.

规则设计原则（真实合同 benchmark 驱动的重写）：

旧版规则是"话题关键词检测"——条款只要提到付款/保密/不可抗力就触发。
这在全是风险条款的合成集上看不出问题，但在真实示范文本（大多均衡）上
造成误报洪水（real benchmark 实测 rule_only precision 0.14）。

新版规则只做**不利模式正向匹配**：条款必须出现真正的风险语言
（"概不退还""不设上限""由乙方视情况确定"……）才触发。
拿不准的语义风险留给检索证据 + LLM 判断层，规则层宁缺毋滥。
"""

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


def _any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def match_adverse(risk_type: str, text: str) -> bool:
    """判断条款文本是否出现该风险类型的不利模式语言。

    这是规则引擎与检索证据门控共用的判定核心：证据检索给出候选
    risk_type，但只有条款本身出现不利语言时才升级为风险预测。
    """

    if risk_type == "unlimited_liability":
        direct = (
            "不设赔偿责任上限",
            "不设上限",
            "不设任何上限",
            "赔偿总额不设",
            "无限责任",
            "无责任上限",
            "不受合同总额约束",
            "不受本合同价款总额约束",
            "开放式补偿",
            "无最高金额限制",
            "不设最高金额限制",
        )
        return _any(text, direct) or (
            "全部损失" in text and _any(text, ("间接损失", "预期利润", "预期利益", "商誉"))
        )
    if risk_type == "auto_renewal":
        if _any(text, ("视为认可延展", "依原有内容继续生效", "按原条件顺延", "顺延次数不限", "续约次数不限", "延展次数不受限制")):
            return True
        if not _any(text, ("自动续约", "自动续期", "自动顺延")):
            return False
        return not _any(text, ("提前", "通知", "取消")) or _any(
            text, ("未约定提前", "未约定通知", "缺少明确提前通知", "未明确取消", "未约定取消")
        )
    if risk_type == "data_security":
        subject = ("客户数据", "个人信息", "数据泄露", "用户资料", "业务数据", "业务记录", "行为信息", "行为记录", "业务日志", "终端用户资料")
        adverse = ("未约定", "未就", "未规定", "自行决定", "用于自身", "用于内部", "无需另行", "未明确", "责任边界不清")
        return _any(text, subject) and _any(text, adverse)
    if risk_type == "payment_acceptance":
        return _any(
            text,
            (
                "不以验收",
                "不以交付",
                "无需验收",
                "不以成果",
                "未设置成果确认",
                "未设置质量确认",
                "成果确认或质量确认要求",
                "收到请款",
                "请款后支付",
                "以乙方申请为准",
                "申请为准",
                "尚待确认为由",
            ),
        )
    if risk_type == "payment_cycle":
        return _any(
            text,
            (
                "签署后 5 日",
                "签署后5日",
                "预付全部",
                "一次性支付全部",
                "一次性预付",
                "提前支付全部",
                "一笔划付",
                "不随交付进度",
                "不影响已收款项",
                "进度不影响付款",
                "付款安排不随",
            ),
        )
    if risk_type == "ip_ownership":
        adverse = (
            "归乙方所有",
            "知识产权归乙方",
            "成果归乙方",
            "著作权属于咨询人",
            "著作权归咨询人",
            "拥有著作权",
            "由服务提供方保留",
            "默认由服务商保留",
            "服务商保留",
            "仅可在本项目范围内使用",
            "仅可内部查看",
            "以单个项目为限",
            "本项目内部使用",
        )
        favorable = ("归甲方所有", "甲方所有", "归委托人所有", "甲方拥有", "委托人拥有", "买受人拥有")
        return _any(text, adverse) and not _any(text, favorable)
    if risk_type == "sla_remedy":
        direct = (
            "按乙方内部标准",
            "按乙方标准执行",
            "由乙方视运营情况决定",
            "视运营情况决定",
            "未约定故障分级",
            "未设置故障分级",
            "不得据此主张",
            "后台记录为准",
        )
        if _any(text, direct):
            return True
        topic = ("服务可用性", "SLA", "sla")
        return _any(text, topic) and _any(text, ("未约定", "未设置", "内部标准"))
    if risk_type == "jurisdiction":
        context = ("管辖", "法院", "仲裁", "争议", "诉讼", "有权机构", "处理途径", "争议解决机构")
        adverse = (
            "乙方所在地",
            "乙方住所地",
            "乙方注册地",
            "服务方注册地",
            "服务商注册地",
            "供应商注册地",
            "不得另行选择",
            "不得向其他法院",
            "不得向其他机构",
        )
        return _any(text, context) and _any(text, adverse)
    if risk_type == "confidentiality":
        return _any(
            text,
            (
                "永久保密",
                "永久守密",
                "无限期保密",
                "无期限保密",
                "不设例外",
                "无豁免",
                "不受本条约束",
                "未约定保密期限",
                "未明确保密范围",
                "不承担对等义务",
                "单方保密",
            ),
        )
    if risk_type == "termination_notice":
        return _any(
            text,
            (
                "无需提前通知",
                "无需通知",
                "无须通知",
                "不经通知",
                "无需事先通知",
                "即时终止",
                "自作出时生效",
                "不得要求过渡",
                "无需提供交接",
                "不提供过渡",
                "基于自身经营需要单方解除",
                "随时单方解除",
            ),
        )
    if risk_type == "force_majeure":
        if not _any(text, ("不可抗力", "意外事件", "非乙方所能控制")):
            return False
        if _any(text, ("双方均不", "互不承担", "各自承担")):
            return False  # 双向对等免责是常规安排
        return _any(
            text,
            (
                "无需通知",
                "无须履行任何告知",
                "未约定告知",
                "未约定通知",
                "凭证提交和损失控制",
                "免除全部责任",
                "自动中止",
                "照常履行",
                "政府行为",
                "不承担赔偿责任",
            ),
        )
    if risk_type == "deposit_return":
        subject = ("押金", "保证金", "担保款项", "履约保证金")
        adverse = ("视情况确定", "酌定", "内部核算为准", "扣除其认定", "不予退还", "概不退还", "不予返还", "未约定")
        return _any(text, subject) and _any(text, adverse)
    if risk_type == "prepaid_refund":
        subject = ("预付", "预收", "充值", "储值", "余额", "预付式")
        adverse = ("概不退还", "不予退款", "不退还", "不予退还", "清零", "不适用任何返还", "不得转让", "未约定", "无法索回")
        return _any(text, subject) and _any(text, adverse)
    return False


_RULE_META: dict[str, tuple[str, str, str, str, bool]] = {
    # risk_type: (rule_id, risk_level, summary, suggestion, requires_human_review)
    "unlimited_liability": (
        "rule_unlimited_liability",
        "high",
        "条款要求承担无上限或含间接损失的扩张赔偿责任。",
        "建议设置赔偿责任上限，并排除间接损失、预期利润损失等扩张责任。",
        True,
    ),
    "auto_renewal": (
        "rule_auto_renewal_notice",
        "medium",
        "自动续约/自动延展条款缺少明确提前通知或退出路径。",
        "建议增加提前 30 天书面通知和明确取消方式。",
        False,
    ),
    "data_security": (
        "rule_data_security_boundary",
        "high",
        "数据或个人信息处理条款出现不利安排或责任边界不清。",
        "建议明确处理目的、保存期限、安全措施、泄露通知时限和责任分担。",
        True,
    ),
    "payment_acceptance": (
        "rule_payment_acceptance",
        "medium",
        "付款义务未与交付、验收或成果确认绑定。",
        "建议将付款条件与交付、验收或发票开具绑定。",
        False,
    ),
    "payment_cycle": (
        "rule_abnormal_payment_cycle",
        "medium",
        "付款周期过于前置，未与交付、验收或服务进度匹配。",
        "建议拆分付款节点，并与交付成果、验收通过或服务周期绑定。",
        False,
    ),
    "ip_ownership": (
        "rule_ip_ownership_ambiguous",
        "high",
        "知识产权或交付成果归属偏向乙方，影响后续使用和商业化。",
        "建议明确交付成果、背景知识产权、改进成果和使用许可边界。",
        True,
    ),
    "sla_remedy": (
        "rule_sla_remedy_missing",
        "medium",
        "服务水平条款缺少故障分级、响应时限或补救机制。",
        "建议补充服务可用性指标、故障响应时限和服务抵扣/赔偿机制。",
        False,
    ),
    "jurisdiction": (
        "rule_jurisdiction",
        "medium",
        "争议解决管辖单方固定于乙方一侧，可能对己方不利。",
        "建议评估是否改为公司所在地法院或双方认可的中立机构。",
        False,
    ),
    "confidentiality": (
        "rule_confidentiality_scope",
        "medium",
        "保密条款出现无限期、无例外或单方义务等不利安排。",
        "建议明确保密信息范围、保密期限、例外情形和违约责任边界。",
        False,
    ),
    "termination_notice": (
        "rule_termination_notice",
        "medium",
        "解除或终止条款允许单方即时解除，缺少通知期或过渡安排。",
        "建议补充提前书面通知期限、违约整改期和服务/交接过渡安排。",
        False,
    ),
    "force_majeure": (
        "rule_force_majeure_notice",
        "low",
        "不可抗力条款免责单方化或缺少通知、证明和减损义务。",
        "建议补充不可抗力发生后的通知时限、证明材料和对等的减损义务。",
        False,
    ),
    "deposit_return": (
        "rule_deposit_return",
        "medium",
        "押金或保证金的返还条件、扣除范围由单方决定或缺失。",
        "建议明确押金/保证金金额、扣除范围、返还期限和逾期返还责任。",
        False,
    ),
    "prepaid_refund": (
        "rule_prepaid_refund",
        "medium",
        "预付费条款出现概不退还、余额清零等不利安排或退款机制缺失。",
        "建议明确未消费余额退还、服务终止后的退款路径和手续费边界。",
        False,
    ),
}


class RiskRuleEngine:
    """Deterministic adverse-pattern risk rules that complement RAG evidence."""

    def evaluate(self, text: str) -> list[RuleHit]:
        hits: list[RuleHit] = []
        for risk_type, (rule_id, level, summary, suggestion, review) in _RULE_META.items():
            if match_adverse(risk_type, text):
                hits.append(
                    RuleHit(
                        rule_id=rule_id,
                        risk_type=risk_type,
                        risk_level=level,
                        summary=summary,
                        suggestion=suggestion,
                        requires_human_review=review,
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
