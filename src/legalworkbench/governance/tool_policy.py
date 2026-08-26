"""Mandatory tool-level authorization and durable human approvals."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from legalworkbench.auth import ROLE_PERMISSIONS
from legalworkbench.paths import workspace_dir
from legalworkbench.secure_storage import secure_read_text, secure_write_text


class ToolAccess(str, Enum):
    READ = "read"
    COMPUTE = "compute"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"


@dataclass(frozen=True)
class ToolPolicy:
    """Static policy declared by every registered tool."""

    scope: str
    access: ToolAccess
    required_permission: str = "review:create"
    sensitive_resource: bool = False
    approval_required: bool = False


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str
    approval_id: str = ""
    requires_approval: bool = False


_APPROVAL_LOCKS: dict[str, threading.RLock] = {}
_APPROVAL_LOCKS_GUARD = threading.Lock()


def _approval_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _APPROVAL_LOCKS_GUARD:
        return _APPROVAL_LOCKS.setdefault(key, threading.RLock())


class ToolApprovalStore:
    """Encrypted approval ledger; approved grants are bound and single-use."""

    def __init__(self, cwd: str | Path) -> None:
        self.cwd = Path(cwd).resolve()
        self.path = workspace_dir(self.cwd) / "tool_approvals.json"
        self._lock = _approval_lock(self.path)

    def list(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._load()
            if tenant_id is None:
                return rows
            return [row for row in rows if row.get("tenant_id") == tenant_id]

    def request(
        self,
        *,
        fingerprint: str,
        tenant_id: str,
        user_id: str,
        review_run_id: str,
        tool_name: str,
        scope: str,
        reason: str,
    ) -> dict[str, Any]:
        with self._lock:
            rows = self._load()
            existing = next(
                (
                    row
                    for row in rows
                    if row.get("fingerprint") == fingerprint
                    and row.get("status") == "pending"
                ),
                None,
            )
            if existing is not None:
                return dict(existing)
            now = time.time()
            record = {
                "approval_id": f"approval_{uuid4().hex[:12]}",
                "fingerprint": fingerprint,
                "tenant_id": tenant_id,
                "requested_by": user_id,
                "review_run_id": review_run_id,
                "tool_name": tool_name,
                "scope": scope,
                "reason": reason,
                "status": "pending",
                "requested_at": now,
                "expires_at": now + 3600,
                "approved_by": "",
                "decided_at": 0.0,
                "consumed_at": 0.0,
            }
            rows.append(record)
            self._save(rows)
            return dict(record)

    def decide(
        self,
        approval_id: str,
        *,
        approver: str,
        approve: bool,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            rows = self._load()
            for row in rows:
                if row.get("approval_id") != approval_id:
                    continue
                if tenant_id is not None and row.get("tenant_id") != tenant_id:
                    raise KeyError(approval_id)
                if row.get("status") != "pending":
                    raise ValueError("approval is no longer pending")
                if approve and approver == row.get("requested_by"):
                    raise ValueError("requester cannot approve their own tool request")
                row["status"] = "approved" if approve else "rejected"
                row["approved_by"] = approver
                row["decided_at"] = time.time()
                self._save(rows)
                return dict(row)
        raise KeyError(approval_id)

    def consume(
        self,
        approval_id: str,
        *,
        fingerprint: str,
        tenant_id: str,
        user_id: str,
    ) -> tuple[bool, str]:
        with self._lock:
            rows = self._load()
            for row in rows:
                if row.get("approval_id") != approval_id:
                    continue
                if row.get("tenant_id") != tenant_id:
                    return False, "approval tenant mismatch"
                if row.get("requested_by") != user_id:
                    return False, "approval requester mismatch"
                if row.get("fingerprint") != fingerprint:
                    return False, "approval request fingerprint mismatch"
                if row.get("status") != "approved":
                    return False, f"approval status is {row.get('status') or 'unknown'}"
                if float(row.get("expires_at") or 0) <= time.time():
                    row["status"] = "expired"
                    self._save(rows)
                    return False, "approval expired"
                row["status"] = "consumed"
                row["consumed_at"] = time.time()
                self._save(rows)
                return True, "approved grant consumed"
        return False, "approval not found"

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(secure_read_text(self.path, cwd=self.cwd))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
        return payload if isinstance(payload, list) else []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        secure_write_text(
            self.path,
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
            cwd=self.cwd,
            purpose="tool-approvals",
        )


class ToolPolicyMiddleware:
    """Fail-closed policy gate that every ToolRegistry execution traverses."""

    def evaluate(
        self,
        *,
        tool_name: str,
        policy: ToolPolicy | None,
        arguments: dict[str, Any],
        context: Any,
    ) -> ToolPolicyDecision:
        if policy is None:
            return ToolPolicyDecision(False, "tool has no declared policy")
        tenant_id = str(context.metadata.get("tenant_id") or "local")
        user_id = str(context.metadata.get("user_id") or "local-agent")
        roles = [str(role) for role in (context.metadata.get("roles") or ["admin"])]
        if not _has_permission(roles, policy.required_permission):
            return ToolPolicyDecision(
                False,
                f"permission required: {policy.required_permission}",
            )
        requires_approval = policy.approval_required or policy.access == ToolAccess.EXTERNAL_WRITE
        if policy.sensitive_resource and bool(context.metadata.get("sensitive_resource")):
            requires_approval = policy.access in {ToolAccess.LOCAL_WRITE, ToolAccess.EXTERNAL_WRITE}
        if not requires_approval:
            return ToolPolicyDecision(True, f"allowed {policy.access.value} scope {policy.scope}")

        fingerprint = tool_request_fingerprint(
            tenant_id=tenant_id,
            user_id=user_id,
            review_run_id=context.review_run_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        store = ToolApprovalStore(context.cwd)
        approval_id = str(context.metadata.get("approval_id") or "")
        if approval_id:
            allowed, reason = store.consume(
                approval_id,
                fingerprint=fingerprint,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            return ToolPolicyDecision(
                allowed,
                reason,
                approval_id=approval_id,
                requires_approval=True,
            )
        record = store.request(
            fingerprint=fingerprint,
            tenant_id=tenant_id,
            user_id=user_id,
            review_run_id=context.review_run_id,
            tool_name=tool_name,
            scope=policy.scope,
            reason=f"{policy.access.value} operation requires human approval",
        )
        return ToolPolicyDecision(
            False,
            "human approval required",
            approval_id=str(record["approval_id"]),
            requires_approval=True,
        )


def tool_request_fingerprint(
    *,
    tenant_id: str,
    user_id: str,
    review_run_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    raw = "\0".join((tenant_id, user_id, review_run_id, tool_name, canonical))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_permission(roles: list[str], permission: str) -> bool:
    permissions = {
        item
        for role in roles
        for item in ROLE_PERMISSIONS.get(role, frozenset())
    }
    return "*" in permissions or permission in permissions
