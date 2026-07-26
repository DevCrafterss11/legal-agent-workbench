# Human-Annotated Benchmark

This project includes a v1 human-annotation-style benchmark for contract review evaluation.

## Dataset

- Path: `data/human_benchmark/`
- Contracts: 30
- Clause-level risk annotations: 120
- Hard paraphrase cases: 12
- Contract types: SaaS, procurement, NDA, lease, service, employment, consumer, construction, sales
- Risk taxonomy: auto renewal, unlimited liability, data security, payment acceptance, payment cycle, IP ownership, SLA remedy, jurisdiction, confidentiality, termination notice, force majeure, deposit return, prepaid refund

Each annotation contains:

- `risk_type`
- `risk_level`
- `clause_id`
- `rationale`
- `expected_suggestion`
- `evidence_source`
- `requires_human_review`

## Evaluation

Run:

```bash
.venv/bin/legal-agent eval --human
```

Metrics:

- `risk_recall_at_10`: whether RAG + rules can recover the annotated risk type.
- `rule_recall`: whether deterministic rules alone catch the annotated risk.
- `source_coverage_at_10`: whether retrieved evidence includes the expected risk source.
- `high_risk_recall`: recall on high-risk annotations.
- `human_review_capture_rate`: whether high-risk / human-review annotations are routed to review.

Current v1 result:

```text
contracts: 30
annotated_risks: 120
risk_recall_at_10: 1.0
rule_recall: 0.9
source_coverage_at_10: 1.0
high_risk_recall: 1.0
human_review_capture_rate: 1.0
```

## Interview Notes

This benchmark is intentionally not only keyword-explicit. Twelve annotations use paraphrased risk expressions, so the rule-only score is lower than the RAG + rule score. This makes the evaluation useful for explaining why the system combines BM25/vector retrieval, rules, and review gating instead of relying on one method.
