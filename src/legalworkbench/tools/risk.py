"""Risk rule tool."""

from __future__ import annotations

from typing import Any

from legalworkbench.governance import RiskRuleEngine
from legalworkbench.tools.base import ToolContext, ToolResult


class RiskRuleTool:
    name = "risk_rule"
    description = "Evaluate explicit legal risk rules against clause text."

    def __init__(self) -> None:
        self.engine = RiskRuleEngine()

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        text = str(arguments.get("text") or "")
        hits = self.engine.evaluate(text)
        return ToolResult(output=hits, summary=f"{len(hits)} rule hits")
