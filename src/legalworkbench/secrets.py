"""Local secret storage for connector credentials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from legalworkbench.fs import atomic_write_text
from legalworkbench.paths import secrets_path


SENSITIVE_KEYS = {"secret", "app_secret", "appSecret", "token", "user_access_token", "USER_ACCESS_TOKEN", "APP_SECRET"}


def load_secrets(cwd: str | Path | None = None) -> dict[str, Any]:
    path = secrets_path(cwd)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_secrets(payload: dict[str, Any], cwd: str | Path | None = None) -> None:
    path = secrets_path(cwd)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def update_connector_secret(server_name: str, secret_payload: dict[str, Any], cwd: str | Path | None = None) -> None:
    secrets = load_secrets(cwd)
    connectors = secrets.setdefault("connectors", {})
    existing = connectors.get(server_name, {})
    if not isinstance(existing, dict):
        existing = {}
    existing.update({key: value for key, value in secret_payload.items() if value not in {"", None}})
    connectors[server_name] = existing
    save_secrets(secrets, cwd)


def connector_secret(server_name: str, cwd: str | Path | None = None) -> dict[str, Any]:
    connectors = load_secrets(cwd).get("connectors", {})
    if not isinstance(connectors, dict):
        return {}
    value = connectors.get(server_name, {})
    return value if isinstance(value, dict) else {}


def redact(value: object) -> object:
    if not isinstance(value, str):
        return value
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def redact_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if key in SENSITIVE_KEYS or any(word in key.lower() for word in ("secret", "token")):
            redacted[key] = redact(value)
        elif isinstance(value, dict):
            redacted[key] = redact_mapping(value)
        elif isinstance(value, list):
            redacted[key] = [redact_mapping(item) if isinstance(item, dict) else item for item in value]
        else:
            redacted[key] = value
    return redacted
