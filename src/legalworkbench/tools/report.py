"""Report export tool."""

from __future__ import annotations

from typing import Any

from legalworkbench.fs import atomic_write_text
from legalworkbench.privacy import mask
from legalworkbench.report import render_markdown_report
from legalworkbench.tools.base import ToolContext, ToolResult


class ReportExportTool:
    name = "report_export"
    description = "Render and persist a Markdown review report."

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        run = arguments["run"]
        path = arguments["path"]
        run.report_path = str(path)
        run.report_markdown = mask(render_markdown_report(run)).masked_text
        atomic_write_text(path, run.report_markdown)
        return ToolResult(output=str(path), summary=str(path), metadata={"review_run_id": context.review_run_id})
