"""Context selection and provenance tests."""

from __future__ import annotations

import time

from legalworkbench.context import ContextManager
from legalworkbench.models import (
    ContractClause,
    LegalMemory,
    RetrievedEvidence,
    ReviewRun,
)


def make_run() -> ReviewRun:
    now = time.time()
    return ReviewRun(
        review_run_id="law_context",
        tenant_id="tenant_a",
        contract_path="contract.md",
        created_at=now,
        updated_at=now,
    )


def test_context_manager_filters_memory_and_records_provenance() -> None:
    run = make_run()
    clause = ContractClause(clause_id="c1", title="赔偿责任", text="乙方承担全部损失。")
    evidence = RetrievedEvidence(
        entry_id="e1",
        title="责任上限政策",
        source="company_policy:liability",
        score=1.0,
        reason="hybrid",
        body_preview="建议设置责任上限。",
    )
    memories = [
        LegalMemory(
            memory_id="active",
            type="procedural",
            tenant_id="tenant_a",
            summary="检查责任上限",
            approved_advice="建议设置上限",
            status="active",
        ),
        LegalMemory(
            memory_id="proposed",
            type="semantic",
            tenant_id="tenant_a",
            summary="未审核结论",
            status="proposed",
        ),
        LegalMemory(
            memory_id="other_tenant",
            type="semantic",
            tenant_id="tenant_b",
            summary="其他租户结论",
            status="active",
        ),
    ]

    packet = ContextManager(default_budget=200).build_for_clause(
        run,
        clause,
        task="legal_risk_semantic_judgment",
        evidence=[evidence],
        memories=memories,
    )

    assert packet.used_tokens <= packet.token_budget
    assert "原文：乙方承担全部损失。" in packet.text
    assert "active" in {item.source for item in packet.selected}
    assert "proposed" not in packet.text
    assert "other_tenant" not in packet.text
    assert {item["kind"] for item in packet.trace()["selected"]} >= {"current_clause"}


def test_context_manager_deduplicates_and_reports_budget_omissions() -> None:
    run = make_run()
    clause = ContractClause(clause_id="c1", title="保密", text="保密信息应妥善保护。")
    duplicate = RetrievedEvidence(
        entry_id="e1",
        title="同一证据",
        source="policy",
        score=1.0,
        reason="",
        body_preview="保密信息应妥善保护。",
    )
    packet = ContextManager(default_budget=128).build_for_clause(
        run,
        clause,
        task="refine_query",
        evidence=[duplicate, duplicate],
    )

    reasons = {item["reason"] for item in packet.omitted}
    assert "duplicate" in reasons
    assert packet.used_tokens <= packet.token_budget
