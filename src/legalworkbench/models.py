"""Domain models for the legal agent workbench."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ReviewStatus = Literal[
    "created",
    "parsing",
    "retrieving",
    "risk_checking",
    "rewriting",
    "compliance_reviewing",
    "reporting",
    "completed",
    "blocked",
    "failed",
]


class ContractClause(BaseModel):
    clause_id: str
    title: str
    text: str


class KnowledgeEntry(BaseModel):
    id: str
    title: str
    body: str
    contract_type: str = "general"
    clause_type: str = "general"
    risk_type: str = "general"
    risk_level: str = "medium"
    source: str = "local"
    tags: list[str] = Field(default_factory=list)


class RetrievedEvidence(BaseModel):
    entry_id: str
    title: str
    source: str
    score: float
    reason: str
    body_preview: str
    risk_type: str = "general"
    risk_level: str = "medium"
    rerank_score: float = 0.0


class RiskFinding(BaseModel):
    finding_id: str
    clause_id: str
    clause_title: str
    risk_type: str
    risk_level: str
    summary: str
    evidence: list[RetrievedEvidence] = Field(default_factory=list)
    rule_hits: list[str] = Field(default_factory=list)
    semantic_score: float = 0.0
    confidence: float = 0.0
    source_coverage: float = 0.0
    suggestion: str = ""
    requires_human_review: bool = False
    blocked: bool = False
    block_reason: str = ""


class LegalMemory(BaseModel):
    memory_id: str
    type: Literal["semantic", "episodic", "procedural", "preference"]
    contract_type: str = "general"
    clause_type: str = "general"
    risk_type: str = "general"
    risk_level: str = "medium"
    summary: str
    approved_advice: str = ""
    source_review_run_id: str = ""
    approved_by_human: bool = False
    confidence: float = 0.0
    tags: list[str] = Field(default_factory=list)


class LegalSkill(BaseModel):
    name: str
    contract_type: str
    description: str
    focus_clause_types: list[str] = Field(default_factory=list)
    risk_rules: list[str] = Field(default_factory=list)
    report_style: str = "concise"
    priority: int = 50
    retrieval_top_k: int = 10
    review_playbook: list[str] = Field(default_factory=list)


class ToolCallTrace(BaseModel):
    tool_name: str
    status: Literal["success", "blocked", "error"] = "success"
    input_summary: str = ""
    output_summary: str = ""
    duration_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReflectionCheck(BaseModel):
    check_id: str
    target: str
    status: Literal["pass", "warn", "block"]
    summary: str
    recommendation: str = ""
    evidence_count: int = 0
    requires_human_review: bool = False


class CompactSnapshot(BaseModel):
    snapshot_id: str
    source_tokens: int
    retained_tokens: int
    retention_rate: float
    retained_clause_ids: list[str] = Field(default_factory=list)
    retained_risk_types: list[str] = Field(default_factory=list)
    summary: str = ""


class ReviewRun(BaseModel):
    review_run_id: str
    status: ReviewStatus = "created"
    contract_path: str
    contract_type: str = "general"
    created_at: float
    updated_at: float
    clauses: list[ContractClause] = Field(default_factory=list)
    findings: list[RiskFinding] = Field(default_factory=list)
    memory_hits: list[LegalMemory] = Field(default_factory=list)
    reflection_checks: list[ReflectionCheck] = Field(default_factory=list)
    compact_snapshot: CompactSnapshot | None = None
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    selected_skills: list[str] = Field(default_factory=list)
    mcp_context: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    token_usage: dict[str, int] = Field(default_factory=dict)
    report_markdown: str = ""
    report_path: str = ""
    error: str = ""


class BenchmarkCase(BaseModel):
    id: str
    contract_text: str
    expected_risk_types: list[str]
    contract_type: str = "general"


class BenchmarkResult(BaseModel):
    cases: int
    risk_recall_at_10: float
    source_coverage: float
    tool_success_rate: float
    memory_recall_at_5: float
    context_retention_rate: float
    hallucination_block_rate: float


class HumanAnnotatedRisk(BaseModel):
    risk_id: str
    clause_id: str
    clause_title: str
    risk_type: str
    risk_level: str
    rationale: str
    expected_suggestion: str
    evidence_source: str
    requires_human_review: bool = False


class HumanBenchmarkContract(BaseModel):
    contract_id: str
    title: str
    contract_type: str
    scenario: str
    file: str
    annotator: str = "legal_reviewer_v1"
    annotations: list[HumanAnnotatedRisk] = Field(default_factory=list)


class HumanBenchmarkResult(BaseModel):
    contracts: int
    annotated_risks: int
    risk_recall_at_10: float
    rule_recall: float
    source_coverage_at_10: float
    high_risk_recall: float
    human_review_capture_rate: float
    evaluated_at: float
