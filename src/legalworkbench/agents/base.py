"""Base classes and shared state for legal review agents."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from legalworkbench.compact import LegalContextCompactor
from legalworkbench.connectors import EnterpriseConnectorRegistry
from legalworkbench.hooks import HookEvent, HookEventBus
from legalworkbench.llm import LlmClient
from legalworkbench.memory import LegalMemoryStore
from legalworkbench.models import LegalMemory, ReviewRun
from legalworkbench.reflection import ReflectionAuditor
from legalworkbench.skills import SkillCatalog
from legalworkbench.storage import ReviewSessionStore
from legalworkbench.store import WorkbenchStore
from legalworkbench.tools.base import ToolContext, ToolRegistry, ToolResult


class AgentExecutionError(RuntimeError):
    """Raised when an agent cannot complete its assigned stage."""


@dataclass
class ReviewAgentContext:
    """Shared state passed between Supervisor and worker agents."""

    cwd: Path
    run: ReviewRun
    contract_text: str
    tools: ToolRegistry
    hooks: HookEventBus
    skills: SkillCatalog
    llm: LlmClient
    reflection: ReflectionAuditor
    compactor: LegalContextCompactor
    connectors: EnterpriseConnectorRegistry
    memory_store: LegalMemoryStore
    sessions: ReviewSessionStore
    store: WorkbenchStore
    connect_mcp: bool = False
    started_at: float = field(default_factory=time.time)
    evidence_total: int = 0
    memory_hits: dict[str, LegalMemory] = field(default_factory=dict)

    def record_agent_step(self, agent_name: str, action: str, payload: dict[str, Any] | None = None) -> None:
        steps = self.run.mcp_context.setdefault("agent_steps", [])
        if isinstance(steps, list):
            steps.append(
                {
                    "agent": agent_name,
                    "action": action,
                    "time": round(time.time(), 3),
                    "payload": payload or {},
                }
            )


class LegalReviewAgent:
    """Base worker agent with structured tool tracing."""

    name = "base_agent"
    role = "base"

    def emit(self, ctx: ReviewAgentContext, event: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        ctx.record_agent_step(self.name, event, payload)
        ctx.hooks.emit(
            HookEvent(
                name=f"agent.{event}",
                review_run_id=ctx.run.review_run_id,
                payload={"agent": self.name, "role": self.role, **payload},
            )
        )

    def execute_tool(
        self,
        ctx: ReviewAgentContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        tool_context = ToolContext(
            ctx.cwd,
            ctx.run.review_run_id,
            metadata={"agent": self.name, "agent_role": self.role},
        )
        result, trace = ctx.tools.execute(tool_name, arguments, tool_context)
        ctx.run.tool_calls.append(trace)
        ctx.hooks.emit(HookEvent("tool.called", ctx.run.review_run_id, trace.model_dump(mode="json")))
        if result.is_error:
            self.emit(ctx, "tool_error", {"tool": tool_name, "summary": result.summary})
        else:
            self.emit(ctx, "tool_success", {"tool": tool_name, "summary": result.summary})
        return result
