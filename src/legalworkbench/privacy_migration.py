"""Idempotent migration of legacy plaintext and derived persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from legalworkbench.fs import atomic_write_text
from legalworkbench.paths import workspace_dir
from legalworkbench.privacy import mask
from legalworkbench.secure_storage import (
    EncryptionConfigurationError,
    is_encrypted,
    load_encryption_config,
    secure_write_bytes,
)

_DERIVED_ROOT_FILES = {
    "dashboard.json",
    "dashboard.html",
    "events.jsonl",
    "memory.json",
    "memory_archive.jsonl",
    "memory_export.jsonl",
    "MEMORY.md",
    "server.log",
    "feishu-listen.log",
}


def migrate_private_storage(cwd: str | Path | None = None) -> dict[str, Any]:
    """Mask derived records, encrypt source material, and normalize permissions."""

    root = workspace_dir(cwd)
    config = load_encryption_config(cwd)
    if not config.enabled:
        raise EncryptionConfigurationError(
            "configure an encryption provider before running the migration"
        )

    masked_files = 0
    pii_counts: dict[str, int] = {}
    for path in _derived_candidates(root):
        try:
            raw = path.read_bytes()
            if is_encrypted(raw):
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        masked = text
        file_counts: dict[str, int] = {}
        for _ in range(4):
            result = mask(masked)
            if result.masked_text == masked:
                break
            masked = result.masked_text
            for pii_type, count in result.counts.items():
                file_counts[pii_type] = file_counts.get(pii_type, 0) + count
        if masked == text:
            continue
        atomic_write_text(path, masked)
        masked_files += 1
        for pii_type, count in file_counts.items():
            pii_counts[pii_type] = pii_counts.get(pii_type, 0) + count

    encrypted_files = 0
    for path, purpose in _source_candidates(root):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if is_encrypted(raw):
            continue
        secure_write_bytes(path, raw, cwd=cwd, purpose=purpose)
        encrypted_files += 1

    directories = 0
    files = 0
    for path in [root, *root.rglob("*")]:
        try:
            if path.is_dir():
                path.chmod(0o700)
                directories += 1
            elif path.is_file():
                path.chmod(0o600)
                files += 1
        except OSError:
            continue
    return {
        "provider": config.provider,
        "masked_files": masked_files,
        "pii_counts": pii_counts,
        "encrypted_files": encrypted_files,
        "private_directories": directories,
        "private_files": files,
    }


def _derived_candidates(root: Path) -> list[Path]:
    paths: set[Path] = set()
    for directory in (root / "runs", root / "sessions"):
        if directory.exists():
            paths.update(path for path in directory.rglob("*") if path.is_file())
    paths.update(root / name for name in _DERIVED_ROOT_FILES if (root / name).is_file())
    return sorted(paths)


def _source_candidates(root: Path) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    uploads = root / "uploads"
    if uploads.exists():
        for path in uploads.rglob("*"):
            if not path.is_file():
                continue
            if path.name == "documents.json":
                purpose = "uploaded-contract-index"
            elif path.name.startswith("raw_"):
                purpose = "uploaded-contract-original"
            else:
                purpose = "uploaded-contract-text"
            candidates.append((path, purpose))
    contracts = root / "contracts"
    if contracts.exists():
        candidates.extend(
            (path, "stored-contract") for path in contracts.rglob("*") if path.is_file()
        )
    candidates.extend(
        (path, "uploaded-contract-text")
        for path in root.glob("contract_*")
        if path.is_file()
    )
    secrets = root / "secrets.json"
    if secrets.is_file():
        candidates.append((secrets, "connector-secrets"))
    tasks = root / "tasks.json"
    if tasks.is_file():
        candidates.append((tasks, "review-task-queue"))
    return sorted(candidates, key=lambda item: str(item[0]))
