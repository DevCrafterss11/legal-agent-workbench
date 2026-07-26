"""Feishu connector contract for docs, approvals, and audit logs."""

from __future__ import annotations

from legalworkbench.connectors.base import ConnectorResource, ConnectorTool


class FeishuConnector:
    name = "feishu"

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool(self.name, "read_feishu_doc", "读取飞书文档中的合同正文"),
            ConnectorTool(self.name, "write_review_report", "将审查报告写回飞书文档"),
            ConnectorTool(self.name, "create_approval_task", "为高风险条款创建法务复核任务"),
            ConnectorTool(self.name, "append_audit_log", "写入企业审计日志"),
        ]

    def resources(self) -> list[ConnectorResource]:
        return [
            ConnectorResource(self.name, "contract_docs", "mcp://feishu/docs/contracts", "合同文档库"),
            ConnectorResource(self.name, "approval_tasks", "mcp://feishu/approval/tasks", "审批任务列表"),
        ]
