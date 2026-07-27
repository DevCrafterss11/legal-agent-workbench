"""Project-local path helpers."""

from __future__ import annotations

import os
from pathlib import Path


def project_root(cwd: str | Path | None = None) -> Path:
    return Path(cwd or Path.cwd()).resolve()


def workspace_dir(cwd: str | Path | None = None) -> Path:
    path = project_root(cwd) / ".lawbench"
    return _private_dir(path)


def knowledge_dir(cwd: str | Path | None = None) -> Path:
    path = workspace_dir(cwd) / "knowledge"
    return _private_dir(path)


def contracts_dir(cwd: str | Path | None = None) -> Path:
    path = workspace_dir(cwd) / "contracts"
    return _private_dir(path)


def uploads_dir(cwd: str | Path | None = None) -> Path:
    path = workspace_dir(cwd) / "uploads"
    return _private_dir(path)


def runs_dir(cwd: str | Path | None = None) -> Path:
    path = workspace_dir(cwd) / "runs"
    return _private_dir(path)


def memory_path(cwd: str | Path | None = None) -> Path:
    return workspace_dir(cwd) / "memory.json"


def skills_path(cwd: str | Path | None = None) -> Path:
    return workspace_dir(cwd) / "skills.json"


def skills_dir(cwd: str | Path | None = None) -> Path:
    path = workspace_dir(cwd) / "skills"
    return _private_dir(path)


def benchmark_path(cwd: str | Path | None = None) -> Path:
    return workspace_dir(cwd) / "benchmark.json"


def settings_path(cwd: str | Path | None = None) -> Path:
    env = os.environ.get("LEGAL_WORKBENCH_SETTINGS")
    return Path(env).expanduser().resolve() if env else workspace_dir(cwd) / "settings.json"


def secrets_path(cwd: str | Path | None = None) -> Path:
    env = os.environ.get("LEGAL_WORKBENCH_SECRETS")
    return Path(env).expanduser().resolve() if env else workspace_dir(cwd) / "secrets.json"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # Read-only/delegated filesystems may own permissions outside the process.
        pass
    return path
