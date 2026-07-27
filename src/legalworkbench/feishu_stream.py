"""Long-connection Feishu/Lark bot event listener."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from legalworkbench.feishu_events import FeishuEventBridge, normalize_feishu_event_payload
from legalworkbench.hooks import HookEvent, HookEventBus
from legalworkbench.lark_mcp import DEFAULT_LARK_SERVER_NAME, load_settings
from legalworkbench.secrets import connector_secret


@dataclass(frozen=True)
class FeishuStreamConfig:
    app_id: str
    app_secret: str
    encrypt_key: str = ""
    verification_token: str = ""
    domain: str = "https://open.feishu.cn"
    log_level: str = "INFO"


class FeishuLongConnectionListener:
    """Receive Feishu message events through the official SDK WebSocket client."""

    def __init__(
        self,
        cwd: str | Path | None = None,
        *,
        server_name: str = DEFAULT_LARK_SERVER_NAME,
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.server_name = server_name
        self.bridge = FeishuEventBridge(self.cwd)
        self.hooks = HookEventBus(self.cwd)
        self.on_result = on_result

    def status(self) -> dict[str, Any]:
        config = self._load_config()
        return {
            "mode": "long_connection",
            "server_name": self.server_name,
            "configured": bool(config.app_id and config.app_secret),
            "app_id_configured": bool(config.app_id),
            "app_secret_configured": bool(config.app_secret),
            "domain": config.domain,
            "event": "im.message.receive_v1",
            "requires_public_domain": False,
            "next_actions": self._next_actions(config),
        }

    def start(self) -> None:
        config = self._load_config()
        if not config.app_id or not config.app_secret:
            raise RuntimeError("Feishu App ID/App Secret is not configured. Run `legal-agent lark-mcp` first.")

        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        except ImportError as exc:  # pragma: no cover - depends on optional environment
            raise RuntimeError("Missing lark-oapi. Install with `python -m pip install lark-oapi`.") from exc

        def handle_message(data: P2ImMessageReceiveV1) -> None:
            payload = normalize_feishu_event_payload(data)
            result = self.bridge.handle(payload, trusted_source=True)
            self.hooks.emit(
                HookEvent(
                    name="feishu.long_connection.message",
                    review_run_id=str(result.get("review_run_id") or "feishu_stream"),
                    payload=result,
                )
            )
            if self.on_result:
                self.on_result(result)

        log_level = getattr(lark.LogLevel, config.log_level.upper(), lark.LogLevel.INFO)
        handler = (
            lark.EventDispatcherHandler.builder(config.encrypt_key, config.verification_token, log_level)
            .register_p2_im_message_receive_v1(handle_message)
            .build()
        )
        client = lark.ws.Client(
            config.app_id,
            config.app_secret,
            log_level=log_level,
            event_handler=handler,
            domain=config.domain,
            auto_reconnect=True,
            source="legal-agent-workbench",
        )
        self.hooks.emit(
            HookEvent(
                name="feishu.long_connection.started",
                review_run_id="feishu_stream",
                payload={"domain": config.domain, "event": "im.message.receive_v1"},
            )
        )
        client.start()

    def _load_config(self) -> FeishuStreamConfig:
        settings = load_settings(self.cwd)
        server = settings.get("mcp_servers", {}).get(self.server_name, {}) if isinstance(settings.get("mcp_servers"), dict) else {}
        if not isinstance(server, dict):
            server = {}
        secret = connector_secret(self.server_name, self.cwd)
        event_config = secret.get("EVENTS", {}) if isinstance(secret.get("EVENTS"), dict) else {}
        return FeishuStreamConfig(
            app_id=str(server.get("app_id") or secret.get("APP_ID") or ""),
            app_secret=str(secret.get("APP_SECRET") or ""),
            encrypt_key=str(event_config.get("ENCRYPT_KEY") or ""),
            verification_token=str(event_config.get("VERIFICATION_TOKEN") or ""),
            domain=str(server.get("domain") or "https://open.feishu.cn"),
            log_level=str(event_config.get("LOG_LEVEL") or "INFO"),
        )

    def _next_actions(self, config: FeishuStreamConfig) -> list[str]:
        actions: list[str] = []
        if not config.app_id or not config.app_secret:
            actions.append("先配置飞书开放平台 App ID / App Secret。")
        actions.append("在飞书开放平台开启机器人能力，并订阅 im.message.receive_v1。")
        actions.append("本地执行 legal-agent feishu-listen 后，在飞书里给机器人发送合同文本、文档链接或 PDF/DOCX 附件。")
        return actions
