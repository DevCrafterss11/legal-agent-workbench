"""Authentication, RBAC, and tenant resource-isolation tests."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import legalworkbench.auth as auth_module
from legalworkbench.auth import AuthError, AuthManager
from legalworkbench.memory import LegalMemoryStore
from legalworkbench.governance import ToolApprovalStore
from legalworkbench.models import LegalMemory
from legalworkbench.secrets import save_secrets
from legalworkbench.tasks import ReviewTaskWorker
from legalworkbench.web import create_app


def configure_jwt(tmp_path: Path) -> AuthManager:
    workspace = tmp_path / ".lawbench"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "settings.json").write_text(
        json.dumps(
            {
                "auth": {
                    "mode": "jwt",
                    "issuer": "test-issuer",
                    "audience": "test-api",
                    "clock_skew_seconds": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    save_secrets({"jwt_signing_key": "test-signing-key-" + "x" * 40}, tmp_path)
    return AuthManager(tmp_path)


def bearer(manager: AuthManager, tenant: str, user: str, *roles: str) -> dict[str, str]:
    token = manager.issue_token(
        tenant_id=tenant,
        user_id=user,
        roles=list(roles),
        ttl_seconds=3600,
    )
    return {"Authorization": f"Bearer {token}"}


def test_jwt_signature_claims_and_expiry(tmp_path: Path, monkeypatch) -> None:
    manager = configure_jwt(tmp_path)
    token = manager.issue_token(
        tenant_id="tenant_a", user_id="user_a", roles=["reviewer"], ttl_seconds=60
    )
    principal = manager.verify_token(token)
    assert principal.tenant_id == "tenant_a"
    assert principal.user_id == "user_a"
    assert principal.can("review:create") is True
    assert principal.can("settings:write") is False

    head, payload, signature = token.split(".")
    try:
        manager.verify_token(f"{head}.{payload}.{signature[:-1]}x")
    except AuthError as exc:
        assert "signature" in str(exc)
    else:
        raise AssertionError("tampered JWT must be rejected")

    issued_at = int(auth_module.time.time())
    monkeypatch.setattr(auth_module.time, "time", lambda: issued_at + 61)
    try:
        manager.verify_token(token)
    except AuthError as exc:
        assert "expired" in str(exc)
    else:
        raise AssertionError("expired JWT must be rejected")


def test_web_rbac_and_cross_tenant_resources_are_isolated(tmp_path: Path) -> None:
    manager = configure_jwt(tmp_path)
    admin_a = bearer(manager, "tenant_a", "admin_a", "admin")
    reviewer_a = bearer(manager, "tenant_a", "reviewer_a", "reviewer")
    viewer_a = bearer(manager, "tenant_a", "viewer_a", "viewer")
    reviewer_b = bearer(manager, "tenant_b", "reviewer_b", "reviewer")
    app = create_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 401
        assert client.post("/api/init", headers=admin_a).status_code == 200
        assert client.get("/api/auth/me", headers=viewer_a).json()["tenant_id"] == "tenant_a"
        assert (
            client.post(
                "/api/review", json={"contract_text": "## 条款\n测试"}, headers=viewer_a
            ).status_code
            == 403
        )
        assert client.post("/api/rag-config", json={}, headers=reviewer_a).status_code == 403

        uploaded = client.post(
            "/api/upload",
            json={"filename": "tenant-a.md", "text": "## 责任\n不设责任上限。"},
            headers=reviewer_a,
        ).json()
        assert uploaded["tenant_id"] == "tenant_a"
        assert client.get("/api/state", headers=reviewer_b).json()["documents"] == []
        assert (
            client.post(
                "/api/review-document",
                json={"document_id": uploaded["document_id"]},
                headers=reviewer_b,
            ).status_code
            == 404
        )

        accepted = client.post(
            "/api/review-document",
            json={"document_id": uploaded["document_id"]},
            headers=reviewer_a,
        ).json()
        assert accepted["tenant_id"] == "tenant_a"
        assert client.get(f"/api/tasks/{accepted['task_id']}", headers=reviewer_b).status_code == 404

        result = ReviewTaskWorker(tmp_path).run_once(block_ms=1)
        assert result is not None and result["status"] == "completed"
        run_id = str(result["review_run_id"])
        state_a = client.get("/api/state", headers=reviewer_a).json()
        state_b = client.get("/api/state", headers=reviewer_b).json()
        assert state_a["runs"][0]["review_run_id"] == run_id
        assert state_b["runs"] == []
        assert client.get(f"/api/report/{run_id}", headers=reviewer_b).status_code == 404
        assert client.get(f"/api/report/{run_id}", headers=reviewer_a).status_code == 200


def test_memory_recall_is_tenant_scoped(tmp_path: Path) -> None:
    store = LegalMemoryStore(tmp_path)
    store.save(
        [
            LegalMemory(
                memory_id="mem_a",
                tenant_id="tenant_a",
                type="semantic",
                contract_type="SaaS",
                summary="责任上限审查",
                approved_advice="租户 A 建议",
                status="active",
                confidence=0.9,
            ),
            LegalMemory(
                memory_id="mem_b",
                tenant_id="tenant_b",
                type="semantic",
                contract_type="SaaS",
                summary="责任上限审查",
                approved_advice="租户 B 建议",
                status="active",
                confidence=0.9,
            ),
        ]
    )

    recalled = store.recall(
        "责任上限", contract_type="SaaS", tenant_id="tenant_a", top_k=5
    )
    assert [item.memory_id for item in recalled] == ["mem_a"]


def test_tool_approval_api_requires_operator_and_tenant_scope(tmp_path: Path) -> None:
    manager = configure_jwt(tmp_path)
    reviewer_a = bearer(manager, "tenant_a", "requester_a", "reviewer")
    operator_a = bearer(manager, "tenant_a", "operator_a", "operator")
    operator_b = bearer(manager, "tenant_b", "operator_b", "operator")
    approval = ToolApprovalStore(tmp_path).request(
        fingerprint="fingerprint-a",
        tenant_id="tenant_a",
        user_id="requester_a",
        review_run_id="law_a",
        tool_name="feishu.write_document",
        scope="feishu.document.write",
        reason="external write requires approval",
    )

    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/tool-approvals", headers=reviewer_a).status_code == 403
        assert client.get("/api/tool-approvals", headers=operator_b).json() == {
            "approvals": []
        }
        path = f"/api/tool-approvals/{approval['approval_id']}/approve"
        assert client.post(path, headers=operator_b).status_code == 404
        decided = client.post(path, headers=operator_a)
        assert decided.status_code == 200
        assert decided.json()["status"] == "approved"
        assert decided.json()["approved_by"] == "operator_a"
