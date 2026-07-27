"""Automated clause-level risk annotation pipeline (real LLM calls, no mocks).

对 ``data/real_benchmark/tasks/*.clauses.json`` 逐条款调用 LLM 做 13 类风险标注，
输出与 ``build_real_benchmark.py assemble`` 兼容的标注文件。

诚实口径：这是**程序化 LLM 标注**，不是人工标注。所有产出默认
``requires_human_review=true``，请用 ``scripts/review_annotations.py`` 逐条人工
复核后，数据集口径才能升级为“程序化标注 + 人工复核”。

注意：标注模型与被评测系统若使用同一个模型（如 glm-4-flash），存在
评审圈闭（annotator-system circularity）。建议用 ``--model`` 指定一个更强的
独立模型做标注，或依赖人工复核消解。

用法：
    .venv/bin/python scripts/annotate_contracts.py --only-missing
    .venv/bin/python scripts/annotate_contracts.py --contracts real_025,real_026 --model glm-4-plus
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from legalworkbench.llm.client import LlmClient, _extract_json_object, load_llm_config  # noqa: E402

TASKS = ROOT / "data" / "real_benchmark" / "tasks"
OUT = ROOT / "data" / "real_benchmark" / "llm_annotations"

RISK_TAXONOMY = {
    "auto_renewal": "自动续约无通知期/退出路径",
    "unlimited_liability": "赔偿含间接损失且无上限",
    "data_security": "个人信息/数据处理缺安全措施、泄露通知约定",
    "payment_acceptance": "付款不与验收/成果确认绑定",
    "payment_cycle": "付款过度前置、一次性预付",
    "ip_ownership": "成果知识产权归属偏向乙方",
    "sla_remedy": "服务水平无故障分级/响应时限/补偿",
    "jurisdiction": "争议解决单方固定偏向乙方所在地",
    "confidentiality": "保密义务单方/无限期/无例外",
    "termination_notice": "单方任意解除且无通知期/交接",
    "force_majeure": "不可抗力免责单方化、无通知减损义务",
    "deposit_return": "保证金/押金返还条件、扣除范围单方决定",
    "prepaid_refund": "预付费概不退还/余额清零",
}

SYSTEM_PROMPT = (
    "你是资深中国企业法务合同审查员，从合同中甲方（委托方/买方/承租方/消费者一侧）的利益视角，"
    "判断单个条款是否构成给定 13 类风险之一。这些合同多为官方示范文本，绝大多数条款是均衡的："
    "空白待填字段、□选择项、双向对等条款、使用说明、签署页一律不算风险。"
    "只有条款书面内容本身已构成对甲方明显不利的模式时才标注，拿不准就返回空数组。"
    '只输出一个 JSON 对象，格式：{"risks":[{"risk_type":"...","risk_level":"low|medium|high",'
    '"rationale":"一句中文理由，引用条款关键表述","suggestion":"一句中文修改建议"}]}，无风险时 {"risks":[]}。'
    "risk_type 只能取："
    + "；".join(f"{key}={value}" for key, value in RISK_TAXONOMY.items())
)


def annotate_clause(llm: LlmClient, clause: dict, contract_title: str, contract_type: str) -> list[dict]:
    if len(clause.get("text", "").strip()) < 30:
        return []  # 页眉/空白表单行等，直接负例，不浪费调用
    user = json.dumps(
        {
            "contract_title": contract_title,
            "contract_type": contract_type,
            "clause_title": clause.get("title", ""),
            "clause_text": clause.get("text", ""),
        },
        ensure_ascii=False,
    )
    try:
        response = llm.complete(system=SYSTEM_PROMPT, user=user)
    except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整体，如实记录
        return [{"_error": str(exc)[:200]}]
    parsed = _extract_json_object(response.text) or {}
    risks = parsed.get("risks")
    if not isinstance(risks, list):
        return []
    output = []
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        risk_type = str(risk.get("risk_type") or "")
        if risk_type not in RISK_TAXONOMY:
            continue
        level = str(risk.get("risk_level") or "medium")
        output.append(
            {
                "clause_id": clause["clause_id"],
                "risk_type": risk_type,
                "risk_level": level if level in {"low", "medium", "high"} else "medium",
                "rationale": str(risk.get("rationale") or ""),
                "expected_suggestion": str(risk.get("suggestion") or ""),
                # 程序化标注一律待人工复核，绝不冒充人工标注
                "requires_human_review": True,
                "annotation_notes": "llm_script",
            }
        )
    return output


def annotate_contract(llm: LlmClient, task_path: Path, model_label: str) -> dict:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    annotations: list[dict] = []
    errors: list[str] = []
    for clause in task.get("clauses", []):
        for item in annotate_clause(llm, clause, task.get("title", ""), task.get("contract_type", "general")):
            if "_error" in item:
                errors.append(f"{clause['clause_id']}: {item['_error']}")
            else:
                annotations.append(item)
    return {
        "contract_id": task["contract_id"],
        "annotator": f"script:{model_label}",
        "annotations": annotations,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=TASKS)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--model", default="", help="Override annotator model (defaults to configured llm.model)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--contracts", default="", help="Comma list of contract_ids; empty = all")
    parser.add_argument("--only-missing", action="store_true", help="Skip contracts already annotated in --out")
    args = parser.parse_args()

    config = load_llm_config(ROOT)
    if args.model:
        config = replace(config, model=args.model)
    llm = LlmClient(config)
    if llm.remote_endpoint() is None:
        raise SystemExit(
            "未配置远端 LLM（settings.json llm 段 + secrets.json llm_api_key）。"
            "程序化标注必须使用真实模型，不提供本地模拟标注。"
        )

    args.out.mkdir(parents=True, exist_ok=True)
    wanted = {item.strip() for item in args.contracts.split(",") if item.strip()}
    task_paths = []
    for path in sorted(args.tasks.glob("*.clauses.json")):
        contract_id = path.name.replace(".clauses.json", "")
        if wanted and contract_id not in wanted:
            continue
        if args.only_missing and (args.out / f"{contract_id}.json").exists():
            continue
        task_paths.append(path)
    if not task_paths:
        print("nothing to annotate")
        return

    started = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda p: annotate_contract(llm, p, config.model), task_paths):
            out_path = args.out / f"{result['contract_id']}.json"
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            done += 1
            print(
                f"[{done}/{len(task_paths)}] {result['contract_id']}: "
                f"{len(result['annotations'])} risks, {len(result['errors'])} errors"
            )
    print(json.dumps({"annotated_contracts": done, "seconds": round(time.time() - started, 1), "model": config.model}, ensure_ascii=False))


if __name__ == "__main__":
    main()
