"""Freeze a deterministic, stratified held-out split of the real benchmark.

The split is selected by contract id hash, separately for original contracts and
redline variants, so evaluation order or later corpus sorting cannot change it.
Run this once after annotations are frozen, then treat the generated JSON as a
read-only evaluation artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path


def build(source: Path, target: Path, *, ratio: float = 0.2) -> int:
    payload = json.loads(source.read_text(encoding="utf-8"))
    contracts = list(payload.get("contracts") or [])
    selected: list[dict] = []
    for contract in contracts:
        scenario = str(contract.get("scenario") or "")
        bucket = "variant" if scenario == "injected_redline_variant" else "original"
        key = f"{bucket}:{contract.get('contract_id', '')}".encode("utf-8")
        score = int(hashlib.sha256(key).hexdigest()[:12], 16) / float(16**12)
        if score < ratio:
            selected.append(contract)
    if not selected:
        raise ValueError("held-out split is empty; increase --ratio")
    frozen = {
        **payload,
        "name": f"{payload.get('name', 'real_contract_benchmark')}_heldout",
        "version": f"{payload.get('version', '')}-heldout",
        "split": "heldout",
        "split_policy": {
            "algorithm": "sha256(contract_type_bucket:contract_id)",
            "ratio": ratio,
            "source": str(source),
            "frozen_at": time.time(),
            "note": "Do not use held-out annotations to tune rules, prompts, or thresholds.",
        },
        "contracts": selected,
    }
    target.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(selected)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/real_benchmark/annotations.json"))
    parser.add_argument("--target", type=Path, default=Path("data/real_benchmark/annotations_heldout.json"))
    parser.add_argument("--ratio", type=float, default=0.2)
    args = parser.parse_args()
    if not 0 < args.ratio < 1:
        parser.error("--ratio must be between 0 and 1")
    args.target.parent.mkdir(parents=True, exist_ok=True)
    print(f"wrote {build(args.source, args.target, ratio=args.ratio)} contracts to {args.target}")


if __name__ == "__main__":
    main()
