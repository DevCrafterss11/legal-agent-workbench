"""Tool abstractions for the legal agent runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from legalworkbench.governance.tool_policy import ToolPolicy, ToolPolicyMiddleware
from legalworkbench.models import ToolCallTrace
from legalworkbench.privacy import mask


@dataclass
class ToolContext:
    """Execution context shared by all legal tools."""

    cwd: Path
    review_run_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Normalized tool execution result."""

    output: Any
    summary: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class LegalTool(Protocol):
    """Protocol implemented by all legal tools."""

    name: str
    description: str
    policy: ToolPolicy

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool."""


class ToolRegistry:
    """Map tool names to legal tool implementations and trace calls."""

    def __init__(self) -> None:
        self._tools: dict[str, LegalTool] = {}
        self._policy = ToolPolicyMiddleware()

    def register(self, tool: LegalTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> LegalTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[LegalTool]:
        return list(self._tools.values())

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult, ToolCallTrace]:
        started = time.time()
        tool = self._tools.get(name)
        if tool is None:
            result = ToolResult(output=None, summary=f"tool not found: {name}", is_error=True)
        else:
            decision = self._policy.evaluate(
                tool_name=name,
                policy=getattr(tool, "policy", None),
                arguments=arguments,
                context=context,
            )
            if not decision.allowed:
                result = ToolResult(
                    output=None,
                    summary=decision.reason,
                    is_error=True,
                    metadata={
                        "policy": "blocked",
                        "approval_id": decision.approval_id,
                        "requires_approval": decision.requires_approval,
                    },
                )
            else:
                try:
                    result = tool.execute(arguments, context)
                    result.metadata = {
                        "policy": "allowed",
                        "policy_reason": decision.reason,
                        "approval_id": decision.approval_id,
                        **result.metadata,
                    }
                except Exception as exc:  # pragma: no cover - defensive boundary
                    result = ToolResult(output=None, summary=str(exc), is_error=True)
        blocked = bool(result.metadata.get("policy") == "blocked")
        trace = ToolCallTrace(
            tool_name=name,
            tenant_id=str(context.metadata.get("tenant_id") or "local"),
            user_id=str(context.metadata.get("user_id") or ""),
            status="blocked" if blocked else ("error" if result.is_error else "success"),
            input_summary=_summarize(arguments),
            output_summary=mask(result.summary).masked_text,
            duration_ms=int((time.time() - started) * 1000),
            metadata={**context.metadata, **result.metadata},
        )
        return result, trace


def _summarize(value: Any) -> str:
    text = mask(str(value)).masked_text
    return text if len(text) <= 180 else text[:177] + "..."
