"""Permission and governance tool."""

from __future__ import annotations

from typing import Any

from legalworkbench.governance import PermissionGuard, ToolAccess, ToolPolicy
from legalworkbench.tools.base import ToolContext, ToolResult


class PermissionGuardTool:
    name = "permission_guard"
    description = "Block unsupported legal conclusions and flag high-risk findings for review."
    policy = ToolPolicy("governance.check", ToolAccess.COMPUTE)

    def __init__(self) -> None:
        self.guard = PermissionGuard()

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        ok, reason = self.guard.check(
            risk_level=str(arguments.get("risk_level") or "medium"),
            evidence_count=int(arguments.get("evidence_count") or 0),
            suggestion=str(arguments.get("suggestion") or ""),
            requires_human_review=bool(arguments.get("requires_human_review")),
        )
        return ToolResult(output={"ok": ok, "reason": reason}, summary=reason or "allowed")
