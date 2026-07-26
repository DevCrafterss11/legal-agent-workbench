#!/usr/bin/env python3
"""Run baseline comparisons for the Legal Agent Workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from legalworkbench.evals import BaselineEvaluator, format_baseline_table  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare rule-only, RAG-only, and full-system evaluation baselines.")
    parser.add_argument("--cwd", default=str(ROOT), help="Project root containing .lawbench and data directories.")
    parser.add_argument("--dataset", choices=["synthetic", "human", "both"], default="both", help="Dataset to evaluate.")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format.")
    args = parser.parse_args()

    rows = BaselineEvaluator(args.cwd).run(dataset=args.dataset)
    if args.format == "json":
        print(json.dumps([row.to_dict() for row in rows], ensure_ascii=False, indent=2))
        return
    print("Baseline comparison:")
    print(format_baseline_table(rows))


if __name__ == "__main__":
    main()
