"""Sample knowledge, skills, memory, benchmark, and contract text."""

from __future__ import annotations

from legalworkbench.models import BenchmarkCase, KnowledgeEntry, LegalMemory, LegalSkill

SAMPLE_SETTINGS = {
    "rag": {
        "vector_backend": "local",
        "milvus_uri": "http://127.0.0.1:19530",
        "collection": "legal_clause_knowledge",
        "embedding_provider": "hashing",
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "embedding_device": "cpu",
        "embedding_normalize": True,
        "embedding_fallback": True,
        "lexical_top_k": 32,
        "vector_top_k": 32,
        "final_top_k": 10,
        "connect_timeout": 1.0,
    },
    "mcp_servers": {
        "feishu_legal_workspace": {
            "type": "mock",
            "description": "Feishu Docs, approval tasks, and legal audit logs.",
        },
        "notion_legal_playbook": {
            "type": "mock",
            "description": "Notion legal playbook and review record database.",
        },
    }
}

SAMPLE_KNOWLEDGE = [
    KnowledgeEntry(
        id="risk_unlimited_liability",
        title="无限责任赔偿风险",
        body="出现无限责任、全部损失、间接损失、预期利润损失等表述时，应建议设置赔偿责任上限。",
        contract_type="SaaS",
        clause_type="liability",
        risk_type="unlimited_liability",
        risk_level="high",
        source="company_policy:liability_v1",
        tags=["赔偿", "责任上限", "高风险"],
    ),
    KnowledgeEntry(
        id="risk_auto_renewal",
        title="自动续约条款审查",
        body="自动续约条款应明确提前通知期限、通知方式和取消路径。",
        contract_type="SaaS",
        clause_type="term",
        risk_type="auto_renewal",
        risk_level="medium",
        source="playbook:saas_terms",
        tags=["自动续约", "通知期"],
    ),
    KnowledgeEntry(
        id="risk_data_security",
        title="数据安全责任边界",
        body="涉及客户数据、个人信息、商业秘密时，应明确处理目的、保存期限、安全措施、泄露通知时限和责任分担。",
        contract_type="SaaS",
        clause_type="data_security",
        risk_type="data_security",
        risk_level="high",
        source="company_policy:data_security_v2",
        tags=["数据安全", "个人信息"],
    ),
    KnowledgeEntry(
        id="risk_payment_acceptance",
        title="付款与验收节点",
        body="采购合同应将付款节点与交付、验收、发票开具绑定，避免付款前置风险。",
        contract_type="procurement",
        clause_type="payment",
        risk_type="payment_acceptance",
        risk_level="medium",
        source="template:procurement_review",
        tags=["付款", "验收"],
    ),
    KnowledgeEntry(
        id="risk_payment_cycle",
        title="付款周期前置风险",
        body="合同签署后短期内一次性支付全部款项、预付全部费用或付款节点不随交付进度释放，通常需要拆分付款节点并绑定验收。",
        contract_type="procurement",
        clause_type="payment",
        risk_type="payment_cycle",
        risk_level="medium",
        source="company_policy:payment_v1",
        tags=["付款周期", "预付款", "验收"],
    ),
    KnowledgeEntry(
        id="risk_ip_ownership",
        title="知识产权归属风险",
        body="涉及交付成果、背景知识产权、衍生成果、改进成果时，应明确权属、使用许可、授权范围和商业化限制。",
        contract_type="procurement",
        clause_type="ip",
        risk_type="ip_ownership",
        risk_level="high",
        source="company_policy:ip_v1",
        tags=["知识产权", "交付成果", "授权范围"],
    ),
    KnowledgeEntry(
        id="risk_sla_remedy",
        title="SLA 补救机制缺失",
        body="SaaS 服务协议应明确服务可用性、故障等级、响应时限、服务抵扣或赔偿机制，避免 SLA 只有指标没有补救。",
        contract_type="SaaS",
        clause_type="sla",
        risk_type="sla_remedy",
        risk_level="medium",
        source="playbook:saas_sla",
        tags=["SLA", "服务可用性", "补救"],
    ),
    KnowledgeEntry(
        id="risk_jurisdiction",
        title="管辖地不利风险",
        body="争议解决条款应避免选择对公司明显不利的异地法院或仲裁机构。",
        contract_type="general",
        clause_type="dispute",
        risk_type="jurisdiction",
        risk_level="medium",
        source="playbook:dispute_resolution",
        tags=["管辖", "争议解决"],
    ),
    KnowledgeEntry(
        id="risk_confidentiality_scope",
        title="保密范围与期限风险",
        body="保密条款应明确保密信息范围、保密期限、例外情形、返还/销毁义务和违约责任边界，避免永久保密或范围过宽。",
        contract_type="NDA",
        clause_type="confidentiality",
        risk_type="confidentiality",
        risk_level="medium",
        source="playbook:nda_confidentiality",
        tags=["保密", "期限", "例外"],
    ),
    KnowledgeEntry(
        id="risk_termination_notice",
        title="解除与终止通知风险",
        body="合同解除或提前终止应明确提前书面通知期限、违约整改期、费用结算、资料返还和服务交接安排。",
        contract_type="general",
        clause_type="termination",
        risk_type="termination_notice",
        risk_level="medium",
        source="company_policy:termination_v1",
        tags=["解除", "终止", "通知"],
    ),
    KnowledgeEntry(
        id="risk_force_majeure_notice",
        title="不可抗力通知与减损风险",
        body="不可抗力条款应约定及时通知、证明材料、持续影响期间的减损义务和延期/解除条件。",
        contract_type="general",
        clause_type="force_majeure",
        risk_type="force_majeure",
        risk_level="low",
        source="playbook:force_majeure",
        tags=["不可抗力", "通知", "减损"],
    ),
    KnowledgeEntry(
        id="risk_deposit_return",
        title="押金和保证金返还风险",
        body="租赁、服务等合同涉及押金或保证金时，应明确金额、扣除范围、返还期限、逾期返还责任和争议处理方式。",
        contract_type="lease",
        clause_type="deposit",
        risk_type="deposit_return",
        risk_level="medium",
        source="company_policy:deposit_v1",
        tags=["押金", "保证金", "返还"],
    ),
    KnowledgeEntry(
        id="risk_prepaid_refund",
        title="预付式消费退款风险",
        body="预付式消费、充值、储值或预付款条款应明确未消费余额退还、服务终止后的退款路径、手续费边界和消费者解除权。",
        contract_type="consumer",
        clause_type="prepaid",
        risk_type="prepaid_refund",
        risk_level="medium",
        source="company_policy:prepaid_v1",
        tags=["预付式", "充值", "退款"],
    ),
]

SAMPLE_SKILLS = [
    LegalSkill(
        name="saas_agreement_review",
        contract_type="SaaS",
        description="Review SaaS agreements with focus on liability, uptime, data security, and renewal.",
        focus_clause_types=["liability", "data_security", "term", "sla"],
        risk_rules=["unlimited_liability", "data_security", "auto_renewal", "sla_remedy"],
        report_style="risk-first",
    ),
    LegalSkill(
        name="procurement_review",
        contract_type="procurement",
        description="Review procurement contracts with focus on payment, acceptance, delivery, and breach.",
        focus_clause_types=["payment", "acceptance", "liability", "ip"],
        risk_rules=["payment_acceptance", "payment_cycle", "unlimited_liability", "ip_ownership"],
        report_style="business-readable",
    ),
    LegalSkill(
        name="nda_review",
        contract_type="NDA",
        description="Review NDA clauses with focus on confidentiality scope, term, exclusions, and liability.",
        focus_clause_types=["confidentiality", "term", "liability", "termination"],
        risk_rules=["confidentiality", "unlimited_liability", "termination_notice"],
        report_style="concise",
    ),
    LegalSkill(
        name="sales_contract_review",
        contract_type="sales",
        description="Review sales contracts with focus on payment, delivery, acceptance, warranty, liability, and dispute resolution.",
        focus_clause_types=["payment", "delivery", "acceptance", "warranty", "liability", "dispute"],
        risk_rules=["payment_acceptance", "payment_cycle", "unlimited_liability", "jurisdiction"],
        report_style="business-readable",
    ),
    LegalSkill(
        name="lease_contract_review",
        contract_type="lease",
        description="Review lease contracts with focus on rent, deposit return, repair obligations, termination, and jurisdiction.",
        focus_clause_types=["rent", "deposit", "repair", "termination", "dispute"],
        risk_rules=["deposit_return", "termination_notice", "payment_acceptance", "jurisdiction"],
        report_style="risk-first",
    ),
    LegalSkill(
        name="service_contract_review",
        contract_type="service",
        description="Review service contracts with focus on service scope, SLA, acceptance, data, liability, and termination.",
        focus_clause_types=["scope", "sla", "acceptance", "data_security", "liability", "termination"],
        risk_rules=["sla_remedy", "data_security", "unlimited_liability", "termination_notice"],
        report_style="risk-first",
    ),
    LegalSkill(
        name="construction_contract_review",
        contract_type="construction",
        description="Review construction contracts with focus on scope, change orders, payment, acceptance, delay, and liability.",
        focus_clause_types=["scope", "change", "payment", "acceptance", "delay", "liability"],
        risk_rules=["payment_acceptance", "payment_cycle", "unlimited_liability", "force_majeure"],
        report_style="business-readable",
    ),
    LegalSkill(
        name="consumer_contract_review",
        contract_type="consumer",
        description="Review consumer and prepaid contracts with focus on refund, service delivery, data, liability, and dispute handling.",
        focus_clause_types=["prepaid", "refund", "service", "data_security", "liability", "dispute"],
        risk_rules=["prepaid_refund", "data_security", "unlimited_liability", "jurisdiction"],
        report_style="concise",
    ),
    LegalSkill(
        name="employment_contract_review",
        contract_type="employment",
        description="Review employment contracts with focus on term, compensation, confidentiality, IP, termination, and dispute resolution.",
        focus_clause_types=["term", "compensation", "confidentiality", "ip", "termination", "dispute"],
        risk_rules=["confidentiality", "ip_ownership", "termination_notice", "jurisdiction"],
        report_style="concise",
    ),
]

SAMPLE_MEMORY = [
    LegalMemory(
        memory_id="mem_saas_liability_cap",
        type="episodic",
        contract_type="SaaS",
        clause_type="liability",
        risk_type="unlimited_liability",
        risk_level="high",
        summary="历史 SaaS 协议中，无限责任条款通常改为合同总金额或近 12 个月服务费上限。",
        approved_advice="建议改为：乙方仅对因其违约造成的直接损失承担赔偿责任，累计赔偿总额不超过事故发生前十二个月甲方已实际支付的服务费用；间接损失、预期利润损失、商誉损失及惩罚性赔偿不纳入赔偿范围。",
        source_review_run_id="sample_run_001",
        approved_by_human=True,
        confidence=0.92,
        tags=["SaaS", "责任上限"],
    ),
    LegalMemory(
        memory_id="mem_auto_renewal_notice",
        type="procedural",
        contract_type="SaaS",
        clause_type="term",
        risk_type="auto_renewal",
        risk_level="medium",
        summary="自动续约条款应检查通知期、通知方式和取消路径。",
        approved_advice="建议增加至少 30 天书面通知期，并明确可通过邮件取消续约。",
        source_review_run_id="sample_run_002",
        approved_by_human=True,
        confidence=0.86,
        tags=["自动续约"],
    ),
]

SAMPLE_CONTRACT = """# SaaS 服务协议样例

## 1. 服务内容
乙方向甲方提供在线软件服务，服务期限为一年。

## 2. 自动续约
服务期满后，本协议自动续约一年，除非双方另有书面约定。

## 3. 赔偿责任
乙方应赔偿甲方因此遭受的全部损失，包括直接损失、间接损失、预期利润损失，且不设赔偿责任上限。

## 4. 数据安全
乙方可处理甲方客户数据，但双方未约定数据泄露通知时限和安全措施标准。

## 5. 争议解决
双方同意由乙方所在地人民法院管辖。
"""

SAMPLE_BENCHMARK = [
    BenchmarkCase(
        id="bench_saas_001",
        contract_type="SaaS",
        contract_text=SAMPLE_CONTRACT,
        expected_risk_types=["auto_renewal", "unlimited_liability", "data_security"],
    ),
    BenchmarkCase(
        id="bench_procurement_001",
        contract_type="procurement",
        contract_text="## 付款\n甲方应在合同签署后 5 日内支付全部款项，付款不以验收或交付作为前提。\n## 责任\n乙方承担全部间接损失且无责任上限。",
        expected_risk_types=["payment_acceptance", "unlimited_liability"],
    ),
]
