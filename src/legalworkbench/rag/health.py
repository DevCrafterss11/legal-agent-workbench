"""RAG infrastructure health checks."""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from legalworkbench.rag.service import LegalRagService


def rag_health(cwd: str | Path | None = None, *, timeout: float = 1.0) -> dict[str, Any]:
    docker = _command_status(["docker", "info"], timeout=3)
    compose = _command_status(["docker", "compose", "version"], timeout=3)
    port_19530 = _tcp_check("127.0.0.1", 19530, timeout=timeout)
    port_9091 = _tcp_check("127.0.0.1", 9091, timeout=timeout)
    service = LegalRagService(cwd)
    status = service.status()
    vector = status.get("vector_store", {})
    embedding_real = str(status.get("embedding_model", "")).startswith("sentence-transformers:")
    return {
        "ok": bool(vector.get("connected")) and not status.get("embedding_error"),
        "docker": docker,
        "docker_compose": compose,
        "ports": {
            "milvus_grpc_19530": port_19530,
            "milvus_health_9091": port_9091,
        },
        "rag": status,
        "checks": {
            "milvus_connected": bool(vector.get("connected")),
            "milvus_fallback_active": bool(vector.get("fallback")) and not bool(vector.get("connected")),
            "bge_active": embedding_real,
            "embedding_fallback_active": not embedding_real,
        },
        "next_actions": _next_actions(docker, port_19530, status),
    }


def _command_status(command: list[str], *, timeout: float) -> dict[str, Any]:
    binary = shutil.which(command[0])
    result: dict[str, Any] = {"available": bool(binary), "path": binary or "", "ok": False, "summary": ""}
    if not binary:
        result["summary"] = f"{command[0]} not found"
        return result
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:
        result["summary"] = str(exc)
        return result
    result["ok"] = completed.returncode == 0
    output = (completed.stdout or completed.stderr).strip().splitlines()
    result["summary"] = output[0] if output else f"exit={completed.returncode}"
    return result


def _tcp_check(host: str, port: int, *, timeout: float) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"host": host, "port": port, "open": True, "error": ""}
    except OSError as exc:
        return {"host": host, "port": port, "open": False, "error": str(exc)}


def _next_actions(docker: dict[str, Any], port_19530: dict[str, Any], status: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if not docker.get("ok"):
        actions.append("启动 Docker Desktop。")
    if not port_19530.get("open"):
        actions.append("执行 `docker compose -f docker-compose.milvus.yml up -d` 启动 Milvus。")
    if status.get("embedding_error"):
        actions.append("安装 BGE 依赖：`pip install -e '.[bge]'`，或临时切回 hashing embedding。")
    vector = status.get("vector_store", {})
    if vector.get("fallback") and not vector.get("connected"):
        actions.append("当前正在使用 in-memory fallback；启动 Milvus 后重新运行 `legal-agent rag-health`。")
    if not actions:
        actions.append("RAG 基础设施已就绪，可以运行合同审查或 benchmark。")
    return actions
