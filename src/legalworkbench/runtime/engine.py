"""Legal Agent Runtime facade.

The runtime owns durable services and exposes the public API. Actual review
orchestration is delegated to the LegalReviewSupervisor so the codebase shows
the intended supervisor-worker agent architecture directly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from legalworkbench.agents import LegalReviewSupervisor
from legalworkbench.agents.risk_reviewer import (
    evidence_implies_risk,
    skill_implies_risk,
)
from legalworkbench.cache import create_cache
from legalworkbench.compact import LegalContextCompactor
from legalworkbench.connectors import EnterpriseConnectorRegistry
from legalworkbench.context import ContextManager
from legalworkbench.evals import BenchmarkRunner, HumanBenchmarkRunner
from legalworkbench.fs import atomic_write_text
from legalworkbench.governance import LegalPermissionChecker, PermissionMode
from legalworkbench.hooks import HookEventBus
from legalworkbench.llm import LlmClient
from legalworkbench.memory import LegalMemoryStore
from legalworkbench.models import ReviewRun
from legalworkbench.paths import workspace_dir
from legalworkbench.privacy import mask_value
from legalworkbench.reflection import ReflectionAuditor
from legalworkbench.report import render_dashboard_html
from legalworkbench.secure_storage import secure_read_text
from legalworkbench.skills import SkillCatalog
from legalworkbench.storage import ReviewSessionStore
from legalworkbench.store import WorkbenchStore
from legalworkbench.tools import build_default_tool_registry
from legalworkbench.workflow import LegalReviewWorkflow


class LegalAgentRuntime:
    """Public runtime facade for contract review, evals, and dashboard export."""

    def __init__(
        self,
        cwd: str | Path | None = None,
        *,
        permission_mode: PermissionMode = PermissionMode.REVIEW,
        memory_enabled: bool = True,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.store = WorkbenchStore(self.cwd)
        self.skills = SkillCatalog(self.cwd)
        self.memory = LegalMemoryStore(self.cwd)
        self.sessions = ReviewSessionStore(self.cwd)
        self.hooks = HookEventBus(self.cwd)
        self.connectors = EnterpriseConnectorRegistry(self.cwd)
        self.workflow = LegalReviewWorkflow()
        self.reflection = ReflectionAuditor()
        self.compactor = LegalContextCompactor()
        self.context_manager = ContextManager()
        self.cache = create_cache(self.cwd)
        self.llm = LlmClient(cwd=self.cwd, cache=self.cache)
        self.permission = LegalPermissionChecker(mode=permission_mode)
        self.tools = build_default_tool_registry()
        self.memory_enabled = memory_enabled

    def init_samples(self, *, force: bool = False) -> dict[str, Path]:
        paths = self.store.init_samples(force=force)
        self.memory.write_index()
        return paths

    def review(
        self,
        contract_path: str | Path,
        *,
        connect_mcp: bool = False,
        tenant_id: str = "local",
        user_id: str = "",
        roles: list[str] | None = None,
        memory_enabled: bool | None = None,
    ) -> ReviewRun:
        path = self._resolve_contract_path(contract_path)
        permission = self.permission.evaluate_tool("contract_parser", is_read_only=True, contract_path=str(path))
        if not permission.allowed:
            raise PermissionError(permission.reason)
        return self._supervisor().review(
            path,
            contract_text=secure_read_text(path, cwd=self.cwd),
            connect_mcp=connect_mcp,
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles or ["admin"],
            memory_enabled=self.memory_enabled if memory_enabled is None else memory_enabled,
        )

    def benchmark(self):
        return BenchmarkRunner(self.cwd).run()

    def human_benchmark(self):
        return HumanBenchmarkRunner(self.cwd).run()

    def export_dashboard(self, output: str | Path | None = None) -> Path:
        runs = self.store.list_runs(limit=50)
        payload = {
            "generated_at": time.time(),
            "workflow": self.workflow.describe(),
            "sessions": self.sessions.list_sessions(limit=20),
            "runs": [run.model_dump(mode="json") for run in runs],
        }
        path = Path(output).resolve() if output else workspace_dir(self.cwd) / "dashboard.json"
        atomic_write_text(
            path,
            json.dumps(mask_value(payload), ensure_ascii=False, indent=2) + "\n",
        )
        atomic_write_text(path.with_suffix(".html"), render_dashboard_html(runs))
        return path

    def _resolve_contract_path(self, contract_path: str | Path) -> Path:
        path = Path(contract_path).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        return path.resolve()

    def _supervisor(self) -> LegalReviewSupervisor:
        return LegalReviewSupervisor(
            cwd=self.cwd,
            store=self.store,
            skills=self.skills,
            memory=self.memory,
            sessions=self.sessions,
            hooks=self.hooks,
            connectors=self.connectors,
            workflow=self.workflow,
            reflection=self.reflection,
            compactor=self.compactor,
            context_manager=self.context_manager,
            llm=self.llm,
            tools=self.tools,
        )


__all__ = ["LegalAgentRuntime", "evidence_implies_risk", "skill_implies_risk"]
