"""Generate deterministic benchmark cases for local evaluation."""

from __future__ import annotations

from legalworkbench.models import BenchmarkCase


CASE_TEMPLATES: tuple[tuple[str, str, list[str]], ...] = (
    (
        "SaaS",
        "## 自动续约\n服务期满自动续约一年，未约定提前通知或取消方式。\n## 责任\n乙方赔偿全部损失且不设赔偿责任上限。\n## 数据\n乙方处理客户数据但未约定安全措施。",
        ["auto_renewal", "unlimited_liability", "data_security"],
    ),
    (
        "procurement",
        "## 付款\n甲方在合同签署后 5 日内预付全部款项，不以验收为前提。\n## 成果\n交付成果的知识产权归乙方所有。",
        ["payment_acceptance", "payment_cycle", "ip_ownership"],
    ),
    (
        "NDA",
        "## 保密责任\n接收方承担全部间接损失且无责任上限。\n## 管辖\n双方同意由乙方所在地人民法院管辖。",
        ["unlimited_liability", "jurisdiction"],
    ),
    (
        "SaaS",
        "## SLA\n服务可用性按乙方标准执行，未约定故障等级、响应时间或服务抵扣。\n## 数据\n出现数据泄露时双方未约定通知时限。",
        ["sla_remedy", "data_security"],
    ),
    # ---- hard 样本：风险用隐式措辞表达，刻意避开规则引擎的关键词触发 ----
    # 用于测量语义检索相对关键词规则的真实增益，防止评测饱和
    (
        "SaaS",
        "## 补偿安排\n乙方须就服务缺陷给甲方造成的直接与间接经济影响承担全额补偿，且补偿金额不受合同总额约束。",
        ["unlimited_liability"],
    ),
    (
        "SaaS",
        "## 期限延续\n服务期届满时，除非任一方在期满前书面提出异议，本协议按原条件顺延一年，顺延次数不限。",
        ["auto_renewal"],
    ),
    (
        "procurement",
        "## 信息使用\n乙方可将履约过程中获取的甲方业务记录与最终用户行为信息用于内部分析和产品改进，双方未就使用范围与删除义务另行约定。",
        ["data_security"],
    ),
    (
        "NDA",
        "## 争议处理\n因本协议引起的任何分歧，均提交乙方注册地的争议解决机构处理，甲方不得向其他机构提出。",
        ["jurisdiction"],
    ),
    (
        "SaaS",
        "## 服务水平\n系统中断或故障的处理时点与补偿方式由乙方视运营情况决定，甲方不得另行主张。",
        ["sla_remedy"],
    ),
    # 未解 hard 样本：措辞完全脱离"续约/自动/顺延"词面，当前规则与检索均漏检。
    # 保留在 benchmark 中作为已知失败案例，防止评测饱和并标记改进方向
    (
        "SaaS",
        "## 存续安排\n本约定于期间届满后依原有内容继续生效，任一方未作表示即视为认可延展。",
        ["auto_renewal"],
    ),
    # 多风险混排样本：保密条款在知识库中覆盖稀疏（检索易漏），但规则引擎可兜底；
    # 隐式补偿条款则相反（规则漏、检索中）——用于验证规则与检索的互补性
    (
        "procurement",
        "## 付款\n甲方按月支付服务费。\n## 成果\n交付成果的知识产权归乙方所有，含衍生成果。\n"
        "## 保密\n双方对合作内容承担保密义务，未约定保密期限。\n"
        "## 补偿\n乙方承担全额补偿且金额不受合同总额约束。\n## 不可抗力\n发生不可抗力时双方可暂停履行。",
        ["payment_acceptance", "ip_ownership", "confidentiality", "unlimited_liability", "force_majeure"],
    ),
)


def build_scaled_benchmark(*, contract_cases: int = 80, risk_clauses: int = 300) -> list[BenchmarkCase]:
    """Build a scaled deterministic benchmark without external data."""

    cases: list[BenchmarkCase] = []
    risk_count = 0
    idx = 1
    while len(cases) < contract_cases or risk_count < risk_clauses:
        contract_type, text, risks = CASE_TEMPLATES[(idx - 1) % len(CASE_TEMPLATES)]
        variant = f"\n## 版本\n该样本用于 benchmark variant {idx}。"
        case = BenchmarkCase(
            id=f"bench_scaled_{idx:03d}",
            contract_type=contract_type,
            contract_text=text + variant,
            expected_risk_types=risks,
        )
        cases.append(case)
        risk_count += len(risks)
        idx += 1
    return cases
