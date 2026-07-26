"""Legal Review Supervisor: deterministic orchestration for worker agents."""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

from legalworkbench.agents.base import AgentExecutionError, LegalReviewAgent, ReviewAgentContext
from legalworkbench.agents.clause_rewriter import ClauseRewriterAgent
from legalworkbench.agents.compliance_auditor import ComplianceAuditorAgent
from legalworkbench.agents.evidence import EvidenceAgent
from legalworkbench.agents.memory_curator import MemoryCuratorAgent
from legalworkbench.agents.parser import ParserAgent
from legalworkbench.agents.report_writer import ReportWriterAgent
from legalworkbench.agents.risk_reviewer import RiskReviewerAgent
from legalworkbench.agents.skill_planner import SkillPlannerAgent
from legalworkbench.compact import LegalContextCompactor
from legalworkbench.connectors import EnterpriseConnectorRegistry
from legalworkbench.fs import atomic_write_text
from legalworkbench.hooks import HookEvent, HookEventBus
from legalworkbench.llm import LlmClient
from legalworkbench.memory import LegalMemoryStore
from legalworkbench.models import ReviewRun
from legalworkbench.paths import workspace_dir
from legalworkbench.reflection import ReflectionAuditor
from legalworkbench.report import render_dashboard_html
from legalworkbench.skills import SkillCatalog
from legalworkbench.storage import ReviewSessionStore
from legalworkbench.store import WorkbenchStore
from legalworkbench.tools.base import ToolRegistry
from legalworkbench.workflow import LegalReviewWorkflow


class LegalReviewSupervisor(LegalReviewAgent):
    """Main agent that coordinates worker agents through ReviewRun state."""

    name = "legal_review_supervisor"
    role = "orchestration"

    def __init__(
        self,
        *,
        cwd: Path,
        store: WorkbenchStore,
        skills: SkillCatalog,
        memory: LegalMemoryStore,
        sessions: ReviewSessionStore,
        hooks: HookEventBus,
        connectors: EnterpriseConnectorRegistry,
        workflow: LegalReviewWorkflow,
        reflection: ReflectionAuditor,
        compactor: LegalContextCompactor,
        llm: LlmClient,
        tools: ToolRegistry,
    ) -> None:
        self.cwd = cwd
        self.store = store
        self.skills = skills
        self.memory = memory
        self.sessions = sessions
        self.hooks = hooks
        self.connectors = connectors
        self.workflow = workflow
        self.reflection = reflection
        self.compactor = compactor
        self.llm = llm
        self.tools = tools
        self.parser = ParserAgent()
        self.skill_planner = SkillPlannerAgent()
        self.evidence_agent = EvidenceAgent()
        self.risk_reviewer = RiskReviewerAgent()
        self.rewriter = ClauseRewriterAgent()
        self.auditor = ComplianceAuditorAgent()
        self.report_writer = ReportWriterAgent()
        self.memory_curator = MemoryCuratorAgent()

    def review(self, contract_path: Path, *, contract_text: str, connect_mcp: bool = False) -> ReviewRun:
        now = time.time()
        run = ReviewRun(
            review_run_id=f"law_{uuid4().hex[:10]}",
            contract_path=str(contract_path),
            status="created",
            created_at=now,
            updated_at=now,
            mcp_context={
                "workflow": self.workflow.describe(),
                "agent_architecture": self.architecture(),
            },
        )
        ctx = ReviewAgentContext(
            cwd=self.cwd,
            run=run,
            contract_text=contract_text,
            tools=self.tools,
            hooks=self.hooks,
            skills=self.skills,
            llm=self.llm,
            reflection=self.reflection,
            compactor=self.compactor,
            connectors=self.connectors,
            memory_store=self.memory,
            sessions=self.sessions,
            store=self.store,
            connect_mcp=connect_mcp,
            started_at=now,
        )

        self.sessions.save_snapshot(run, event="created")
        self.hooks.emit(HookEvent("review.created", run.review_run_id, {"contract_path": str(contract_path)}))
        self.emit(ctx, "started", {"contract_path": str(contract_path)})

        try:
            self.parser.run(ctx)
            # 隐私扫描：PII 统计进 trace，敏感合同全程携带标记；
            # 明文只存在于本地信任边界，远端 LLM 与飞书回发链路各自做出/入境脱敏
            from legalworkbench.governance import scan_injection
            from legalworkbench.privacy import scan as scan_pii

            pii_counts = scan_pii(contract_text)
            run.mcp_context["privacy"] = {"pii_counts": pii_counts, "sensitive": bool(pii_counts)}
            if pii_counts:
                self.hooks.emit(HookEvent("privacy.pii_detected", run.review_run_id, {"counts": pii_counts}))
            # 注入检测：合同是不可信输入，命中指令注入模式 -> 打标 + 审计事件 +
            # 本次审查全部结论强制人工复核（宁可保守，不让被污染的结论静默通过）
            injection_hits = scan_injection(contract_text)
            run.mcp_context["injection"] = {
                "detected": bool(injection_hits),
                "hits": [{"pattern": hit.pattern_id, "snippet": hit.snippet[:80]} for hit in injection_hits],
            }
            if injection_hits:
                self.hooks.emit(
                    HookEvent(
                        "security.injection_detected",
                        run.review_run_id,
                        {"patterns": [hit.pattern_id for hit in injection_hits]},
                    )
                )
            skill_profile = self.skill_planner.run(ctx)
            skill_risk_focus = set(skill_profile.get("risk_focus") or [])
            retrieval_top_k = int(skill_profile.get("retrieval_top_k") or 10)

            findings = []
            for clause in run.clauses:
                bundle = self.evidence_agent.retrieve_clause(ctx, clause, top_k=retrieval_top_k)
                if bundle is None:
                    continue
                drafts = self.risk_reviewer.draft_findings(ctx, bundle, skill_risk_focus=skill_risk_focus)
                for draft in drafts:
                    suggestion = self.rewriter.rewrite(ctx, draft)
                    finding = self.auditor.approve_finding(
                        ctx,
                        draft,
                        suggestion=suggestion,
                        finding_id=f"F{len(findings) + 1:03d}",
                    )
                    if injection_hits:
                        finding.requires_human_review = True
                    findings.append(finding)
                    self.hooks.emit(
                        HookEvent(
                            "risk.detected",
                            run.review_run_id,
                            {
                                "finding_id": finding.finding_id,
                                "risk_type": finding.risk_type,
                                "risk_level": finding.risk_level,
                            },
                        )
                    )

            run.memory_hits = list(ctx.memory_hits.values())[:10]
            run.findings = findings
            self.auditor.reflect(ctx)
            self.report_writer.finalize_context(ctx)
            self.memory_curator.consolidate(ctx)
            self.report_writer.write_report(ctx)
            run.updated_at = time.time()
            self.store.save_run(run)
            self.sessions.save_snapshot(run, event="completed", metadata={"report_path": run.report_path})
            self.hooks.emit(
                HookEvent("review.completed", run.review_run_id, {"status": run.status, "findings": len(run.findings)})
            )
            self.export_dashboard()
            self.emit(ctx, "completed", {"status": run.status, "findings": len(run.findings)})
            return run
        except AgentExecutionError as exc:
            return self.fail(ctx, str(exc))

    def fail(self, ctx: ReviewAgentContext, error: str) -> ReviewRun:
        ctx.run.status = "failed"
        ctx.run.error = error
        ctx.run.updated_at = time.time()
        self.store.save_run(ctx.run)
        self.sessions.save_snapshot(ctx.run, event="failed", metadata={"error": error})
        self.hooks.emit(HookEvent("review.failed", ctx.run.review_run_id, {"error": error}))
        self.emit(ctx, "failed", {"error": error})
        return ctx.run

    def export_dashboard(self, output: str | Path | None = None) -> Path:
        runs = self.store.list_runs(limit=50)
        payload = {
            "generated_at": time.time(),
            "workflow": self.workflow.describe(),
            "sessions": self.sessions.list_sessions(limit=20),
            "runs": [run.model_dump(mode="json") for run in runs],
        }
        path = Path(output).resolve() if output else workspace_dir(self.cwd) / "dashboard.json"
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        atomic_write_text(path.with_suffix(".html"), render_dashboard_html(runs))
        return path

    def architecture(self) -> dict[str, object]:
        return {
            "pattern": "supervisor_worker",
            "communication": "ReviewRun shared state + structured agent steps + ToolCallTrace",
            "supervisor": self.name,
            "workers": [
                self.parser.name,
                self.skill_planner.name,
                self.evidence_agent.name,
                self.risk_reviewer.name,
                self.rewriter.name,
                self.auditor.name,
                self.report_writer.name,
                self.memory_curator.name,
            ],
            "rag_role": "RAG is a retrieval capability owned by evidence_agent, not an autonomous decision agent.",
            "memory_layers": {
                "working": "ReviewRun 共享状态：当前条款、证据包、决策来源，生命周期为单次审查（context 内工作记忆）",
                "short_term": "ReviewSession 阶段快照 + CompactSnapshot 长合同压缩：会话级，可回溯可恢复",
                "long_term": "LegalMemoryStore 跨会话记忆：写入门槛/冲突强化/使用反馈/时间衰减/容量驱逐",
            },
        }
