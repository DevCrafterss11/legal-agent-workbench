"""Tool registry and legal review tool implementations."""

from legalworkbench.tools.base import ToolContext, ToolResult, LegalTool, ToolRegistry
from legalworkbench.tools.contract import ContractParserTool
from legalworkbench.tools.retrieval import ClauseRetrieverTool
from legalworkbench.tools.risk import RiskRuleTool
from legalworkbench.tools.governance import PermissionGuardTool
from legalworkbench.tools.report import ReportExportTool

__all__ = [
    "ToolContext",
    "ToolResult",
    "LegalTool",
    "ToolRegistry",
    "ContractParserTool",
    "ClauseRetrieverTool",
    "RiskRuleTool",
    "PermissionGuardTool",
    "ReportExportTool",
]
"""Registered tools for the legal agent runtime."""

from legalworkbench.tools.base import ToolRegistry
from legalworkbench.tools.contract import ContractParserTool
from legalworkbench.tools.governance import PermissionGuardTool
from legalworkbench.tools.report import ReportExportTool
from legalworkbench.tools.retrieval import ClauseRetrieverTool
from legalworkbench.tools.rewrite import ClauseRewriterTool
from legalworkbench.tools.risk import RiskRuleTool


def build_default_tool_registry() -> ToolRegistry:
    """Create the default set of contract review tools."""

    registry = ToolRegistry()
    registry.register(ContractParserTool())
    registry.register(ClauseRetrieverTool())
    registry.register(RiskRuleTool())
    registry.register(ClauseRewriterTool())
    registry.register(PermissionGuardTool())
    registry.register(ReportExportTool())
    return registry


__all__ = [
    "build_default_tool_registry",
    "ClauseRetrieverTool",
    "ClauseRewriterTool",
    "ContractParserTool",
    "PermissionGuardTool",
    "ReportExportTool",
    "RiskRuleTool",
    "ToolRegistry",
]
