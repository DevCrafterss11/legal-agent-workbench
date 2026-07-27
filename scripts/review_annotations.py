"""Interactive human review of benchmark annotations.

程序化/LLM 标注 → 人工逐条复核的收口工具。逐条展示条款原文与标注，
由真人确认（y）、修改级别（e）、拒绝（n）或跳过（s）。复核结论写回
``annotations.json``：每条标注记录 ``review``（confirmed/rejected/edited），
全部复核完成后合同级 ``review_status`` 升级为 ``human_reviewed``，
数据集级 provenance 记录复核人与时间。

只有走完这个流程，对外口径才可以说“人工复核标注”；在此之前
数据集口径是“LLM 标注（待人工复核）”。

用法：
    .venv/bin/python scripts/review_annotations.py --reviewer 付豪
    .venv/bin/python scripts/review_annotations.py --reviewer 付豪 --contracts real_002
    .venv/bin/python scripts/review_annotations.py --status   # 只看进度
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from legalworkbench.parser import parse_clauses  # noqa: E402

BENCH = ROOT / "data" / "real_benchmark"
ANNOTATIONS = BENCH / "annotations.json"


def load() -> dict:
    return json.loads(ANNOTATIONS.read_text(encoding="utf-8"))


def save(payload: dict) -> None:
    ANNOTATIONS.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def show_status(payload: dict) -> None:
    total = confirmed = rejected = pending = 0
    for contract in payload.get("contracts", []):
        for ann in contract.get("annotations", []):
            total += 1
            verdict = (ann.get("review") or {}).get("verdict", "")
            if verdict in {"confirmed", "edited"}:
                confirmed += 1
            elif verdict == "rejected":
                rejected += 1
            else:
                pending += 1
    print(f"annotations: {total} | confirmed/edited: {confirmed} | rejected: {rejected} | pending: {pending}")
    print(f"dataset review_status: {payload.get('annotation_provenance', {}).get('review_status')}")


def review(payload: dict, reviewer: str, only_contracts: set[str]) -> None:
    contracts = payload.get("contracts", [])
    reviewed_any = False
    for contract in contracts:
        if only_contracts and contract["contract_id"] not in only_contracts:
            continue
        pending = [a for a in contract.get("annotations", []) if not (a.get("review") or {}).get("verdict")]
        if not pending:
            continue
        body = (BENCH / contract["file"]).read_text(encoding="utf-8")
        clauses = {c.clause_id: c for c in parse_clauses(body)}
        print(f"\n===== {contract['contract_id']}  {contract['title']}  ({len(pending)} 条待复核) =====")
        for ann in pending:
            clause = clauses.get(ann["clause_id"])
            print("-" * 72)
            print(f"[{ann['risk_id']}] {ann['clause_id']} {ann.get('clause_title', '')}")
            print(f"risk_type={ann['risk_type']}  level={ann['risk_level']}  notes={ann.get('annotation_notes', '')}")
            print(f"标注理由：{ann.get('rationale', '')}")
            if clause is not None:
                text = clause.text if len(clause.text) <= 600 else clause.text[:600] + "……"
                print(f"条款原文：\n{text}")
            answer = ""
            while answer not in {"y", "n", "e", "s", "q"}:
                answer = input("确认(y) / 拒绝(n) / 改级别(e) / 跳过(s) / 退出(q) > ").strip().lower()
            if answer == "q":
                save(payload)
                print("已保存进度并退出。")
                return
            if answer == "s":
                continue
            reviewed_any = True
            verdict = {"y": "confirmed", "n": "rejected", "e": "edited"}[answer]
            if answer == "e":
                level = ""
                while level not in {"low", "medium", "high"}:
                    level = input("新 risk_level (low/medium/high) > ").strip().lower()
                ann["risk_level"] = level
            ann["review"] = {"verdict": verdict, "reviewer": reviewer, "reviewed_at": time.time()}
        # 合同内全部有结论后升级合同级状态
        if all((a.get("review") or {}).get("verdict") for a in contract.get("annotations", [])):
            contract["review_status"] = "human_reviewed"
    # rejected 的标注保留在文件里作复核痕迹，但评测加载时会剔除
    if reviewed_any:
        provenance = payload.setdefault("annotation_provenance", {})
        if all(
            (a.get("review") or {}).get("verdict")
            for c in contracts
            for a in c.get("annotations", [])
        ):
            provenance["review_status"] = "human_reviewed"
        provenance.setdefault("reviewers", [])
        if reviewer not in provenance["reviewers"]:
            provenance["reviewers"].append(reviewer)
        provenance["last_reviewed_at"] = time.time()
    save(payload)
    show_status(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewer", default="", help="复核人姓名（必填，写入数据集溯源）")
    parser.add_argument("--contracts", default="", help="只复核这些合同（逗号分隔）")
    parser.add_argument("--status", action="store_true", help="只显示复核进度")
    args = parser.parse_args()
    payload = load()
    if args.status:
        show_status(payload)
        return
    if not args.reviewer:
        raise SystemExit("--reviewer 必填：复核记录需要真实署名")
    review(payload, args.reviewer, {c.strip() for c in args.contracts.split(",") if c.strip()})


if __name__ == "__main__":
    main()
