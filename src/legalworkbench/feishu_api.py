"""Small Feishu OpenAPI client for bot message attachments."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from legalworkbench.lark_mcp import DEFAULT_LARK_SERVER_NAME, load_settings
from legalworkbench.secrets import connector_secret


class FeishuApiError(RuntimeError):
    """Raised when Feishu OpenAPI cannot complete an operation."""


@dataclass(frozen=True)
class FeishuDownloadedFile:
    filename: str
    content: bytes
    content_type: str = ""


class FeishuOpenApiClient:
    """Download files from Feishu message events with tenant credentials."""

    def __init__(
        self,
        cwd: str | Path | None = None,
        *,
        server_name: str = DEFAULT_LARK_SERVER_NAME,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.server_name = server_name
        self.timeout_seconds = timeout_seconds
        self._tenant_token = ""
        self._tenant_token_expires_at = 0.0

    def ready(self) -> bool:
        config = self._load_config()
        return bool(config["app_id"] and config["app_secret"])

    def download_message_file(
        self,
        *,
        message_id: str,
        file_key: str,
        filename: str = "contract",
        resource_type: str = "file",
    ) -> FeishuDownloadedFile:
        if not message_id:
            raise FeishuApiError("message_id required for Feishu file download")
        if not file_key:
            raise FeishuApiError("file_key required for Feishu file download")
        token = self.tenant_access_token()
        config = self._load_config()
        url = (
            f"{config['domain'].rstrip('/')}/open-apis/im/v1/messages/"
            f"{quote(message_id, safe='')}/resources/{quote(file_key, safe='')}"
        )
        headers = {"Authorization": f"Bearer {token}"}
        params = {"type": resource_type or "file"}
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.get(url, headers=headers, params=params)
        if response.status_code >= 400:
            raise FeishuApiError(_format_openapi_error(response, "download message file"))
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type.lower():
            payload = _safe_json(response)
            if int(payload.get("code") or 0) != 0:
                raise FeishuApiError(f"Feishu download message file failed: {payload.get('msg') or payload}")
        if not response.content:
            raise FeishuApiError("Feishu download message file returned empty content")
        return FeishuDownloadedFile(filename=filename or "contract", content=response.content, content_type=content_type)

    def tenant_access_token(self) -> str:
        if self._tenant_token and time.time() < self._tenant_token_expires_at - 60:
            return self._tenant_token
        config = self._load_config()
        if not config["app_id"] or not config["app_secret"]:
            raise FeishuApiError("Feishu App ID/App Secret is not configured")
        url = f"{config['domain'].rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal"
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(
                url,
                json={"app_id": config["app_id"], "app_secret": config["app_secret"]},
            )
        if response.status_code >= 400:
            raise FeishuApiError(_format_openapi_error(response, "get tenant_access_token"))
        payload = _safe_json(response)
        if int(payload.get("code") or 0) != 0:
            raise FeishuApiError(f"Feishu tenant_access_token failed: {payload.get('msg') or payload}")
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise FeishuApiError("Feishu tenant_access_token missing in response")
        expire = float(payload.get("expire") or 7200)
        self._tenant_token = token
        self._tenant_token_expires_at = time.time() + expire
        return token

    def _load_config(self) -> dict[str, str]:
        settings = load_settings(self.cwd)
        servers = settings.get("mcp_servers", {}) if isinstance(settings.get("mcp_servers"), dict) else {}
        server = servers.get(self.server_name, {}) if isinstance(servers.get(self.server_name, {}), dict) else {}
        secret = connector_secret(self.server_name, self.cwd)
        return {
            "app_id": str(server.get("app_id") or secret.get("APP_ID") or ""),
            "app_secret": str(secret.get("APP_SECRET") or ""),
            "domain": str(server.get("domain") or "https://open.feishu.cn"),
        }


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"raw": response.text[:500]}
    return payload if isinstance(payload, dict) else {"raw": payload}


def _format_openapi_error(response: httpx.Response, action: str) -> str:
    payload = _safe_json(response)
    msg = payload.get("msg") or payload.get("message") or response.text[:300]
    code = payload.get("code") or response.status_code
    return f"Feishu {action} failed: status={response.status_code}, code={code}, msg={msg}"
