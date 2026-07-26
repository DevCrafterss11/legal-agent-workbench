"""Feishu/Lark bot event bridge for contract review."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from legalworkbench.documents import ContractDocumentStore
from legalworkbench.feishu_api import FeishuApiError, FeishuOpenApiClient
from legalworkbench.fs import atomic_write_text
from legalworkbench.hooks import HookEvent, HookEventBus
from legalworkbench.mcp import McpConnectorRegistry
from legalworkbench.models import ReviewRun
from legalworkbench.paths import workspace_dir
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.secrets import connector_secret


DOC_TOKEN_PATTERNS = [
    re.compile(r"/docx/([A-Za-z0-9]+)"),
    re.compile(r"/docs/([A-Za-z0-9]+)"),
    re.compile(r"document_id[=:]([A-Za-z0-9]+)"),
]


@dataclass(frozen=True)
class FeishuEventConfig:
    verification_token: str = ""
    encrypt_key: str = ""
    app_id: str = ""
    callback_secret: str = ""
    auto_reply: bool = True
    review_sync: bool = True


@dataclass(frozen=True)
class FeishuAttachment:
    file_key: str
    file_name: str
    resource_type: str = "file"


class FeishuEventBridge:
    """Handle Feishu event callbacks and route messages to LegalAgentRuntime."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.documents = ContractDocumentStore(self.cwd)
        self.hooks = HookEventBus(self.cwd)
        self.mcp = McpConnectorRegistry(self.cwd)
        self.feishu_api = FeishuOpenApiClient(self.cwd)
        self.config = self._load_config()
        self._dedup_cache: Any = None

    def handle(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        trusted_source: bool = False,
    ) -> dict[str, Any]:
        headers = headers or {}
        if "challenge" in payload:
            return {"challenge": payload.get("challenge")}
        if not trusted_source and not self._verify_token(payload):
            return {"ok": False, "error": "invalid verification token", "status": "ignored"}
        if not trusted_source and not self._verify_signature(payload, headers):
            return {"ok": False, "error": "invalid event signature", "status": "ignored"}

        event = self._extract_event(payload)
        if not event:
            return {"ok": True, "status": "ignored", "reason": "unsupported event payload"}

        message = event.get("message", {}) if isinstance(event.get("message"), dict) else {}
        sender = event.get("sender", {}) if isinstance(event.get("sender"), dict) else {}
        chat_id = str(message.get("chat_id") or "")
        message_id = str(message.get("message_id") or "")
        sender_id = _first_id(sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {})
        if _is_self_message(sender, self.config.app_id):
            result = {"ok": True, "status": "ignored", "reason": "self app message", "message_id": message_id}
            self._emit("feishu.event.ignored", result)
            return result
        if message_id and self._is_message_processed(message_id):
            result = {"ok": True, "status": "ignored", "reason": "duplicate message", "message_id": message_id}
            self._emit("feishu.event.ignored", result)
            return result
        attachment = _extract_file_attachment(message)
        unsupported_reason = _unsupported_message_reason(message)
        if unsupported_reason:
            self._mark_message_processed(message_id)
            reply_result = self._send_reply(chat_id=chat_id, open_id=sender_id, text=unsupported_reason) if self.config.auto_reply else {"ok": False, "skipped": True}
            result = {"ok": True, "status": "ignored", "reason": "unsupported message type", "message_id": message_id, "reply": reply_result}
            self._emit("feishu.event.ignored", result)
            return result
        text, document_id = self._extract_contract_input(message)
        if _looks_like_agent_reply(text):
            self._mark_message_processed(message_id)
            result = {"ok": True, "status": "ignored", "reason": "agent reply text", "message_id": message_id}
            self._emit("feishu.event.ignored", result)
            return result
        if not text and document_id:
            text = self._read_feishu_document(document_id)
        record: dict[str, Any] | None = None
        if not text and attachment:
            try:
                record = self._download_feishu_attachment(message_id=message_id, attachment=attachment)
            except FeishuApiError as exc:
                self._mark_message_processed(message_id)
                reply_text = (
                    f"收到文件：{attachment.file_name}\n\n"
                    f"但下载失败：{exc}\n\n"
                    "请检查飞书应用是否已开通读取消息资源文件权限，并确认机器人能接收文件消息。"
                )
                reply_result = self._send_reply(chat_id=chat_id, open_id=sender_id, text=reply_text) if self.config.auto_reply else {"ok": False, "skipped": True}
                result = {
                    "ok": False,
                    "status": "failed",
                    "reason": "file download failed",
                    "message_id": message_id,
                    "file_key": attachment.file_key,
                    "file_name": attachment.file_name,
                    "reply": reply_result,
                }
                self._emit("feishu.event.failed", result)
                return result
        if not text and record is None:
            self._mark_message_processed(message_id)
            result = {
                "ok": True,
                "status": "ignored",
                "reason": "no contract text, document token, or file attachment found",
                "message_id": message_id,
            }
            self._emit("feishu.event.ignored", result)
            return result

        self._mark_message_processed(message_id)
        if record is None:
            record = self.documents.save_text(filename=f"feishu_{message_id or int(time.time())}.md", text=text, source="feishu_bot")
        run = LegalAgentRuntime(self.cwd).review(record["path"], connect_mcp=True) if self.config.review_sync else None
        reply = self._build_reply(run) if run else "已收到合同，审查任务已创建。"
        reply_result = self._send_reply(chat_id=chat_id, open_id=sender_id, text=reply) if self.config.auto_reply else {"ok": False, "skipped": True}
        result = {
            "ok": True,
            "status": "reviewed" if run else "queued",
            "document_id": record["document_id"],
            "review_run_id": run.review_run_id if run else "",
            "report_path": run.report_path if run else "",
            "message_id": message_id,
            "file_name": attachment.file_name if attachment else "",
            "reply": reply_result,
        }
        self._emit("feishu.event.reviewed", result)
        return result

    def _load_config(self) -> FeishuEventConfig:
        secret = connector_secret("feishu_legal_workspace", self.cwd)
        event_config = secret.get("EVENTS", {}) if isinstance(secret.get("EVENTS"), dict) else {}
        return FeishuEventConfig(
            verification_token=str(event_config.get("VERIFICATION_TOKEN") or ""),
            encrypt_key=str(event_config.get("ENCRYPT_KEY") or ""),
            app_id=str(secret.get("APP_ID") or ""),
            callback_secret=str(event_config.get("CALLBACK_SECRET") or ""),
            auto_reply=bool(event_config.get("AUTO_REPLY", True)),
            review_sync=bool(event_config.get("REVIEW_SYNC", True)),
        )

    def _verify_token(self, payload: dict[str, Any]) -> bool:
        if not self.config.verification_token:
            return True
        token = str(payload.get("token") or payload.get("header", {}).get("token") or "")
        return hmac.compare_digest(token, self.config.verification_token)

    def _verify_signature(self, payload: dict[str, Any], headers: dict[str, str]) -> bool:
        if not self.config.callback_secret:
            return True
        signature = headers.get("x-lark-signature") or headers.get("X-Lark-Signature") or ""
        timestamp = headers.get("x-lark-request-timestamp") or headers.get("X-Lark-Request-Timestamp") or ""
        nonce = headers.get("x-lark-request-nonce") or headers.get("X-Lark-Request-Nonce") or ""
        if not signature or not timestamp or not nonce:
            return False
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        base = f"{timestamp}{nonce}{self.config.callback_secret}{body}".encode("utf-8")
        digest = hashlib.sha256(base).hexdigest()
        return hmac.compare_digest(signature, digest)

    def _extract_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(payload.get("event"), dict):
            return payload["event"]
        if isinstance(payload.get("schema"), str) and isinstance(payload.get("event"), dict):
            return payload["event"]
        return None

    def _extract_contract_input(self, message: dict[str, Any]) -> tuple[str, str]:
        msg_type = str(message.get("message_type") or message.get("msg_type") or "")
        content = _parse_json_object(message.get("content"))
        text = ""
        document_id = ""
        if msg_type == "text" or "text" in content:
            text = str(content.get("text") or "")
            document_id = extract_document_id(text)
        if not document_id:
            document_id = _find_token_in_object(content)
        if not text and msg_type in {"post", "interactive"}:
            text = json.dumps(content, ensure_ascii=False, indent=2)
        return text.strip(), document_id

    def _processed_path(self) -> Path:
        return workspace_dir(self.cwd) / "feishu_processed_messages.json"

    def _is_message_processed(self, message_id: str) -> bool:
        if not message_id:
            return False
        # 飞书事件回调是 at-least-once 重试语义：先查 Redis（多实例共享、带 TTL），
        # 再查文件审计记录兜底
        if self._cache().get(f"feishu:msg:{message_id}") is not None:
            return True
        return message_id in self._load_processed_messages()

    def _mark_message_processed(self, message_id: str) -> None:
        if not message_id:
            return
        self._cache().set(f"feishu:msg:{message_id}", f"{time.time():.3f}", ttl_seconds=7 * 86_400)
        processed = self._load_processed_messages()
        processed[message_id] = time.time()
        if len(processed) > 1000:
            processed = dict(sorted(processed.items(), key=lambda item: item[1], reverse=True)[:1000])
        atomic_write_text(self._processed_path(), json.dumps(processed, ensure_ascii=False, indent=2) + "\n")

    def _cache(self) -> Any:
        if self._dedup_cache is None:
            from legalworkbench.cache import create_cache

            self._dedup_cache = create_cache(self.cwd)
        return self._dedup_cache

    def _load_processed_messages(self) -> dict[str, float]:
        path = self._processed_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): float(value) for key, value in raw.items() if key}

    def _read_feishu_document(self, document_id: str) -> str:
        result = self.mcp.call_tool(
            "feishu_legal_workspace",
            "docx.v1.document.rawContent",
            {"path": {"document_id": document_id}, "params": {"lang": 0}},
        )
        if not result.get("ok"):
            return f"# 飞书文档读取失败\n\nDocument ID: `{document_id}`\n\n{result.get('error')}"
        text_parts = [item.get("text", "") for item in result.get("content", []) if item.get("text")]
        return "\n\n".join(text_parts).strip() or f"# 空飞书文档\n\nDocument ID: `{document_id}`"

    def _download_feishu_attachment(self, *, message_id: str, attachment: "FeishuAttachment") -> dict[str, Any]:
        downloaded = self.feishu_api.download_message_file(
            message_id=message_id,
            file_key=attachment.file_key,
            filename=attachment.file_name,
            resource_type=attachment.resource_type,
        )
        return self.documents.save_bytes(
            filename=downloaded.filename,
            data=downloaded.content,
            source="feishu_file",
        )

    def _send_reply(self, *, chat_id: str, open_id: str, text: str) -> dict[str, Any]:
        receive_id = chat_id or open_id
        if not receive_id:
            return {"ok": False, "error": "missing chat_id/open_id", "content": []}
        # 出境脱敏：回发内容属于跨信任边界输出，PII 一律占位符化（不可逆方向，
        # 群聊里不应出现身份证/手机号明文；需要原文时在本地工作台查看）
        from legalworkbench.privacy import mask as mask_pii

        masked = mask_pii(text)
        if masked.has_pii:
            text = masked.masked_text
            self.hooks.emit(
                HookEvent("privacy.outbound_masked", "feishu", {"counts": masked.counts})
            )
        receive_id_type = "chat_id" if chat_id else "open_id"
        return self.mcp.call_tool(
            "feishu_legal_workspace",
            "im.v1.message.create",
            {
                "params": {"receive_id_type": receive_id_type},
                "data": {
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text[:3500]}, ensure_ascii=False),
                    "uuid": f"lawbench-{int(time.time() * 1000)}",
                },
            },
        )

    def _build_reply(self, run: ReviewRun) -> str:
        findings = run.findings
        high_count = sum(1 for item in findings if item.risk_level == "high")
        review_count = sum(1 for item in findings if item.requires_human_review)
        lines = [
            f"合同审查完成：{run.review_run_id}",
            "",
            f"结论：发现 {len(findings)} 个风险；高风险 {high_count} 个；需人工复核 {review_count} 个。",
        ]
        if not findings:
            lines.extend(["", "未发现高置信风险条款。建议仍由法务对核心商业条款做最终确认。"])
        for index, finding in enumerate(findings[:3], start=1):
            evidence = finding.evidence[0] if finding.evidence else None
            source = f"{evidence.source} · {evidence.title}" if evidence else "未命中明确来源"
            reason = evidence.body_preview if evidence else finding.summary
            review = "需人工复核" if finding.requires_human_review else "可按建议修改"
            clause_text = _clause_text(run, finding.clause_id)
            lines.extend(
                [
                    "",
                    f"{index}. [{_risk_label(finding.risk_level)}] {finding.clause_title}：{_risk_title(finding.risk_type)}",
                    f"原文：{_compact_text(clause_text, 180)}",
                    f"问题：{finding.summary} {_risk_impact(finding.risk_type)}".strip(),
                    f"依据：{source}",
                    f"要点：{reason}",
                    _format_suggestion(finding.suggestion),
                    f"处理：{review}",
                ]
            )
        if len(findings) > 3:
            lines.append(f"\n其余 {len(findings) - 3} 个风险请在工作台查看完整报告。")
        lines.extend(
            [
                "",
                f"完整报告：{run.report_path}",
                "提示：本回复为 Agent 初审结果，高风险条款需法务最终确认。",
            ]
        )
        return "\n".join(lines)[:3400]

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        self.hooks.emit(HookEvent(name=name, review_run_id=str(payload.get("review_run_id") or "feishu_event"), payload=payload))


def extract_document_id(text: str) -> str:
    for pattern in DOC_TOKEN_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def _find_token_in_object(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"document_id", "doc_token", "file_token", "token"} and isinstance(item, str) and item:
                return item
            found = _find_token_in_object(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_token_in_object(item)
            if found:
                return found
    if isinstance(value, str):
        return extract_document_id(value)
    return ""


def _extract_file_attachment(message: dict[str, Any]) -> FeishuAttachment | None:
    msg_type = str(message.get("message_type") or message.get("msg_type") or "")
    if msg_type != "file":
        return None
    content = _parse_json_object(message.get("content"))
    file_key = str(content.get("file_key") or content.get("fileKey") or message.get("file_key") or "")
    file_name = str(
        content.get("file_name")
        or content.get("fileName")
        or content.get("name")
        or message.get("file_name")
        or f"feishu_{message.get('message_id') or int(time.time())}.bin"
    )
    if not file_key:
        return None
    return FeishuAttachment(file_key=file_key, file_name=file_name, resource_type="file")


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"text": value}
        return parsed if isinstance(parsed, dict) else {"text": value}
    return {}


def _first_id(value: dict[str, Any]) -> str:
    for key in ("open_id", "user_id", "union_id"):
        if value.get(key):
            return str(value[key])
    return ""


def _is_self_message(sender: dict[str, Any], app_id: str) -> bool:
    sender_type = str(sender.get("sender_type") or "").lower()
    if sender_type == "app":
        return True
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    if app_id and any(str(value) == app_id for value in sender_id.values()):
        return True
    return False


def _looks_like_agent_reply(text: str) -> bool:
    head = re.sub(r"\s+", " ", text.strip())[:240]
    if not head:
        return False
    return any(
        marker in head
        for marker in (
            "合同审查完成：law_",
            "合同审查完成: law_",
            "Review Run: `law_",
            "完整报告：",
        )
    )


def _unsupported_message_reason(message: dict[str, Any]) -> str:
    msg_type = str(message.get("message_type") or message.get("msg_type") or "")
    if msg_type in {"text", "post", "interactive", "file"}:
        return ""
    return (
        f"收到 `{msg_type or 'unknown'}` 类型消息。\n\n"
        "当前机器人入口支持合同文本、飞书文档链接，以及 PDF/DOCX/TXT/MD 文件附件。"
    )


def _risk_label(level: str) -> str:
    return {
        "high": "高风险",
        "medium": "中风险",
        "low": "低风险",
    }.get(level, level or "风险")


def _risk_title(risk_type: str) -> str:
    return {
        "unlimited_liability": "赔偿责任过宽/无限责任",
        "auto_renewal": "自动续约约束不足",
        "data_security": "数据安全责任不清",
        "jurisdiction": "争议管辖不利",
        "payment": "付款周期异常",
        "ip_ownership": "知识产权归属不清",
    }.get(risk_type, risk_type or "风险事项")


def _risk_impact(risk_type: str) -> str:
    return {
        "unlimited_liability": "风险影响：责任范围没有边界，可能把间接损失、预期利润损失、商誉损失等扩张到乙方承担。",
        "auto_renewal": "风险影响：未约定通知期和取消路径时，业务方可能在不知情情况下被动续约。",
        "data_security": "风险影响：数据处理范围、安全标准和泄露通知不清，会放大监管、客户索赔和举证风险。",
        "jurisdiction": "风险影响：争议解决地不利会提高维权成本，并削弱后续谈判空间。",
        "payment_acceptance": "风险影响：付款未绑定交付、验收或发票，可能造成款项前置和履约控制不足。",
        "payment_cycle": "风险影响：付款过于前置会削弱交付约束，增加供应商未按期交付时的追偿难度。",
        "ip_ownership": "风险影响：成果权属和授权边界不清，会影响后续使用、商业化和二次开发。",
        "sla_remedy": "风险影响：只有服务指标没有补救机制时，服务不可用后难以落地追责。",
    }.get(risk_type, "")


def _clause_text(run: ReviewRun, clause_id: str) -> str:
    for clause in run.clauses:
        if clause.clause_id == clause_id:
            return clause.text
    return ""


def _compact_text(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    if not value:
        return "未定位到原文片段"
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _format_suggestion(suggestion: str) -> str:
    text = suggestion.strip() or "建议补充更明确的责任边界、履约标准和救济方式。"
    if text.startswith("建议改为："):
        return f"建议替换为：{text.removeprefix('建议改为：')}"
    return f"修改建议：{text}"


def normalize_feishu_event_payload(data: Any) -> dict[str, Any]:
    """Convert SDK event objects or dictionaries to the bridge payload format."""
    if isinstance(data, dict):
        if isinstance(data.get("event"), dict):
            return data
        return {"schema": "2.0", "header": {"event_type": "im.message.receive_v1"}, "event": data}

    header = getattr(data, "header", None)
    event = getattr(data, "event", None)
    sender = getattr(event, "sender", None) if event is not None else None
    message = getattr(event, "message", None) if event is not None else None
    sender_id = getattr(sender, "sender_id", None)
    return {
        "schema": "2.0",
        "header": {
            "event_type": getattr(header, "event_type", None) or "im.message.receive_v1",
            "event_id": getattr(header, "event_id", None) or "",
            "create_time": getattr(header, "create_time", None) or "",
            "tenant_key": getattr(header, "tenant_key", None) or "",
            "app_id": getattr(header, "app_id", None) or "",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": getattr(sender_id, "open_id", None) or "",
                    "user_id": getattr(sender_id, "user_id", None) or "",
                    "union_id": getattr(sender_id, "union_id", None) or "",
                    "app_id": getattr(sender_id, "app_id", None) or "",
                },
                "sender_type": getattr(sender, "sender_type", None) or "",
                "tenant_key": getattr(sender, "tenant_key", None) or "",
            },
            "message": {
                "message_id": getattr(message, "message_id", None) or "",
                "root_id": getattr(message, "root_id", None) or "",
                "parent_id": getattr(message, "parent_id", None) or "",
                "chat_id": getattr(message, "chat_id", None) or "",
                "thread_id": getattr(message, "thread_id", None) or "",
                "chat_type": getattr(message, "chat_type", None) or "",
                "message_type": getattr(message, "message_type", None) or "",
                "content": getattr(message, "content", None) or "",
            },
        },
    }


def write_event_setup_guide(cwd: str | Path | None = None) -> Path:
    path = workspace_dir(cwd) / "feishu_event_setup.md"
    path.write_text(
        "\n".join(
            [
                "# Feishu Bot Event Setup",
                "",
                "## Option A: Long connection event subscription",
                "",
                "Use this for local development. It does not require a public HTTPS domain because the local process connects to Feishu actively.",
                "",
                "1. Enable bot capability in Feishu Open Platform.",
                "2. Subscribe to `im.message.receive_v1`.",
                "3. Start `legal-agent feishu-listen` locally.",
                "4. Send a Feishu doc link, contract text, or PDF/DOCX attachment to the bot.",
                "",
                "## Option B: HTTP callback",
                "",
                "Use this for production deployment or when you already have a public HTTPS service.",
                "",
                "1. Expose the local server with ngrok/cloudflared or deploy to a public HTTPS domain.",
                "2. Set Feishu event callback URL to `https://<public-domain>/api/feishu/events`.",
                "3. Subscribe to message receive events, for example `im.message.receive_v1`.",
                "4. Send a Feishu doc link, contract text, or PDF/DOCX attachment to the bot.",
                "5. Watch the Legal Agent Workbench create a review run and reply through MCP.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path
