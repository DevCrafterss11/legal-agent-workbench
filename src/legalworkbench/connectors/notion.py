"""Notion connector contract for playbooks and review records."""

from __future__ import annotations

from legalworkbench.connectors.base import ConnectorResource, ConnectorTool


class NotionConnector:
    name = "notion"

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool(self.name, "query_contract_playbook", "查询 Notion 合同审查 playbook"),
            ConnectorTool(self.name, "append_review_record", "写入合同审查结果数据库"),
            ConnectorTool(self.name, "query_vendor_history", "查询供应商历史合同风险"),
        ]

    def resources(self) -> list[ConnectorResource]:
        return [
            ConnectorResource(self.name, "legal_playbook", "mcp://notion/databases/legal_playbook", "法务知识库"),
            ConnectorResource(self.name, "review_records", "mcp://notion/databases/review_records", "历史审查记录"),
        ]
