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

from legalworkbench.fs import atomic_write_text
from legalworkbench.paths import settings_path
from legalworkbench.privacy_migration import migrate_private_storage
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.secure_storage import default_keychain_service


def register_security_commands(app: typer.Typer) -> None:
    app.command("encryption-init")(encryption_init_cmd)
    app.command("privacy-migrate")(privacy_migrate_cmd)


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


def _load_settings(cwd: Path) -> dict:
    path = settings_path(cwd)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}
