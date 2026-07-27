"""Interactive web UI for the independent Legal Agent Workbench.

服务层为 FastAPI + uvicorn：阻塞型业务端点用 def 定义（FastAPI 自动放入线程池，
不阻塞事件循环），SSE 事件流用 async def + asyncio.to_thread 轮询 hooks 事件总线，
把审查过程中的 Agent 事件实时推给浏览器。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from legalworkbench.documents import ContractDocumentStore
from legalworkbench.feishu_events import FeishuEventBridge
from legalworkbench.fs import atomic_write_text
from legalworkbench.lark_mcp import (
    DEFAULT_LEGAL_TOOLS,
    configure_lark_mcp,
    lark_mcp_status,
)
from legalworkbench.models import LegalSkill
from legalworkbench.paths import settings_path, skills_path
from legalworkbench.rag import (
    LegalRagService,
    clear_rag_service_cache,
    get_rag_service,
    lightweight_rag_status,
)
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.store import write_model_list
from legalworkbench.tasks import ReviewTaskQueue, ReviewTaskWorker


class LegalWorkbenchServer:
    def __init__(self, cwd: str | Path | None = None, *, host: str = "127.0.0.1", port: int = 5180) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.host = host
        self.port = port
        self.app = create_app(self.cwd)
        self.server: uvicorn.Server | None = None

    def serve_forever(self) -> None:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        print(f"Enterprise Legal Agent Workbench running at http://{self.host}:{self.port}/")
        self.server.run()

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread


def create_app(cwd: str | Path | None = None) -> FastAPI:
    cwd = Path(cwd or Path.cwd()).resolve()
    runtime = LegalAgentRuntime(cwd)
    documents = ContractDocumentStore(cwd)
    tasks = ReviewTaskQueue(cwd)
    feishu_events = FeishuEventBridge(cwd)
    app = FastAPI(title="Legal Agent Workbench", docs_url="/api/docs", openapi_url="/api/openapi.json")
    review_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lawbench-review")
    submitted_task_ids: set[str] = set()
    submitted_lock = threading.Lock()

    def run_review_task(task_id: str) -> None:
        try:
            task = tasks.claim(task_id)
            if task is None:
                return
            contract_path = str(task.get("contract_path") or "")
            if not contract_path:
                raise ValueError("contract_path required")
            run = runtime.review(
                contract_path,
                connect_mcp=bool(task.get("connect_mcp")),
            )
            task_status = "completed" if run.status in {"completed", "blocked"} else "failed"
            tasks.update(
                task_id,
                status=task_status,
                review_run_id=run.review_run_id,
                report_path=run.report_path,
                error=run.error if task_status == "failed" else "",
                completed_at=run.updated_at,
            )
        except Exception as exc:  # noqa: BLE001 - persisted for the operator
            tasks.update(task_id, status="failed", error=str(exc), completed_at=time.time())
        finally:
            with submitted_lock:
                submitted_task_ids.discard(task_id)

    def schedule_review_task(task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or "")
        if not task_id or task.get("status") != "pending":
            return
        with submitted_lock:
            if task_id in submitted_task_ids:
                return
            submitted_task_ids.add(task_id)
        review_executor.submit(run_review_task, task_id)

    def enqueue_review(
        record: dict[str, Any],
        *,
        dedup_key: str,
        connect_mcp: bool,
    ) -> JSONResponse:
        task = tasks.add(
            title=f"审查 {record.get('filename') or record.get('document_id') or '合同'}",
            source="web_async",
            contract_path=str(record.get("path") or ""),
            document_id=str(record.get("document_id") or ""),
            connect_mcp=connect_mcp,
            publish=False,
            dedup_key=dedup_key,
            auto_execute=True,
        )
        schedule_review_task(task)
        return JSONResponse({**task, "accepted": True}, status_code=202)

    def startup_background_services() -> None:
        # RAG model/index warming happens before the serial review queue, so the
        # first user request returns immediately instead of paying cold-start cost.
        review_executor.submit(get_rag_service, cwd)
        tasks.recover_stale_running(max_age_seconds=0.0, auto_only=True)
        for task in tasks.list():
            if task.get("auto_execute") and task.get("status") == "pending":
                schedule_review_task(task)

    def shutdown_background_services() -> None:
        review_executor.shutdown(wait=False, cancel_futures=True)

    app.router.add_event_handler("startup", startup_background_services)
    app.router.add_event_handler("shutdown", shutdown_background_services)

    def error(message: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return render_app_html()

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return {
            "runs": [run_summary(run) for run in runtime.store.list_runs(limit=20)],
            "skills": [skill.model_dump(mode="json") for skill in runtime.skills.list()],
            "documents": documents.list(limit=30),
            "tasks": tasks.list(),
            "task_summary": tasks.summary(),
            "workflow": runtime.workflow.describe(),
            "sessions": runtime.sessions.list_sessions(limit=20),
            "events": runtime.hooks.tail(limit=20),
            "rag": lightweight_rag_status(cwd),
            "connectors": runtime.connectors.context(connect_mcp=False),
            "lark": lark_mcp_status(cwd),
        }

    @app.get("/api/report/{run_id}")
    def api_report(run_id: str):
        run = runtime.store.load_run(run_id)
        if run is None:
            return error("run not found", 404)
        return {"review_run_id": run_id, "markdown": run.report_markdown}

    @app.get("/api/events/stream")
    async def api_events_stream(cycles: int = 0) -> StreamingResponse:
        # SSE：轮询 hooks 事件总线（文件型），有增量就推送快照，空闲时发心跳注释。
        # 文件读取放到线程池，避免阻塞事件循环。cycles>0 时发送 N 轮后关闭（测试用）
        async def event_stream():
            last_signature = ""
            sent = 0
            while True:
                events = await asyncio.to_thread(runtime.hooks.tail, limit=50)
                signature = f"{len(events)}:{json.dumps(events[-1], ensure_ascii=False, sort_keys=True) if events else ''}"
                if signature != last_signature:
                    last_signature = signature
                    payload = json.dumps({"events": events}, ensure_ascii=False)
                    yield f"event: events\ndata: {payload}\n\n"
                else:
                    yield ": heartbeat\n\n"
                sent += 1
                if cycles and sent >= cycles:
                    return
                await asyncio.sleep(1.0)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    @app.post("/api/init")
    def api_init() -> dict[str, str]:
        return {key: str(value) for key, value in runtime.init_samples().items()}

    @app.post("/api/review")
    def api_review(payload: dict[str, Any] = Body(default={})):
        text = str(payload.get("contract_text") or "").strip()
        if not text:
            return error("contract_text required")
        connect_mcp = bool(payload.get("connect_mcp"))
        dedup_key = hashlib.sha256(
            f"web-paste\0{int(connect_mcp)}\0{text}".encode("utf-8")
        ).hexdigest()
        existing = tasks.find_active(dedup_key)
        if existing is not None:
            schedule_review_task(existing)
            return JSONResponse(
                {**existing, "accepted": True, "deduplicated": True},
                status_code=202,
            )
        record = documents.save_text(
            filename="pasted-contract.md",
            text=text,
            source="web_paste",
        )
        return enqueue_review(
            record,
            dedup_key=dedup_key,
            connect_mcp=connect_mcp,
        )

    @app.post("/api/upload")
    def api_upload(payload: dict[str, Any] = Body(default={})):
        filename = str(payload.get("filename") or "contract.md")
        if payload.get("content_base64"):
            return documents.save_base64(filename=filename, content_base64=str(payload.get("content_base64")))
        text = str(payload.get("text") or "")
        if not text.strip():
            return error("text or content_base64 required")
        return documents.save_text(filename=filename, text=text)

    @app.post("/api/review-document")
    def api_review_document(payload: dict[str, Any] = Body(default={})):
        document_id = str(payload.get("document_id") or "")
        record = documents.get(document_id)
        if record is None:
            return error("document not found", 404)
        connect_mcp = bool(payload.get("connect_mcp"))
        dedup_key = hashlib.sha256(
            f"web-document\0{int(connect_mcp)}\0{document_id}".encode("utf-8")
        ).hexdigest()
        return enqueue_review(
            record,
            dedup_key=dedup_key,
            connect_mcp=connect_mcp,
        )

    @app.post("/api/skills")
    def api_skills(payload: dict[str, Any] = Body(default={})):
        skill = LegalSkill(
            name=str(payload.get("name") or "").strip(),
            contract_type=str(payload.get("contract_type") or "general").strip(),
            description=str(payload.get("description") or "").strip(),
            focus_clause_types=[item.strip() for item in str(payload.get("focus_clause_types") or "").split(",") if item.strip()],
            risk_rules=[item.strip() for item in str(payload.get("risk_rules") or "").split(",") if item.strip()],
            report_style=str(payload.get("report_style") or "concise").strip(),
            priority=int(payload.get("priority") or 50),
            retrieval_top_k=int(payload.get("retrieval_top_k") or 10),
            review_playbook=[item.strip() for item in str(payload.get("review_playbook") or "").replace("\n", "；").split("；") if item.strip()],
        )
        if not skill.name:
            return error("skill name required")
        skills = runtime.store.load_skills()
        skills = [item for item in skills if item.name != skill.name]
        skills.append(skill)
        write_model_list(skills_path(cwd), skills)
        return {"ok": True, "skill": skill.model_dump(mode="json")}

    @app.post("/api/mcp-server")
    def api_mcp_server(payload: dict[str, Any] = Body(default={})):
        name = str(payload.get("name") or "").strip()
        server_type = str(payload.get("type") or "http").strip()
        if not name:
            return error("server name required")
        if server_type not in {"http", "stdio"}:
            return error("server type must be http or stdio")
        current = _load_settings(cwd)
        servers = current.setdefault("mcp_servers", {})
        servers[name] = {
            "type": server_type,
            "description": str(payload.get("description") or ""),
            "url": str(payload.get("url") or ""),
        }
        atomic_write_text(settings_path(cwd), json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        return runtime.connectors.context(connect_mcp=True)

    @app.post("/api/lark-mcp")
    def api_lark_mcp(payload: dict[str, Any] = Body(default={})):
        app_id = str(payload.get("app_id") or "").strip()
        app_secret = str(payload.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            return error("app_id and app_secret required")
        raw_tools = payload.get("tools") or DEFAULT_LEGAL_TOOLS
        if isinstance(raw_tools, str):
            tools = [item.strip() for item in raw_tools.replace("\n", ",").split(",") if item.strip()]
        elif isinstance(raw_tools, list):
            tools = [str(item).strip() for item in raw_tools if str(item).strip()]
        else:
            tools = DEFAULT_LEGAL_TOOLS
        return configure_lark_mcp(
            cwd,
            server_name=str(payload.get("server_name") or "feishu_legal_workspace").strip(),
            app_id=app_id,
            app_secret=app_secret,
            domain=str(payload.get("domain") or "https://open.feishu.cn").strip(),
            tools=tools,
            oauth=bool(payload.get("oauth")),
            token_mode=str(payload.get("token_mode") or "auto").strip(),
            user_access_token=str(payload.get("user_access_token") or "").strip(),
        )

    @app.post("/api/lark-mcp-status")
    def api_lark_mcp_status(payload: dict[str, Any] = Body(default={})):
        return lark_mcp_status(cwd, server_name=str(payload.get("server_name") or "feishu_legal_workspace").strip())

    @app.post("/api/feishu/events")
    def api_feishu_events(request: Request, payload: dict[str, Any] = Body(default={})):
        result = feishu_events.handle(payload, headers={key: value for key, value in request.headers.items()})
        status = 200 if result.get("ok", True) or result.get("challenge") else 403
        return JSONResponse(result, status_code=status)

    @app.post("/api/feishu/event-test")
    def api_feishu_event_test(payload: dict[str, Any] = Body(default={})):
        text = str(payload.get("text") or "").strip()
        if not text:
            return error("text required")
        fake_event = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "test_open_id"}},
                "message": {
                    "message_id": f"msg_test_{int(threading.get_ident())}",
                    "chat_id": "test_chat_id",
                    "message_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            },
        }
        return feishu_events.handle(fake_event, trusted_source=True)

    @app.post("/api/rag-config")
    def api_rag_config(payload: dict[str, Any] = Body(default={})):
        current = _load_settings(cwd)
        current["rag"] = {
            "vector_backend": str(payload.get("vector_backend") or "local"),
            "milvus_uri": str(payload.get("milvus_uri") or "http://127.0.0.1:19530"),
            "collection": str(payload.get("collection") or "legal_clause_knowledge"),
            "embedding_provider": str(payload.get("embedding_provider") or "hashing"),
            "embedding_model": str(payload.get("embedding_model") or "BAAI/bge-small-zh-v1.5"),
            "embedding_device": str(payload.get("embedding_device") or "cpu"),
            "embedding_normalize": bool(payload.get("embedding_normalize", True)),
            "embedding_fallback": bool(payload.get("embedding_fallback", True)),
            "lexical_top_k": int(payload.get("lexical_top_k") or 32),
            "vector_top_k": int(payload.get("vector_top_k") or 32),
            "final_top_k": int(payload.get("final_top_k") or 10),
            "connect_timeout": float(payload.get("connect_timeout") or 1.0),
            "rerank_provider": str(payload.get("rerank_provider") or "formula"),
            "rerank_model": str(payload.get("rerank_model") or "BAAI/bge-reranker-base"),
        }
        atomic_write_text(settings_path(cwd), json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        clear_rag_service_cache()
        return LegalRagService(cwd).status()

    @app.post("/api/tasks")
    def api_tasks(payload: dict[str, Any] = Body(default={})):
        title = str(payload.get("title") or "").strip()
        document_id = str(payload.get("document_id") or "").strip()
        contract_path = str(payload.get("contract_path") or "").strip()
        source = str(payload.get("source") or "web")
        if document_id:
            record = documents.get(document_id)
            if record is None:
                return error("document not found", 404)
            contract_path = str(record.get("path") or "")
            source = str(record.get("source") or "document")
            title = title or f"审查 {record.get('filename') or document_id}"
        if title:
            if not contract_path:
                return error("contract_path or document_id required")
            return tasks.add(
                title=title,
                source=source,
                contract_path=contract_path,
                document_id=document_id,
                priority=int(payload.get("priority") or 50),
                connect_mcp=bool(payload.get("connect_mcp")),
            )
        return {"tasks": tasks.list(), "task_summary": tasks.summary()}

    @app.get("/api/tasks/{task_id}")
    def api_task(task_id: str):
        task = tasks.get(task_id)
        if task is None:
            return error("task not found", 404)
        return task

    @app.post("/api/tasks/delete")
    def api_tasks_delete(payload: dict[str, Any] = Body(default={})):
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            return error("task_id required")
        if not tasks.delete(task_id):
            return error("task not found", 404)
        return {"ok": True, "task_id": task_id, "tasks": tasks.list(), "task_summary": tasks.summary()}

    @app.post("/api/tasks/cleanup")
    def api_tasks_cleanup():
        deleted = tasks.delete_failed_without_contract()
        return {"ok": True, "deleted": deleted, "tasks": tasks.list(), "task_summary": tasks.summary()}

    @app.post("/api/worker/run-once")
    def api_worker_run_once(payload: dict[str, Any] = Body(default={})):
        result = ReviewTaskWorker(cwd).run_once(connect_mcp=bool(payload.get("connect_mcp")))
        summary = tasks.summary()
        if result is None:
            return {"status": "idle", "queue_summary": summary}
        result["queue_summary"] = summary
        result["remaining"] = summary["remaining"]
        return result

    @app.post("/api/eval")
    def api_eval():
        return {
            "standard": runtime.benchmark().model_dump(mode="json"),
            "human_annotated": runtime.human_benchmark().model_dump(mode="json"),
        }

    @app.post("/api/mcp-context")
    def api_mcp_context(payload: dict[str, Any] = Body(default={})):
        return runtime.connectors.context(connect_mcp=bool(payload.get("connect")))

    return app


def _load_settings(cwd: Path) -> dict[str, Any]:
    if not settings_path(cwd).exists():
        return {}
    try:
        current = json.loads(settings_path(cwd).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return current if isinstance(current, dict) else {}


def run_summary(run) -> dict[str, Any]:
    return {
        "review_run_id": run.review_run_id,
        "status": run.status,
        "contract_type": run.contract_type,
        "findings": len(run.findings),
        "high_risks": sum(1 for finding in run.findings if finding.risk_level == "high"),
        "memory_hits": len(run.memory_hits),
        "tool_calls": len(run.tool_calls),
        "reflection_checks": len(run.reflection_checks),
        "compact_retention": run.compact_snapshot.retention_rate if run.compact_snapshot else 1.0,
        "token_usage": run.token_usage,
        "metrics": run.metrics,
        "report_path": run.report_path,
    }


def render_app_html() -> str:
    sample = """# SaaS 服务协议样例

## 1. 服务内容
乙方向甲方提供在线软件服务，服务期限为一年。

## 2. 自动续约
服务期满后，本协议自动续约一年，除非双方另有书面约定。

## 3. 赔偿责任
乙方应赔偿甲方因此遭受的全部损失，包括直接损失、间接损失、预期利润损失，且不设赔偿责任上限。

## 4. 数据安全
乙方可处理甲方客户数据，但双方未约定数据泄露通知时限和安全措施标准。

## 5. 争议解决
双方同意由乙方所在地人民法院管辖。
"""
    html = Path(__file__).with_name("app_enterprise.html").read_text(encoding="utf-8")
    tools_text = "\n".join(DEFAULT_LEGAL_TOOLS)
    return (
        html.replace("__SAMPLE__", sample)
        .replace("__LARK_TOOLS__", tools_text)
        .replace("__LARK_TOOLS_JS__", json.dumps(DEFAULT_LEGAL_TOOLS, ensure_ascii=False))
    )
