"""Tool and data permission policy for legal review workflows."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class PermissionMode(str, Enum):
    REVIEW = "review"
    APPROVAL_REQUIRED = "approval_required"
    FULL_AUTO = "full_auto"


@dataclass(frozen=True)
class GovernanceDecision:
    allowed: bool
    requires_human_review: bool = False
    reason: str = ""


@dataclass(frozen=True)
class PolicyRule:
    pattern: str
    allow: bool
    reason: str = ""


SENSITIVE_CONTRACT_PATTERNS: tuple[str, ...] = (
    "*董事会*",
    "*并购*",
    "*薪酬*",
    "*个人信息*",
    "*secret*",
    "*confidential*",
)


class LegalPermissionChecker:
    """Evaluate tool usage, export actions, and sensitive contract operations."""

    def __init__(self, *, mode: PermissionMode = PermissionMode.REVIEW, path_rules: list[PolicyRule] | None = None) -> None:
        self.mode = mode
        self.path_rules = path_rules or []

    def evaluate_tool(
        self,
        tool_name: str,
        *,
        is_read_only: bool,
        contract_path: str | None = None,
        action: str = "",
    ) -> GovernanceDecision:
        if contract_path:
            decision = self.evaluate_contract_path(contract_path, action=action or tool_name)
            if not decision.allowed:
                return decision
            if decision.requires_human_review and not is_read_only:
                return decision
        if self.mode == PermissionMode.FULL_AUTO:
            return GovernanceDecision(True, reason="full_auto mode")
        if is_read_only:
            return GovernanceDecision(True, reason="read-only legal tool")
        if self.mode == PermissionMode.APPROVAL_REQUIRED:
            return GovernanceDecision(False, True, "mutating legal operation requires approval")
        if tool_name in {"report_export", "mcp_write_report", "mcp_create_approval"}:
            return GovernanceDecision(True, True, "external write or export requires review trace")
        return GovernanceDecision(True, reason="review mode allowed")

    def evaluate_contract_path(self, contract_path: str, *, action: str) -> GovernanceDecision:
        normalized = str(Path(contract_path).expanduser())
        for rule in self.path_rules:
            if fnmatch.fnmatch(normalized, rule.pattern):
                return GovernanceDecision(rule.allow, not rule.allow, rule.reason or f"path rule matched {rule.pattern}")
        lowered = normalized.lower()
        for pattern in SENSITIVE_CONTRACT_PATTERNS:
            if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(lowered, pattern.lower()):
                if action in {"external_export", "mcp_write_report", "mcp_create_approval"}:
                    return GovernanceDecision(False, True, f"sensitive contract cannot perform {action} without approval")
                return GovernanceDecision(True, True, "sensitive contract requires human review")
        return GovernanceDecision(True, reason="contract path allowed")
