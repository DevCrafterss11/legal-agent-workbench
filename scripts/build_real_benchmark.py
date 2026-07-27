"""Build the real-contract benchmark from the public model-contract corpus.

流程分三步（均可重复执行、确定性输出）：

1. ``prepare``  从 ``data/common_contracts``（国家市场监督管理总局示范文本库）
   按类型/年份/长度选取真实合同，抽取正文、按条款切分，
   生成 ``data/real_benchmark/contracts/*.md`` 与逐条款标注任务文件。
2. ``inject``   对其中一部分合同生成“对手方红线版”变体：改写或追加带风险的
   条款（含改写措辞的 hard 样本），注入位置与预期风险全部记录在案，
   作为 benchmark 中有已知答案的正样本。
3. ``assemble`` 合并条款级标注结果与注入记录，产出 ``annotations.json``。
   标注来源如实记录为 LLM 标注 + 待人工复核；复核用
   ``scripts/review_annotations.py``。

正文里未被标注为风险的条款一律视为负例，用于计算 Precision / 误报率。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from legalworkbench.parser import parse_clauses  # noqa: E402

CORPUS = ROOT / "data" / "common_contracts"
OUT = ROOT / "data" / "real_benchmark"
CONTRACTS = OUT / "contracts"
TASKS = OUT / "tasks"

RISK_TAXONOMY = (
    "auto_renewal",
    "unlimited_liability",
    "data_security",
    "payment_acceptance",
    "payment_cycle",
    "ip_ownership",
    "sla_remedy",
    "jurisdiction",
    "confidentiality",
    "termination_notice",
    "force_majeure",
    "deposit_return",
    "prepaid_refund",
)

# 标题关键词 → 项目内合同类型
TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("租赁", "lease"),
    ("物业", "service"),
    ("保管", "service"),
    ("仓储", "service"),
    ("托管", "service"),
    ("维修", "service"),
    ("家政", "service"),
    ("装饰", "construction"),
    ("装修", "construction"),
    ("施工", "construction"),
    ("建设", "construction"),
    ("旅游", "consumer"),
    ("健身", "consumer"),
    ("培训", "consumer"),
    ("美容", "consumer"),
    ("会员", "consumer"),
    ("养老", "consumer"),
    ("婚庆", "consumer"),
    ("摄影", "consumer"),
    ("买卖", "sales"),
    ("购销", "sales"),
    ("销售", "sales"),
    ("订购", "sales"),
    ("采购", "procurement"),
    ("委托", "procurement"),
    ("技术开发", "procurement"),
    ("技术服务", "procurement"),
    ("劳动", "employment"),
    ("聘用", "employment"),
    ("保密", "NDA"),
    ("供用电", "service"),
    ("供用水", "service"),
    ("供用气", "service"),
    ("供热", "service"),
    ("运输", "service"),
    ("广告", "service"),
    ("演出", "service"),
)

# ---------------------------------------------------------------------------
# 风险注入条款库：per risk_type 两种措辞（explicit 命中规则关键词 / paraphrase 刻意绕开）
# 用于生成“对手方红线版”变体，注入即已知正样本
# ---------------------------------------------------------------------------
INJECTION_BANK: dict[str, dict[str, dict[str, str]]] = {
    "unlimited_liability": {
        "explicit": {
            "title": "补充约定：赔偿责任",
            "text": "乙方违反本合同任何约定的，应赔偿甲方全部损失，包括直接损失、间接损失、预期利润损失及商誉损失，且不设赔偿责任上限。",
        },
        "paraphrase": {
            "title": "补充约定：损失补偿",
            "text": "乙方须就其履约瑕疵给甲方带来的各类经济影响承担开放式补偿义务，补偿金额不受本合同价款总额约束，亦不排除任何衍生性费用。",
        },
        "level": "high",
        "rationale": "责任范围覆盖间接损失且无金额上限，赔付敞口不可控。",
        "suggestion": "建议限定为直接损失，并设置累计赔偿上限（如合同总额或近12个月已付费用）。",
    },
    "jurisdiction": {
        "explicit": {
            "title": "补充约定：争议解决",
            "text": "因本合同引起的一切争议，双方均应提交乙方所在地人民法院管辖，甲方不得向其他法院提起诉讼。",
        },
        "paraphrase": {
            "title": "补充约定：分歧处理",
            "text": "凡因本合同产生的任何分歧，均提交服务方注册地有权机构处理，另一方不得另行选择处理途径。",
        },
        "level": "medium",
        "rationale": "管辖地单方偏向乙方，增加甲方维权成本和不确定性。",
        "suggestion": "建议改为甲方所在地法院或双方认可的中立仲裁机构。",
    },
    "auto_renewal": {
        "explicit": {
            "title": "补充约定：期限与续约",
            "text": "本合同期满后自动续约一年，续约次数不限，双方未约定提前通知或取消续约的方式。",
        },
        "paraphrase": {
            "title": "补充约定：期限延续",
            "text": "本合同于期间届满后依原有条件继续生效，任何一方未在届满前书面表示异议的，即视为认可延展，延展次数不受限制。",
        },
        "level": "medium",
        "rationale": "自动续约缺少通知期与退出路径，容易被动续约。",
        "suggestion": "建议明确续约前至少30日书面通知，并保留到期不续约路径。",
    },
    "payment_cycle": {
        "explicit": {
            "title": "补充约定：付款安排",
            "text": "甲方应在本合同签署后5日内一次性预付全部合同款项，付款安排不随交付进度调整。",
        },
        "paraphrase": {
            "title": "补充约定：款项拨付",
            "text": "全部合同价款于协议生效当周内一笔划付至乙方账户，后续履约进展不影响已收款项的归属与使用。",
        },
        "level": "medium",
        "rationale": "付款过度前置，未与交付进度匹配。",
        "suggestion": "建议拆分首付款、进度款与尾款，并绑定里程碑。",
    },
    "payment_acceptance": {
        "explicit": {
            "title": "补充约定：结算条件",
            "text": "甲方应在收到乙方请款通知后支付相应费用，付款不以验收合格或成果确认作为前提条件。",
        },
        "paraphrase": {
            "title": "补充约定：费用支付",
            "text": "费用拨付以乙方出具的书面申请为准，甲方不得以工作成果尚待确认为由暂缓支付。",
        },
        "level": "medium",
        "rationale": "付款义务未与验收或成果确认绑定，先付款后争议。",
        "suggestion": "建议将付款与交付完成、验收通过和发票开具绑定。",
    },
    "data_security": {
        "explicit": {
            "title": "补充约定：数据使用",
            "text": "乙方可收集并处理甲方及其客户的个人信息与业务数据用于自身经营分析，双方未约定安全措施、保存期限和数据泄露通知时限。",
        },
        "paraphrase": {
            "title": "补充约定：信息利用",
            "text": "乙方有权将履约过程中获取的用户资料与行为记录用于内部研究及产品改进，相关留存时间、防护标准与事故告知安排由乙方自行决定。",
        },
        "level": "high",
        "rationale": "个人信息处理缺少目的限制、安全措施与泄露通知约定。",
        "suggestion": "建议补充处理目的、保存期限、安全措施、泄露通知时限与责任分担。",
    },
    "confidentiality": {
        "explicit": {
            "title": "补充约定：保密义务",
            "text": "甲方对合作中知悉的乙方信息承担无限期保密义务，保密范围包括乙方提供的一切资料，且不设例外情形；乙方对甲方信息不承担对等义务。",
        },
        "paraphrase": {
            "title": "补充约定：信息守密",
            "text": "甲方须对知悉的乙方任何资料永久守密且无豁免情形，乙方对甲方资料的处理不受本条约束。",
        },
        "level": "medium",
        "rationale": "保密义务单方且无限期、无例外情形，义务不对等。",
        "suggestion": "建议双向保密、明确范围期限与法定披露等例外情形。",
    },
    "termination_notice": {
        "explicit": {
            "title": "补充约定：合同解除",
            "text": "乙方可基于自身经营安排随时单方解除本合同，无需提前通知甲方，也无需提供交接或过渡安排。",
        },
        "paraphrase": {
            "title": "补充约定：合作终止",
            "text": "乙方有权视运营情况即时终止本协议项下合作，终止决定自作出时生效，甲方不得要求过渡期或善后支持。",
        },
        "level": "medium",
        "rationale": "单方任意解除且无通知期与交接安排，业务连续性风险高。",
        "suggestion": "建议设置提前书面通知期、整改期与交接过渡安排。",
    },
    "deposit_return": {
        "explicit": {
            "title": "补充约定：保证金",
            "text": "甲方应向乙方支付保证金，合同终止后保证金是否返还及返还时间由乙方视情况确定，乙方有权从中扣除其认定的任何费用。",
        },
        "paraphrase": {
            "title": "补充约定：履约担保",
            "text": "甲方缴纳的担保款项在合作结束后的处理方式由收款方酌定，扣减项目及余额退付时点均以收款方内部核算为准。",
        },
        "level": "medium",
        "rationale": "保证金返还条件、扣除范围和期限均由单方决定。",
        "suggestion": "建议明确返还条件、扣除范围、返还期限与逾期责任。",
    },
    "prepaid_refund": {
        "explicit": {
            "title": "补充约定：预付费用",
            "text": "甲方预付的全部费用一经支付概不退还，服务提前终止的，未消费部分不予退款也不得转让。",
        },
        "paraphrase": {
            "title": "补充约定：储值处理",
            "text": "已充入账户的款项不适用任何返还安排，服务关系结束时账户剩余权益即行清零。",
        },
        "level": "high",
        "rationale": "预付费用概不退还且余额清零，消费者权益受损。",
        "suggestion": "建议明确未消费余额退还路径、退款期限与手续费边界。",
    },
    "ip_ownership": {
        "explicit": {
            "title": "补充约定：成果归属",
            "text": "本合同履行过程中形成的全部交付成果、衍生成果及改进成果的知识产权均归乙方所有，甲方仅可在本项目范围内使用。",
        },
        "paraphrase": {
            "title": "补充约定：产出物权利",
            "text": "合作期间产生的各项产出物及其后续优化版本的相关权利默认由服务提供方保留，委托方获得的使用授权以单个项目为限且不可转授。",
        },
        "level": "high",
        "rationale": "成果归属整体偏向乙方，限制甲方使用与商业化。",
        "suggestion": "建议交付成果归甲方，乙方背景知识产权另行授权。",
    },
    "sla_remedy": {
        "explicit": {
            "title": "补充约定：服务水平",
            "text": "服务可用性与故障处理均按乙方内部标准执行，双方未约定故障分级、响应时限或任何服务补偿机制。",
        },
        "paraphrase": {
            "title": "补充约定：运行保障",
            "text": "系统中断的恢复时点与处理方式由乙方视运营情况决定，甲方不得据此主张费用减免或其他补救。",
        },
        "level": "medium",
        "rationale": "SLA 缺少故障分级、响应时限和补偿机制。",
        "suggestion": "建议补充可用性指标、故障分级、响应时限与服务抵扣机制。",
    },
    "force_majeure": {
        "explicit": {
            "title": "补充约定：不可抗力",
            "text": "发生不可抗力时乙方可免除全部责任，且无需通知甲方、提交证明材料或采取减损措施；甲方不因不可抗力免除付款义务。",
        },
        "paraphrase": {
            "title": "补充约定：意外事件",
            "text": "遇有非乙方所能控制的事件时，乙方各项义务自动中止且无须履行任何告知或止损安排，甲方应照常履行己方义务。",
        },
        "level": "low",
        "rationale": "不可抗力免责单方化，缺少通知、证明与减损义务。",
        "suggestion": "建议约定通知期限、证明材料和双方对等的减损义务。",
    },
}

# 每种合同类型注入哪些风险（3-4 个，覆盖全部 taxonomy）
TYPE_INJECTIONS: dict[str, list[str]] = {
    "lease": ["deposit_return", "jurisdiction", "termination_notice", "auto_renewal"],
    "service": ["unlimited_liability", "sla_remedy", "termination_notice", "jurisdiction"],
    "construction": ["payment_cycle", "payment_acceptance", "force_majeure", "unlimited_liability"],
    "consumer": ["prepaid_refund", "data_security", "jurisdiction", "auto_renewal"],
    "sales": ["payment_acceptance", "payment_cycle", "jurisdiction", "unlimited_liability"],
    "procurement": ["ip_ownership", "payment_acceptance", "confidentiality", "unlimited_liability"],
    "employment": ["confidentiality", "ip_ownership", "termination_notice", "jurisdiction"],
    "NDA": ["confidentiality", "unlimited_liability", "jurisdiction"],
    "general": ["unlimited_liability", "jurisdiction", "termination_notice"],
}


def classify(title: str) -> str:
    for keyword, contract_type in TYPE_KEYWORDS:
        if keyword in title:
            return contract_type
    return "general"


def extract_body(markdown_text: str) -> str:
    """corpus markdown = 标题 + 元数据 bullets + ``## 正文`` + 正文。只保留正文。"""

    lines = markdown_text.splitlines()
    try:
        idx = next(i for i, line in enumerate(lines) if line.strip() == "## 正文")
    except StopIteration:
        return markdown_text
    title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else ""
    body = "\n".join(lines[idx + 1 :]).strip()
    return f"# {title}\n\n{body}\n" if title else body + "\n"


def select_contracts(
    limit: int,
    *,
    exclude_titles: set[str] | None = None,
    min_chars: int = 1200,
    max_chars: int = 12000,
    min_clauses: int = 6,
) -> list[dict]:
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    candidates = []
    for item in manifest.get("items", []):
        if item.get("status") != "ok":
            continue
        if exclude_titles and item.get("title") in exclude_titles:
            continue
        md_path = ROOT / item["markdown_path"]
        if not md_path.exists():
            continue
        body = extract_body(md_path.read_text(encoding="utf-8"))
        clauses = parse_clauses(body)
        if not (min_chars <= len(body) <= max_chars and len(clauses) >= min_clauses):
            continue
        candidates.append(
            {
                "item": item,
                "body": body,
                "clauses": clauses,
                "contract_type": classify(item.get("title", "")),
            }
        )
    # 轮转各类型取样，保证类型多样性；同类型内偏好新年份
    by_type: dict[str, list[dict]] = {}
    for cand in candidates:
        by_type.setdefault(cand["contract_type"], []).append(cand)
    for group in by_type.values():
        group.sort(key=lambda c: str(c["item"].get("year") or ""), reverse=True)
    selected: list[dict] = []
    type_order = sorted(by_type, key=lambda t: -len(by_type[t]))
    while len(selected) < limit and any(by_type.values()):
        for contract_type in type_order:
            if by_type[contract_type] and len(selected) < limit:
                selected.append(by_type[contract_type].pop(0))
    return selected


def cmd_prepare(limit: int, *, extend: bool = False) -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    TASKS.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if extend and (OUT / "selection.json").exists():
        existing = json.loads((OUT / "selection.json").read_text(encoding="utf-8"))
    if extend:
        # 扩容：保留已有编号与标注，放宽长度门槛把剩余可用的真实合同全部纳入
        selected = select_contracts(
            limit,
            exclude_titles={row["title"] for row in existing},
            min_chars=800,
            max_chars=20000,
            min_clauses=5,
        )
    else:
        selected = select_contracts(limit)
    index = list(existing)
    for pos, cand in enumerate(selected, start=len(existing) + 1):
        contract_id = f"real_{pos:03d}"
        file_name = f"{contract_id}.md"
        (CONTRACTS / file_name).write_text(cand["body"], encoding="utf-8")
        item = cand["item"]
        task = {
            "contract_id": contract_id,
            "title": item.get("title", ""),
            "contract_type": cand["contract_type"],
            "source": {
                "corpus": "国家市场监督管理总局合同示范文本库",
                "detail_url": item.get("detail_url", ""),
                "department": item.get("department", ""),
                "year": item.get("year", ""),
                "corpus_markdown": item.get("markdown_path", ""),
            },
            "file": f"contracts/{file_name}",
            "clauses": [
                {"clause_id": c.clause_id, "title": c.title, "text": c.text}
                for c in cand["clauses"]
            ],
        }
        (TASKS / f"{contract_id}.clauses.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        index.append(
            {
                "contract_id": contract_id,
                "title": item.get("title", ""),
                "contract_type": cand["contract_type"],
                "year": item.get("year", ""),
                "clauses": len(cand["clauses"]),
                "chars": len(cand["body"]),
            }
        )
    (OUT / "selection.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"selected": len(index)}, ensure_ascii=False))
    for row in index:
        print(f"  {row['contract_id']}  {row['contract_type']:<12} {row['year']}  "
              f"{row['clauses']:>3} clauses  {row['chars']:>5} chars  {row['title'][:40]}")


# ---------------------------------------------------------------------------
# inject
# ---------------------------------------------------------------------------

APPEND_HEADER = "## 补充约定"


def _modify_liability(body: str) -> tuple[str, str] | None:
    """在既有违约责任条款内追加无上限赔偿句 → (新正文, 被改条款的匹配句)。"""

    marker = "并应赔偿由此给对方造成的全部损失（含间接损失、预期利益损失及商誉损失），赔偿总额不设任何上限。"
    for clause in parse_clauses(body):
        if any(key in clause.title for key in ("违约", "责任")) and marker not in clause.text:
            lines = clause.text.splitlines()
            anchor = lines[0]
            new_first = anchor.rstrip("。") + "。违约方" + marker
            return body.replace(anchor, new_first, 1), marker
    return None


def cmd_inject(ratio: float, seed: int) -> None:
    import random

    rng = random.Random(seed)
    selection = json.loads((OUT / "selection.json").read_text(encoding="utf-8"))
    variant_rows = []
    injections: dict[str, list[dict]] = {}
    picked = [row for i, row in enumerate(selection) if i % max(1, round(1 / ratio)) == 0]
    for row in picked:
        contract_id = row["contract_id"]
        variant_id = f"{contract_id}_redline"
        base_body = (CONTRACTS / f"{contract_id}.md").read_text(encoding="utf-8")
        contract_type = row["contract_type"]
        risk_types = TYPE_INJECTIONS.get(contract_type, TYPE_INJECTIONS["general"])
        body = base_body.rstrip() + "\n"
        gold: list[dict] = []

        modified = _modify_liability(body) if "unlimited_liability" in risk_types else None
        inject_types = [r for r in risk_types if not (modified and r == "unlimited_liability")]
        if modified:
            body, marker = modified
            gold.append({"risk_type": "unlimited_liability", "match_text": marker, "mode": "modified", "phrasing": "explicit"})

        blocks = [APPEND_HEADER, "经双方协商，对本合同作如下补充约定，与本合同正文具有同等效力："]
        for risk_type in inject_types:
            bank = INJECTION_BANK[risk_type]
            phrasing = rng.choice(["explicit", "paraphrase"])
            entry = bank[phrasing]
            blocks.extend([f"### {entry['title']}", entry["text"]])
            gold.append({"risk_type": risk_type, "match_text": entry["text"], "mode": "injected", "phrasing": phrasing})
        body = body + "\n" + "\n".join(blocks) + "\n"

        # 重新解析定位注入条款的 clause_id
        clauses = parse_clauses(body)
        for g in gold:
            g["clause_id"] = next(
                (c.clause_id for c in clauses if g["match_text"] in c.text), ""
            )
        missing = [g["risk_type"] for g in gold if not g["clause_id"]]
        if missing:
            raise RuntimeError(f"{variant_id}: 注入条款定位失败 {missing}")

        (CONTRACTS / f"{variant_id}.md").write_text(body, encoding="utf-8")
        injections[variant_id] = [
            {
                "clause_id": g["clause_id"],
                "risk_type": g["risk_type"],
                "risk_level": INJECTION_BANK[g["risk_type"]]["level"],
                "rationale": INJECTION_BANK[g["risk_type"]]["rationale"],
                "expected_suggestion": INJECTION_BANK[g["risk_type"]]["suggestion"],
                "mode": g["mode"],
                "phrasing": g["phrasing"],
            }
            for g in gold
        ]
        variant_rows.append(
            {
                "variant_id": variant_id,
                "base_contract_id": contract_id,
                "contract_type": contract_type,
                "file": f"contracts/{variant_id}.md",
                "injected": len(gold),
            }
        )
    (OUT / "injections.json").write_text(
        json.dumps({"variants": variant_rows, "gold": injections}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"variants": len(variant_rows), "injected_risks": sum(r["injected"] for r in variant_rows)}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------

def cmd_assemble(annotation_dir: Path) -> None:
    selection = json.loads((OUT / "selection.json").read_text(encoding="utf-8"))
    injections = json.loads((OUT / "injections.json").read_text(encoding="utf-8"))
    contracts_payload = []
    risk_seq = 0
    problems: list[str] = []

    llm_annotations: dict[str, list[dict]] = {}
    annotators: dict[str, str] = {}
    for path in sorted(annotation_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        for entry in items:
            llm_annotations[entry["contract_id"]] = entry.get("annotations", [])
            annotators[entry["contract_id"]] = str(entry.get("annotator") or "llm:claude-fable-5")

    def build_contract(contract_id: str, meta: dict, file: str, raw_annotations: list[dict], origin: str, base_contract_id: str = "", annotator: str = "") -> dict:
        nonlocal risk_seq, problems
        body = (OUT / file).read_text(encoding="utf-8")
        clauses = {c.clause_id: c for c in parse_clauses(body)}
        annotations = []
        for ann in raw_annotations:
            clause_id = ann.get("clause_id", "")
            risk_type = ann.get("risk_type", "")
            if clause_id not in clauses:
                problems.append(f"{contract_id}: clause {clause_id} 不存在")
                continue
            if risk_type not in RISK_TAXONOMY:
                problems.append(f"{contract_id}: 非法 risk_type {risk_type}")
                continue
            risk_seq += 1
            annotations.append(
                {
                    "risk_id": f"RB{risk_seq:04d}",
                    "clause_id": clause_id,
                    "clause_title": clauses[clause_id].title,
                    "risk_type": risk_type,
                    "risk_level": ann.get("risk_level", "medium"),
                    "rationale": ann.get("rationale", ""),
                    "expected_suggestion": ann.get("expected_suggestion", ann.get("suggestion", "")),
                    "evidence_source": ann.get("evidence_source", "llm_annotation"),
                    "requires_human_review": bool(ann.get("requires_human_review", ann.get("risk_level") == "high")),
                    "annotation_notes": ann.get("annotation_notes", origin),
                }
            )
        # 去重：同 clause 同 risk_type 只保留一条
        deduped: dict[tuple[str, str], dict] = {}
        for ann in annotations:
            deduped.setdefault((ann["clause_id"], ann["risk_type"]), ann)
        annotations = sorted(deduped.values(), key=lambda a: (a["clause_id"], a["risk_type"]))
        positive_ids = {a["clause_id"] for a in annotations}
        return {
            "contract_id": contract_id,
            "title": meta.get("title", contract_id),
            "contract_type": meta.get("contract_type", "general"),
            "scenario": origin,
            "file": file,
            "base_contract_id": base_contract_id,
            "annotator": annotator or "llm:claude-fable-5",
            "review_status": "pending_human_review",
            "clause_count": len(clauses),
            "negative_clause_ids": sorted(set(clauses) - positive_ids),
            "annotations": annotations,
        }

    for row in selection:
        contract_id = row["contract_id"]
        contracts_payload.append(
            build_contract(
                contract_id,
                row,
                f"contracts/{contract_id}.md",
                llm_annotations.get(contract_id, []),
                origin="real_model_contract",
                annotator=annotators.get(contract_id, ""),
            )
        )

    base_by_id = {row["contract_id"]: row for row in selection}
    for variant in injections.get("variants", []):
        variant_id = variant["variant_id"]
        base_id = variant["base_contract_id"]
        base_meta = dict(base_by_id.get(base_id, {}))
        base_meta["title"] = f"{base_meta.get('title', base_id)}（对手方红线版）"
        gold = injections["gold"].get(variant_id, [])
        variant_body = (OUT / variant["file"]).read_text(encoding="utf-8")
        variant_clauses = {c.clause_id: c for c in parse_clauses(variant_body)}
        # 原合同上的 LLM 标注按条款文本迁移到变体（条款未被改动时文本一致）
        carried: list[dict] = []
        base_body = (CONTRACTS / f"{base_id}.md").read_text(encoding="utf-8")
        base_clauses = {c.clause_id: c for c in parse_clauses(base_body)}
        for ann in llm_annotations.get(base_id, []):
            base_clause = base_clauses.get(ann.get("clause_id", ""))
            if base_clause is None:
                continue
            match = next((cid for cid, c in variant_clauses.items() if c.text == base_clause.text), "")
            if match:
                carried.append({**ann, "clause_id": match})
        injected = [
            {
                "clause_id": g["clause_id"],
                "risk_type": g["risk_type"],
                "risk_level": g["risk_level"],
                "rationale": g["rationale"],
                "expected_suggestion": g["expected_suggestion"],
                "evidence_source": "injected_redline",
                "requires_human_review": g["risk_level"] == "high",
                "annotation_notes": f"{g['mode']}:{g['phrasing']}",
            }
            for g in gold
        ]
        contracts_payload.append(
            build_contract(
                variant_id,
                base_meta,
                variant["file"],
                carried + injected,
                origin="injected_redline_variant",
                base_contract_id=base_id,
                annotator=f"{annotators.get(base_id, 'llm:claude-fable-5')}+deterministic_injection",
            )
        )

    total_annotations = sum(len(c["annotations"]) for c in contracts_payload)
    total_negatives = sum(len(c["negative_clause_ids"]) for c in contracts_payload)
    payload = {
        "name": "real_contract_benchmark",
        "version": "v2.2026.07",
        "description": (
            "Real public model contracts (国家市场监督管理总局示范文本库) with clause-level risk annotations. "
            "Positives = LLM-annotated real-clause risks + recorded red-line injections; "
            "all other clauses are negatives for precision measurement."
        ),
        "annotation_provenance": {
            "annotator": " | ".join(sorted(set(annotators.values()))) or "llm:claude-fable-5",
            "method": "clause-by-clause LLM annotation against 13-type risk taxonomy + deterministic red-line injection",
            "review_status": "pending_human_review",
            "review_tool": "scripts/review_annotations.py",
        },
        "contracts": contracts_payload,
    }
    (OUT / "annotations.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "contracts": len(contracts_payload),
        "annotated_risks": total_annotations,
        "negative_clauses": total_negatives,
        "problems": problems,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--limit", type=int, default=24)
    p_prepare.add_argument("--extend", action="store_true", help="Append new contracts, keep existing selection")
    p_inject = sub.add_parser("inject")
    p_inject.add_argument("--ratio", type=float, default=0.5)
    p_inject.add_argument("--seed", type=int, default=20260726)
    p_assemble = sub.add_parser("assemble")
    p_assemble.add_argument("--annotations", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd == "prepare":
        cmd_prepare(args.limit, extend=args.extend)
    elif args.cmd == "inject":
        cmd_inject(args.ratio, args.seed)
    elif args.cmd == "assemble":
        cmd_assemble(args.annotations)


if __name__ == "__main__":
    main()
