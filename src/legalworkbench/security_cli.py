"""Security lifecycle commands kept separate from the main CLI surface."""

from __future__ import annotations

import base64
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

import typer

from legalworkbench.auth import AuthError, AuthManager, ROLE_PERMISSIONS
from legalworkbench.fs import atomic_write_text
from legalworkbench.governance import ToolApprovalStore
from legalworkbench.paths import settings_path
from legalworkbench.privacy_migration import migrate_private_storage
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.secure_storage import default_keychain_service
from legalworkbench.secrets import load_secrets, save_secrets


def register_security_commands(app: typer.Typer) -> None:
    app.command("encryption-init")(encryption_init_cmd)
    app.command("privacy-migrate")(privacy_migrate_cmd)
    app.command("auth-config")(auth_config_cmd)
    app.command("auth-token")(auth_token_cmd)
    app.command("tool-approvals")(tool_approvals_cmd)
    app.command("tool-approve")(tool_approve_cmd)
    app.command("tool-reject")(tool_reject_cmd)


def encryption_init_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    provider: str = typer.Option(
        "macos-keychain", "--provider", help="macos-keychain or aws-kms"
    ),
    aws_kms_key_id: str = typer.Option("", "--aws-kms-key-id", help="AWS KMS key ARN/ID"),
    aws_region: str = typer.Option("", "--aws-region", help="AWS region"),
) -> None:
    """Configure external key management and migrate sensitive local files."""

    root = Path(cwd).resolve()
    LegalAgentRuntime(root).init_samples()
    current = _load_settings(root)
    if provider == "macos-keychain":
        if sys.platform != "darwin":
            raise typer.BadParameter("macos-keychain is only available on macOS")
        account = getpass.getuser()
        service = default_keychain_service(root)
        find = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if find.returncode != 0:
            encoded = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-a",
                    account,
                    "-s",
                    service,
                    "-w",
                    encoded,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        current["encryption"] = {
            "provider": provider,
            "keychain_service": service,
            "keychain_account": account,
        }
    elif provider == "aws-kms":
        if not aws_kms_key_id:
            raise typer.BadParameter("--aws-kms-key-id is required for aws-kms")
        current["encryption"] = {
            "provider": provider,
            "aws_kms_key_id": aws_kms_key_id,
            "aws_region": aws_region,
        }
    else:
        raise typer.BadParameter("--provider must be macos-keychain or aws-kms")
    atomic_write_text(
        settings_path(root), json.dumps(current, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(migrate_private_storage(root), ensure_ascii=False, indent=2))


def privacy_migrate_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
) -> None:
    """Re-run the idempotent masking/encryption migration."""

    print(json.dumps(migrate_private_storage(cwd), ensure_ascii=False, indent=2))


def auth_config_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    mode: str = typer.Option("jwt", "--mode", help="local or jwt"),
    issuer: str = typer.Option("legal-agent-workbench", "--issuer"),
    audience: str = typer.Option("legal-agent-api", "--audience"),
) -> None:
    """Enable local development auth or signed JWT authentication."""

    if mode not in {"local", "jwt"}:
        raise typer.BadParameter("--mode must be local or jwt")
    root = Path(cwd).resolve()
    current = _load_settings(root)
    current["auth"] = {
        "mode": mode,
        "issuer": issuer,
        "audience": audience,
        "local_tenant_id": "local",
        "local_user_id": "local-admin",
        "local_roles": ["admin"],
        "clock_skew_seconds": 30,
    }
    atomic_write_text(
        settings_path(root), json.dumps(current, ensure_ascii=False, indent=2) + "\n"
    )
    if mode == "jwt":
        secrets = load_secrets(root)
        if not secrets.get("jwt_signing_key"):
            secrets["jwt_signing_key"] = base64.urlsafe_b64encode(
                os.urandom(48)
            ).decode("ascii")
            save_secrets(secrets, root)
    print(f"Authentication mode: {mode}")


def auth_token_cmd(
    tenant_id: str = typer.Option(..., "--tenant", help="Tenant identifier"),
    user_id: str = typer.Option(..., "--user", help="User identifier"),
    roles: str = typer.Option("reviewer", "--roles", help="Comma-separated roles"),
    ttl_seconds: int = typer.Option(3600, "--ttl", min=60, help="Token lifetime"),
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
) -> None:
    """Issue a signed access token for a configured workbench identity."""

    selected = [item.strip() for item in roles.split(",") if item.strip()]
    invalid = [item for item in selected if item not in ROLE_PERMISSIONS]
    if invalid:
        raise typer.BadParameter(f"unknown roles: {', '.join(invalid)}")
    try:
        token = AuthManager(cwd).issue_token(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=selected,
            ttl_seconds=ttl_seconds,
        )
    except AuthError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print(token)


def tool_approvals_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    tenant_id: str = typer.Option("", "--tenant", help="Optional tenant filter"),
) -> None:
    """List durable tool approval requests."""

    rows = ToolApprovalStore(cwd).list(tenant_id=tenant_id or None)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def tool_approve_cmd(
    approval_id: str = typer.Argument(...),
    approver: str = typer.Option(..., "--approver"),
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    tenant_id: str = typer.Option("", "--tenant", help="Expected tenant"),
) -> None:
    """Approve one bound, single-use tool request."""

    try:
        record = ToolApprovalStore(cwd).decide(
            approval_id,
            approver=approver,
            approve=True,
            tenant_id=tenant_id or None,
        )
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print(json.dumps(record, ensure_ascii=False, indent=2))


def tool_reject_cmd(
    approval_id: str = typer.Argument(...),
    approver: str = typer.Option(..., "--approver"),
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    tenant_id: str = typer.Option("", "--tenant", help="Expected tenant"),
) -> None:
    """Reject one pending tool request."""

    try:
        record = ToolApprovalStore(cwd).decide(
            approval_id,
            approver=approver,
            approve=False,
            tenant_id=tenant_id or None,
        )
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print(json.dumps(record, ensure_ascii=False, indent=2))


def _load_settings(cwd: Path) -> dict:
    path = settings_path(cwd)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}
