"""MCP connector abstraction for enterprise tools/resources."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from legalworkbench.lark_mcp import hydrate_lark_server_config, lark_mcp_status, safe_server_config
from legalworkbench.paths import settings_path


class McpConnectorRegistry:
    """Local MCP connector preview.

    This independent project keeps MCP optional. A real deployment can replace this
    registry with the official MCP Python SDK client while preserving the same
    runtime boundary: Legal Agent Runtime -> MCP Connector -> Enterprise System.
    """

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()

    def context(self, *, connect: bool = False) -> dict[str, Any]:
        settings = self._load_settings()
        servers = settings.get("mcp_servers", {}) if isinstance(settings, dict) else {}
        payload: dict[str, Any] = {
            "configured_servers": sorted(servers),
            "connected": [],
            "failed": [],
            "mocked": [],
            "tools": [],
            "resources": [],
            "server_status": {},
            "lark": lark_mcp_status(self.cwd),
            "connect_attempted": connect,
        }
        for name, server in servers.items():
            if isinstance(server, dict):
                payload["server_status"][name] = safe_server_config(name, server, self.cwd)
        if connect:
            for name, server in servers.items():
                server_type = str(server.get("type") or "mock") if isinstance(server, dict) else "mock"
                hydrated = hydrate_lark_server_config(name, server, self.cwd) if isinstance(server, dict) else server
                connected = _try_real_mcp_connection(name, hydrated)
                if connected["connected"] and server_type in {"stdio", "http"}:
                    payload["connected"].append(name)
                    payload["tools"].extend(connected["tools"])
                    payload["resources"].extend(connected["resources"])
                    continue
                if isinstance(server, dict) and server_type in {"stdio", "http"}:
                    payload["failed"].append({"server": name, "error": connected["error"], "fallback": "mock_catalog"})
                    payload["tools"].extend(_mock_tools_for_server(name, server))
                    payload["resources"].extend(_mock_resources_for_server(name, server))
                else:
                    payload["connected"].append(name)
                    payload["mocked"].append(name)
                    payload["tools"].extend(_mock_tools_for_server(name, server))
                    payload["resources"].extend(_mock_resources_for_server(name, server))
        return payload

    def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any], *, timeout: float = 12.0) -> dict[str, Any]:
        settings = self._load_settings()
        servers = settings.get("mcp_servers", {}) if isinstance(settings, dict) else {}
        server = servers.get(server_name)
        if not isinstance(server, dict):
            return {"ok": False, "server": server_name, "tool": tool_name, "error": "server not configured", "content": []}
        hydrated = hydrate_lark_server_config(server_name, server, self.cwd)
        try:
            return _run_async_blocking(_call_mcp_tool_async(server_name, hydrated, tool_name, arguments), timeout=timeout)
        except TimeoutError:
            return {"ok": False, "server": server_name, "tool": tool_name, "error": "mcp tool call timed out", "content": []}
        except Exception as exc:
            return {"ok": False, "server": server_name, "tool": tool_name, "error": str(exc), "content": []}

    def _load_settings(self) -> dict[str, Any]:
        path = settings_path(self.cwd)
        if not path.exists():
            return {"mcp_servers": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"mcp_servers": {}}
        return raw if isinstance(raw, dict) else {"mcp_servers": {}}


def _mock_tools_for_server(name: str, server: object) -> list[dict[str, str]]:
    del server
    if "feishu" in name.lower():
        return [
            {"server": name, "name": "read_feishu_doc", "description": "Read contract text from Feishu Docs"},
            {"server": name, "name": "write_review_report", "description": "Write legal review report back to Feishu"},
            {"server": name, "name": "create_approval_task", "description": "Create human-review approval task"},
        ]
    if "notion" in name.lower():
        return [
            {"server": name, "name": "query_contract_playbook", "description": "Query Notion legal playbook database"},
            {"server": name, "name": "append_review_record", "description": "Append review result to Notion database"},
        ]
    return [{"server": name, "name": "list_resources", "description": "List enterprise connector resources"}]


def _mock_resources_for_server(name: str, server: object) -> list[dict[str, str]]:
    del server
    return [
        {"server": name, "name": "contract_templates", "uri": f"mcp://{name}/contract_templates"},
        {"server": name, "name": "review_audit_log", "uri": f"mcp://{name}/audit_log"},
    ]


def _try_real_mcp_connection(name: str, server: object) -> dict[str, Any]:
    if not isinstance(server, dict):
        return {"connected": False, "tools": [], "resources": [], "error": "server config is not a mapping"}
    server_type = str(server.get("type") or "mock")
    if server_type not in {"stdio", "http"}:
        return {"connected": False, "tools": [], "resources": [], "error": "mock connector"}
    try:
        timeout = float(server.get("connect_timeout") or 8.0)
        return _run_async_blocking(_connect_mcp_async(name, server), timeout=timeout)
    except TimeoutError:
        return {"connected": False, "tools": [], "resources": [], "error": "mcp connection timed out"}
    except Exception as exc:
        return {"connected": False, "tools": [], "resources": [], "error": str(exc)}


def _run_async_blocking(coro: Any, *, timeout: float) -> Any:
    """Run async MCP operations from sync code, including SDK callbacks with an active loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(asyncio.wait_for(coro, timeout=timeout))

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(asyncio.wait_for(coro, timeout=timeout))
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, name="lawbench-mcp-call", daemon=True)
    thread.start()
    thread.join(timeout + 1)
    if thread.is_alive():
        raise TimeoutError("mcp async runner timed out")
    if "error" in result:
        raise result["error"]
    return result.get("value")


async def _connect_mcp_async(name: str, server: dict[str, Any]) -> dict[str, Any]:
    try:
        from mcp import ClientSession, StdioServerParameters  # type: ignore
        from mcp.client.stdio import stdio_client  # type: ignore
        from mcp.client.streamable_http import streamable_http_client  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        return {"connected": False, "tools": [], "resources": [], "error": f"mcp sdk unavailable: {exc}"}

    server_type = str(server.get("type") or "")
    if server_type == "stdio":
        command = str(server.get("command") or server.get("url") or "")
        args = server.get("args") or []
        if isinstance(args, str):
            args = [item for item in args.split(" ") if item]
        if not command:
            return {"connected": False, "tools": [], "resources": [], "error": "stdio command required"}
        params = StdioServerParameters(command=command, args=args, env=server.get("env") or None, cwd=server.get("cwd") or None)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await _read_session_catalog(name, session)
    if server_type == "http":
        url = str(server.get("url") or "")
        if not url:
            return {"connected": False, "tools": [], "resources": [], "error": "http url required"}
        async with streamable_http_client(url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await _read_session_catalog(name, session)
    return {"connected": False, "tools": [], "resources": [], "error": f"unsupported type {server_type}"}


async def _call_mcp_tool_async(name: str, server: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        from mcp import ClientSession, StdioServerParameters  # type: ignore
        from mcp.client.stdio import stdio_client  # type: ignore
        from mcp.client.streamable_http import streamable_http_client  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        return {"ok": False, "server": name, "tool": tool_name, "error": f"mcp sdk unavailable: {exc}", "content": []}

    server_type = str(server.get("type") or "")
    if server_type == "stdio":
        command = str(server.get("command") or server.get("url") or "")
        args = server.get("args") or []
        if isinstance(args, str):
            args = [item for item in args.split(" ") if item]
        if not command:
            return {"ok": False, "server": name, "tool": tool_name, "error": "stdio command required", "content": []}
        params = StdioServerParameters(command=command, args=args, env=server.get("env") or None, cwd=server.get("cwd") or None)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                return _normalize_tool_result(name, tool_name, result)
    if server_type == "http":
        url = str(server.get("url") or "")
        if not url:
            return {"ok": False, "server": name, "tool": tool_name, "error": "http url required", "content": []}
        async with streamable_http_client(url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                return _normalize_tool_result(name, tool_name, result)
    return {"ok": False, "server": name, "tool": tool_name, "error": f"unsupported type {server_type}", "content": []}


async def _read_session_catalog(name: str, session: Any) -> dict[str, Any]:
    tools: list[dict[str, str]] = []
    resources: list[dict[str, str]] = []
    try:
        tool_result = await session.list_tools()
        for tool in getattr(tool_result, "tools", []):
            tools.append({"server": name, "name": str(tool.name), "description": str(getattr(tool, "description", "") or "")})
    except Exception:
        pass
    try:
        resource_result = await session.list_resources()
        for resource in getattr(resource_result, "resources", []):
            resources.append({"server": name, "name": str(getattr(resource, "name", "")), "uri": str(resource.uri)})
    except Exception:
        pass
    return {"connected": True, "tools": tools, "resources": resources, "error": ""}


def _normalize_tool_result(server: str, tool: str, result: Any) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for item in getattr(result, "content", []) or []:
        content.append(
            {
                "type": str(getattr(item, "type", "")),
                "text": str(getattr(item, "text", "")) if getattr(item, "text", None) is not None else "",
            }
        )
    return {
        "ok": not bool(getattr(result, "isError", False)),
        "server": server,
        "tool": tool,
        "error": "" if not bool(getattr(result, "isError", False)) else "tool returned error",
        "content": content,
    }
