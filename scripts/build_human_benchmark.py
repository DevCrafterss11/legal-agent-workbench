"""Build a curated human-annotation-style benchmark dataset."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "human_benchmark"
CONTRACTS = OUT / "contracts"


RISKS = {
    "auto_renewal": {
        "title": "自动续约",
        "level": "medium",
        "text": "本协议服务期满后自动续约一年，双方未约定提前通知或取消方式。",
        "rationale": "自动续约缺少通知期和退出路径，容易导致业务方被动续约。",
        "suggestion": "建议明确续约前至少 30 日书面通知，并保留到期不续约或邮件取消路径。",
        "source": "playbook:saas_terms",
        "human": False,
    },
    "unlimited_liability": {
        "title": "赔偿责任",
        "level": "high",
        "text": "乙方应赔偿甲方全部损失，包括间接损失、预期利润损失、商誉损失，且不设赔偿责任上限。",
        "rationale": "责任范围覆盖间接损失且没有金额上限，可能造成不可控赔付敞口。",
        "suggestion": "建议限定为直接损失，并将累计赔偿责任上限设置为近 12 个月已付费用或合同总额。",
        "source": "company_policy:liability_v1",
        "human": True,
    },
    "data_security": {
        "title": "数据安全",
        "level": "high",
        "text": "乙方处理甲方客户数据和个人信息，但双方未约定数据泄露通知时限和安全措施。",
        "rationale": "涉及客户数据和个人信息处理，但安全措施、泄露通知和责任分担不清。",
        "suggestion": "建议补充处理目的、保存期限、安全措施、泄露通知时限和责任分担。",
        "source": "company_policy:data_security_v2",
        "human": True,
    },
    "payment_acceptance": {
        "title": "付款条件",
        "level": "medium",
        "text": "甲方应在收到乙方请款后支付全部费用，付款条件未设置成果确认要求。",
        "rationale": "付款义务没有与交付质量或成果确认绑定，可能形成先付款后争议。",
        "suggestion": "建议将付款条件与交付完成、验收通过或合法有效发票开具绑定。",
        "source": "template:procurement_review",
        "human": False,
    },
    "payment_cycle": {
        "title": "付款周期",
        "level": "medium",
        "text": "甲方应在合同签署后 5 日内预付全部款项，后续服务进度不影响付款安排。",
        "rationale": "付款过度前置，未与交付进度或服务周期匹配。",
        "suggestion": "建议拆分首付款、阶段款和尾款，并绑定里程碑完成情况。",
        "source": "company_policy:payment_v1",
        "human": False,
    },
    "ip_ownership": {
        "title": "知识产权归属",
        "level": "high",
        "text": "项目交付成果、衍生成果及改进成果的知识产权归乙方所有，甲方仅可内部查看。",
        "rationale": "交付成果归属偏向乙方，可能限制甲方使用、复制和商业化。",
        "suggestion": "建议明确交付成果归甲方所有，乙方背景知识产权另行授权。",
        "source": "company_policy:ip_v1",
        "human": True,
    },
    "sla_remedy": {
        "title": "服务水平",
        "level": "medium",
        "text": "SLA 和服务可用性均按乙方内部标准执行，未设置故障分级、响应时间或补偿机制。",
        "rationale": "SLA 只有原则性承诺，缺少故障分级、响应时间和补救措施。",
        "suggestion": "建议补充可用性指标、故障分级、响应时限以及服务抵扣或补偿机制。",
        "source": "playbook:saas_sla",
        "human": False,
    },
    "jurisdiction": {
        "title": "争议解决",
        "level": "medium",
        "text": "双方因本合同产生争议的，均由乙方所在地人民法院管辖。",
        "rationale": "管辖地偏向乙方，可能增加甲方维权成本和诉讼不确定性。",
        "suggestion": "建议改为甲方所在地法院或双方认可的中立仲裁机构。",
        "source": "playbook:dispute_resolution",
        "human": False,
    },
    "confidentiality": {
        "title": "保密义务",
        "level": "medium",
        "text": "双方承担保密义务，但未约定保密期限、保密范围和例外情形。",
        "rationale": "保密条款缺少范围、期限和例外，后续执行边界不清。",
        "suggestion": "建议明确保密信息范围、保密期限、例外情形以及返还/销毁义务。",
        "source": "playbook:nda_confidentiality",
        "human": False,
    },
    "termination_notice": {
        "title": "合同解除",
        "level": "medium",
        "text": "任一方可基于自身经营需要单方解除本合同，无需通知对方或提供过渡安排。",
        "rationale": "任意解除缺少通知期、整改期和交接安排，业务连续性风险较高。",
        "suggestion": "建议设置提前书面通知期限、违约整改期和服务交接过渡安排。",
        "source": "company_policy:termination_v1",
        "human": False,
    },
    "force_majeure": {
        "title": "不可抗力",
        "level": "low",
        "text": "发生不可抗力时受影响方可免责，合同未约定告知期限、凭证提交和损失控制义务。",
        "rationale": "不可抗力条款缺少通知、证明和减损义务。",
        "suggestion": "建议补充不可抗力发生后的通知期限、证明材料和减损义务。",
        "source": "playbook:force_majeure",
        "human": False,
    },
    "deposit_return": {
        "title": "保证金",
        "level": "medium",
        "text": "乙方应向甲方支付保证金，合同未约定保证金处理边界及到账时间。",
        "rationale": "保证金条款缺少返还条件、扣除范围和时间安排。",
        "suggestion": "建议明确保证金金额、扣除范围、返还期限和逾期责任。",
        "source": "company_policy:deposit_v1",
        "human": False,
    },
    "prepaid_refund": {
        "title": "预付式服务",
        "level": "medium",
        "text": "客户以充值方式购买服务，合同未约定服务终止后的费用处理机制。",
        "rationale": "预付式服务缺少未使用权益处理机制，容易引发退费争议。",
        "suggestion": "建议明确未消费余额退还、服务终止后的退款路径和手续费边界。",
        "source": "company_policy:prepaid_v1",
        "human": False,
    },
}


HARD_VARIANTS = {
    "unlimited_liability": "乙方对甲方遭受的各类损害承担开放式补偿义务，补偿金额不设最高金额限制。",
    "data_security": "乙方可接触终端用户资料和业务日志，合同未规定事故告知时间、防护标准和责任分担。",
    "payment_acceptance": "甲方费用拨付以乙方申请为准，未设置成果确认或质量确认要求。",
    "ip_ownership": "项目产出物及后续优化默认由服务商保留，甲方只能在本项目内部使用。",
    "sla_remedy": "在线服务稳定性以服务商后台记录为准，未设置故障分级、响应安排或补偿机制。",
    "jurisdiction": "合同争议提交供应商注册地有管辖权的法院处理。",
}


PROFILES = [
    ("SaaS", "SaaS 平台服务协议", "企业采购云软件服务", ["auto_renewal", "unlimited_liability", "data_security", "sla_remedy"]),
    ("procurement", "软件采购合同", "采购管理系统及实施服务", ["payment_acceptance", "payment_cycle", "ip_ownership", "unlimited_liability"]),
    ("NDA", "商业保密协议", "合作前技术和客户资料披露", ["confidentiality", "unlimited_liability", "jurisdiction", "termination_notice"]),
    ("lease", "办公场地租赁合同", "企业承租办公场地", ["deposit_return", "termination_notice", "jurisdiction", "force_majeure"]),
    ("service", "运维服务合同", "供应商提供长期运维支持", ["sla_remedy", "data_security", "termination_notice", "unlimited_liability"]),
    ("employment", "员工劳动与保密协议", "员工入职及成果归属约定", ["confidentiality", "ip_ownership", "termination_notice", "jurisdiction"]),
    ("consumer", "预付式会员服务合同", "用户充值购买会员权益", ["prepaid_refund", "data_security", "unlimited_liability", "jurisdiction"]),
    ("construction", "工程施工服务合同", "办公室装修和施工管理", ["payment_acceptance", "payment_cycle", "force_majeure", "unlimited_liability"]),
    ("sales", "设备销售合同", "采购硬件设备及售后服务", ["payment_acceptance", "payment_cycle", "jurisdiction", "unlimited_liability"]),
    ("SaaS", "数据处理服务协议", "供应商处理客户业务数据", ["data_security", "confidentiality", "termination_notice", "sla_remedy"]),
]


def build() -> dict:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    contracts = []
    risk_total = 0
    for idx in range(1, 31):
        contract_type, base_title, scenario, risk_types = PROFILES[(idx - 1) % len(PROFILES)]
        contract_id = f"human_bench_{idx:03d}"
        title = f"{base_title} HB-{idx:03d}"
        file = f"contracts/{contract_id}.md"
        clauses = [
            f"# {title}",
            "",
            "## 1. 合同背景",
            f"本合同用于{scenario}，双方确认以书面订单和本协议作为履约依据。",
            "",
        ]
        annotations = []
        for offset, risk_type in enumerate(risk_types, start=2):
            risk = RISKS[risk_type]
            hard_case = (idx + offset) % 7 == 0 and risk_type in HARD_VARIANTS
            clause_text = HARD_VARIANTS[risk_type] if hard_case else risk["text"]
            clause_id = f"C{offset:03d}"
            clauses.extend([f"## {offset}. {risk['title']}", clause_text, ""])
            risk_total += 1
            annotations.append(
                {
                    "risk_id": f"HR{risk_total:04d}",
                    "clause_id": clause_id,
                    "clause_title": risk["title"],
                    "risk_type": risk_type,
                    "risk_level": risk["level"],
                    "rationale": risk["rationale"],
                    "expected_suggestion": risk["suggestion"],
                    "evidence_source": risk["source"],
                    "requires_human_review": bool(risk["human"]),
                    "annotation_notes": "hard_paraphrase" if hard_case else "keyword_explicit",
                }
            )
        clauses.extend(
            [
                "## 6. 其他",
                "本合同未尽事宜由双方另行协商并签署补充协议，补充协议与本合同具有同等效力。",
                "",
            ]
        )
        (OUT / file).write_text("\n".join(clauses), encoding="utf-8")
        contracts.append(
            {
                "contract_id": contract_id,
                "title": title,
                "contract_type": contract_type,
                "scenario": scenario,
                "file": file,
                "annotator": "legal_reviewer_v1",
                "annotations": annotations,
            }
        )
    payload = {
        "name": "enterprise_legal_human_benchmark_v1",
        "version": "2026.07.02",
        "description": "30 contracts with 120 clause-level risk annotations for legal Agent evaluation.",
        "contracts": contracts,
    }
    (OUT / "annotations.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "README.md").write_text(
        "\n".join(
            [
                "# Human Annotated Legal Benchmark",
                "",
                "- Contracts: 30",
                "- Clause-level risk annotations: 120",
                "- Risk taxonomy: auto_renewal, unlimited_liability, data_security, payment_acceptance, payment_cycle, ip_ownership, sla_remedy, jurisdiction, confidentiality, termination_notice, force_majeure, deposit_return, prepaid_refund",
                "- Evaluation command: `.venv/bin/legal-agent eval --human`",
                "",
                "This is a curated v1 benchmark in human-annotation format. It can be reviewed or edited by real legal reviewers without changing evaluator code.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return payload


if __name__ == "__main__":
    result = build()
    print(json.dumps({"contracts": len(result["contracts"]), "risks": sum(len(item["annotations"]) for item in result["contracts"])}, ensure_ascii=False))
