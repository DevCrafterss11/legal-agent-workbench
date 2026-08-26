from __future__ import annotations

from pathlib import Path

import pytest

from legalworkbench.governance import ToolAccess, ToolApprovalStore, ToolPolicy
from legalworkbench.tools import ToolContext, ToolRegistry, ToolResult


class ReadTool:
    name = "knowledge.search"
    description = "read knowledge"
    policy = ToolPolicy("knowledge.read", ToolAccess.READ)

    def execute(self, arguments, context):
        del context
        return ToolResult(output=arguments, summary="read")


class ExternalWriteTool:
    name = "feishu.write_document"
    description = "write an external document"
    policy = ToolPolicy(
        "feishu.document.write",
        ToolAccess.EXTERNAL_WRITE,
        required_permission="review:create",
    )

    def execute(self, arguments, context):
        del context
        return ToolResult(output=arguments, summary="written")


def context(tmp_path: Path, *, roles: list[str], approval_id: str = "") -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        review_run_id="law_policy",
        metadata={
            "tenant_id": "tenant-a",
            "user_id": "reviewer-a",
            "roles": roles,
            "approval_id": approval_id,
        },
    )


def test_registry_fails_closed_when_tool_has_no_policy(tmp_path: Path) -> None:
    class UndeclaredTool:
        name = "undeclared"
        description = "missing policy"

        def execute(self, arguments, context):
            raise AssertionError("policy gate must run before the tool")

    registry = ToolRegistry()
    registry.register(UndeclaredTool())  # type: ignore[arg-type]
    result, trace = registry.execute(
        "undeclared", {}, context(tmp_path, roles=["admin"])
    )

    assert result.is_error is True
    assert trace.status == "blocked"
    assert "no declared policy" in result.summary


def test_registry_enforces_role_permission_before_execution(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ReadTool())

    denied, denied_trace = registry.execute(
        "knowledge.search", {}, context(tmp_path, roles=["viewer"])
    )
    allowed, allowed_trace = registry.execute(
        "knowledge.search", {}, context(tmp_path, roles=["reviewer"])
    )

    assert denied.is_error is True
    assert denied_trace.status == "blocked"
    assert allowed.is_error is False
    assert allowed_trace.status == "success"
    assert allowed_trace.metadata["policy"] == "allowed"


def test_external_write_requires_bound_single_use_human_approval(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ExternalWriteTool())
    arguments = {"document_id": "doc-1", "content": "approved version"}

    blocked, trace = registry.execute(
        "feishu.write_document",
        arguments,
        context(tmp_path, roles=["reviewer"]),
    )
    approval_id = str(blocked.metadata["approval_id"])
    assert trace.status == "blocked"
    assert blocked.metadata["requires_approval"] is True
    assert approval_id.startswith("approval_")

    approvals = ToolApprovalStore(tmp_path)
    with pytest.raises(ValueError, match="cannot approve"):
        approvals.decide(approval_id, approver="reviewer-a", approve=True)
    with pytest.raises(KeyError):
        approvals.decide(
            approval_id,
            approver="legal-admin",
            approve=True,
            tenant_id="tenant-b",
        )
    approvals.decide(
        approval_id,
        approver="legal-admin",
        approve=True,
        tenant_id="tenant-a",
    )

    allowed, allowed_trace = registry.execute(
        "feishu.write_document",
        arguments,
        context(tmp_path, roles=["reviewer"], approval_id=approval_id),
    )
    assert allowed.is_error is False
    assert allowed_trace.status == "success"
    assert allowed_trace.metadata["approval_id"] == approval_id

    replay, replay_trace = registry.execute(
        "feishu.write_document",
        arguments,
        context(tmp_path, roles=["reviewer"], approval_id=approval_id),
    )
    assert replay.is_error is True
    assert replay_trace.status == "blocked"
    assert "consumed" in replay.summary


def test_approval_cannot_authorize_changed_arguments(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ExternalWriteTool())
    original = {"document_id": "doc-1", "content": "v1"}
    blocked, _ = registry.execute(
        "feishu.write_document", original, context(tmp_path, roles=["reviewer"])
    )
    approval_id = str(blocked.metadata["approval_id"])
    ToolApprovalStore(tmp_path).decide(
        approval_id, approver="legal-admin", approve=True
    )

    changed, trace = registry.execute(
        "feishu.write_document",
        {"document_id": "doc-1", "content": "v2"},
        context(tmp_path, roles=["reviewer"], approval_id=approval_id),
    )
    assert changed.is_error is True
    assert trace.status == "blocked"
    assert "fingerprint mismatch" in changed.summary
