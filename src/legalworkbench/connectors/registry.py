"""Enterprise connector discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from legalworkbench.connectors.feishu import FeishuConnector
from legalworkbench.connectors.notion import NotionConnector
from legalworkbench.mcp import McpConnectorRegistry


class EnterpriseConnectorRegistry:
    """Combine built-in connector contracts with configured MCP servers."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.builtins = [FeishuConnector(), NotionConnector()]

    def context(self, *, connect_mcp: bool = False) -> dict[str, Any]:
        mcp = McpConnectorRegistry(self.cwd).context(connect=connect_mcp)
        builtin_tools = [tool.__dict__ for connector in self.builtins for tool in connector.tools()]
        builtin_resources = [resource.__dict__ for connector in self.builtins for resource in connector.resources()]
        return {
            **mcp,
            "builtin_tools": builtin_tools,
            "builtin_resources": builtin_resources,
            "enterprise_actions": [
                "read_contract_from_feishu",
                "query_playbook_from_notion",
                "write_report_back",
                "create_human_approval",
                "append_audit_log",
            ],
        }
