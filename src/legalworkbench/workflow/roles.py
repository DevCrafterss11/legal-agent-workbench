"""Named workflow roles used by the legal agent runtime."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    role: str
    tool: str
    description: str


class LegalReviewWorkflow:
    """Declarative multi-agent workflow for contract review."""

    def __init__(self) -> None:
        self.steps = [
            WorkflowStep("parse", "ParserAgent", "contract_parser", "识别合同类型并拆分条款"),
            WorkflowStep("retrieve", "RetrievalAgent", "clause_retriever", "对每个条款召回制度证据与历史记忆"),
            WorkflowStep("risk_check", "RiskReviewer", "risk_rule", "结合规则与证据生成风险发现"),
            WorkflowStep("rewrite", "ClauseRewriter", "clause_rewriter", "生成可落地的修改建议"),
            WorkflowStep("compliance", "ComplianceAuditor", "permission_guard", "拦截无来源或高风险输出"),
            WorkflowStep("report", "ReportWriter", "report_export", "输出可追溯审查报告"),
        ]

    def describe(self) -> list[dict[str, str]]:
        return [step.__dict__.copy() for step in self.steps]
