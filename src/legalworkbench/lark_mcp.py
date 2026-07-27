"""Configuration helpers for the official Feishu/Lark MCP server."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from legalworkbench.fs import atomic_write_text
from legalworkbench.paths import settings_path
from legalworkbench.secrets import connector_secret, redact, redact_mapping, update_connector_secret

LARK_MCP_PACKAGE = "@larksuiteoapi/lark-mcp"
DEFAULT_LARK_SERVER_NAME = "feishu_legal_workspace"
DEFAULT_LEGAL_TOOLS = [
    "docx.v1.document.rawContent",
    "docx.builtin.import",
    "docx.builtin.search",
    "wiki.v2.space.getNode",
    "wiki.v1.node.search",
    "drive.v1.permissionMember.create",
    "task.v2.task.create",
    "task.v2.task.patch",
    "im.v1.message.create",
]


def load_settings(cwd: str | Path | None = None) -> dict[str, Any]:
    path = settings_path(cwd)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_settings(payload: dict[str, Any], cwd: str | Path | None = None) -> None:
    atomic_write_text(settings_path(cwd), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def build_lark_stdio_config(
    *,
    app_id: str,
    app_secret: str,
    tools: list[str] | None = None,
    domain: str = "https://open.feishu.cn",
    oauth: bool = False,
    token_mode: str = "auto",
    language: str = "zh",
    tool_name_case: str = "dot",
    user_access_token: str = "",
) -> dict[str, Any]:
    selected_tools = tools or DEFAULT_LEGAL_TOOLS
    args = [
        "-y",
        LARK_MCP_PACKAGE,
        "mcp",
        "-a",
        app_id,
        "-s",
        app_secret,
        "-d",
        domain,
        "-m",
        "stdio",
        "-l",
        language,
        "-c",
        tool_name_case,
        "-t",
        ",".join(selected_tools),
        "--token-mode",
        token_mode,
    ]
    if oauth:
        args.append("--oauth")
    if user_access_token:
        args.extend(["-u", user_access_token])
    return {
        "type": "stdio",
        "command": "npx",
        "args": args,
        "description": "Official Feishu/Lark OpenAPI MCP for legal documents, approval tasks, messages, and wiki knowledge.",
        "provider": "lark",
        "package": LARK_MCP_PACKAGE,
        "tools": selected_tools,
        "auth_mode": "oauth" if oauth else token_mode,
        "domain": domain,
        "language": language,
        "tool_name_case": tool_name_case,
        "secret_ref": f"connectors.{DEFAULT_LARK_SERVER_NAME}",
    }


def configure_lark_mcp(
    cwd: str | Path | None = None,
    *,
    server_name: str = DEFAULT_LARK_SERVER_NAME,
    app_id: str,
    app_secret: str,
    tools: list[str] | None = None,
    domain: str = "https://open.feishu.cn",
    oauth: bool = False,
    token_mode: str = "auto",
    language: str = "zh",
    tool_name_case: str = "dot",
    user_access_token: str = "",
    connect_timeout: float = 8.0,
) -> dict[str, Any]:
    settings = load_settings(cwd)
    servers = settings.setdefault("mcp_servers", {})
    selected_tools = [item.strip() for item in (tools or DEFAULT_LEGAL_TOOLS) if item.strip()]
    public_config = build_lark_stdio_config(
        app_id="${APP_ID}",
        app_secret="${APP_SECRET}",
        tools=selected_tools,
        domain=domain,
        oauth=oauth,
        token_mode=token_mode,
        language=language,
        tool_name_case=tool_name_case,
        user_access_token="${USER_ACCESS_TOKEN}" if user_access_token else "",
    )
    public_config.update(
        {
            "app_id": app_id,
            "secret_ref": f"connectors.{server_name}",
            "args_template": public_config["args"],
            "args": _build_runtime_args(
                app_id=app_id,
                app_secret="",
                tools=selected_tools,
                domain=domain,
                oauth=oauth,
                token_mode=token_mode,
                language=language,
                tool_name_case=tool_name_case,
                user_access_token="",
            ),
            "env_secret_keys": ["APP_SECRET"] + (["USER_ACCESS_TOKEN"] if user_access_token else []),
            "connect_timeout": connect_timeout,
            "configured": True,
        }
    )
    servers[server_name] = public_config
    save_settings(settings, cwd)
    update_connector_secret(
        server_name,
        {
            "APP_ID": app_id,
            "APP_SECRET": app_secret,
            "USER_ACCESS_TOKEN": user_access_token,
        },
        cwd,
    )
    return lark_mcp_status(cwd, server_name=server_name)


def _build_runtime_args(
    *,
    app_id: str,
    app_secret: str,
    tools: list[str],
    domain: str,
    oauth: bool,
    token_mode: str,
    language: str,
    tool_name_case: str,
    user_access_token: str,
) -> list[str]:
    config = build_lark_stdio_config(
        app_id=app_id,
        app_secret=app_secret,
        tools=tools,
        domain=domain,
        oauth=oauth,
        token_mode=token_mode,
        language=language,
        tool_name_case=tool_name_case,
        user_access_token=user_access_token,
    )
    return list(config["args"])


def hydrate_lark_server_config(server_name: str, server: dict[str, Any], cwd: str | Path | None = None) -> dict[str, Any]:
    if server.get("provider") != "lark":
        return server
    secret = connector_secret(server_name, cwd)
    app_id = str(server.get("app_id") or secret.get("APP_ID") or "")
    app_secret = str(secret.get("APP_SECRET") or "")
    user_access_token = str(secret.get("USER_ACCESS_TOKEN") or "")
    tools = _as_list(server.get("tools")) or DEFAULT_LEGAL_TOOLS
    hydrated = dict(server)
    hydrated["command"] = server.get("command") or "npx"
    hydrated["args"] = _build_runtime_args(
        app_id=app_id,
        app_secret=app_secret,
        tools=tools,
        domain=str(server.get("domain") or "https://open.feishu.cn"),
        oauth=bool(server.get("auth_mode") == "oauth" or server.get("oauth")),
        token_mode=str(server.get("auth_mode") if server.get("auth_mode") != "oauth" else "auto") or "auto",
        language=str(server.get("language") or "zh"),
        tool_name_case=str(server.get("tool_name_case") or "dot"),
        user_access_token=user_access_token,
    )
    env = dict(server.get("env") or {})
    env.update({"APP_ID": app_id, "APP_SECRET": app_secret})
    if user_access_token:
        env["USER_ACCESS_TOKEN"] = user_access_token
    hydrated["env"] = env
    return hydrated


def safe_server_config(server_name: str, server: dict[str, Any], cwd: str | Path | None = None) -> dict[str, Any]:
    safe = redact_mapping(dict(server))
    secret = connector_secret(server_name, cwd)
    safe["secrets"] = {
        "app_secret_configured": bool(secret.get("APP_SECRET")),
        "user_access_token_configured": bool(secret.get("USER_ACCESS_TOKEN")),
        "app_id": redact(secret.get("APP_ID") or server.get("app_id") or ""),
    }
    if "args" in safe:
        safe["args"] = _redact_args([str(item) for item in safe["args"]])
    if "args_template" in safe:
        safe["args_template"] = _redact_args([str(item) for item in safe["args_template"]])
    return safe


def lark_mcp_status(cwd: str | Path | None = None, *, server_name: str = DEFAULT_LARK_SERVER_NAME) -> dict[str, Any]:
    settings = load_settings(cwd)
    server = settings.get("mcp_servers", {}).get(server_name, {}) if isinstance(settings.get("mcp_servers"), dict) else {}
    if not isinstance(server, dict):
        server = {}
    secret = connector_secret(server_name, cwd)
    return {
        "server_name": server_name,
        "configured": bool(server and server.get("provider") == "lark"),
        "package": LARK_MCP_PACKAGE,
        "npx_path": shutil.which("npx") or "",
        "node": node_status(),
        "app_id_configured": bool(server.get("app_id") or secret.get("APP_ID")),
        "app_secret_configured": bool(secret.get("APP_SECRET")),
        "user_access_token_configured": bool(secret.get("USER_ACCESS_TOKEN")),
        "auth_mode": server.get("auth_mode") or "unconfigured",
        "domain": server.get("domain") or "https://open.feishu.cn",
        "tools": _as_list(server.get("tools")) or DEFAULT_LEGAL_TOOLS,
        "server": safe_server_config(server_name, server, cwd) if server else {},
        "next_actions": _next_actions(server, secret),
    }


def node_status() -> dict[str, Any]:
    node_path = shutil.which("node") or ""
    npm_path = shutil.which("npm") or ""
    result: dict[str, Any] = {"node_path": node_path, "npm_path": npm_path, "version": "", "major": 0, "ok": False}
    if not node_path:
        return result
    try:
        completed = subprocess.run([node_path, "-v"], check=False, capture_output=True, text=True, timeout=3)
    except Exception as exc:
        result["error"] = str(exc)
        return result
    version = completed.stdout.strip().lstrip("v")
    result["version"] = version
    try:
        major = int(version.split(".", 1)[0])
    except (ValueError, IndexError):
        major = 0
    result["major"] = major
    result["ok"] = major >= 20
    return result


def lark_login_command(
    *,
    app_id: str,
    app_secret: str,
    scope: str = "offline_access docx:document docx:document:readonly wiki:wiki im:message task:task",
) -> list[str]:
    return [
        "npx",
        "-y",
        LARK_MCP_PACKAGE,
        "login",
        "-a",
        app_id,
        "-s",
        app_secret,
        "--scope",
        scope,
    ]


def _redact_args(args: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for item in args:
        if redact_next:
            redacted.append(str(redact(item)))
            redact_next = False
            continue
        redacted.append(item)
        if item in {"-s", "--app-secret", "-u", "--user-access-token"}:
            redact_next = True
    return redacted


def _as_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace(" ", ",").split(",") if item.strip()]
    return []


def _next_actions(server: dict[str, Any], secret: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if not server or server.get("provider") != "lark":
        actions.append("在工作台或 CLI 中配置飞书开放平台 App ID / App Secret。")
    if not shutil.which("npx"):
        actions.append("安装 Node.js/npm，确保 npx 可用。")
    if not node_status()["ok"]:
        actions.append("升级 Node.js 到 20 或以上。")
    if not secret.get("APP_SECRET"):
        actions.append("把 App Secret 写入本地 secrets，不要提交到代码仓库。")
    if server and server.get("auth_mode") == "oauth" and not secret.get("USER_ACCESS_TOKEN"):
        actions.append("执行 login 命令完成 OAuth 用户授权，或在前端配置 user_access_token。")
    if not actions:
        actions.append("可以点击“连接检查”发现飞书 MCP 工具。")
    return actions
