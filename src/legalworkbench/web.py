"""Interactive web UI for the independent Legal Agent Workbench.

服务层为 FastAPI + uvicorn：阻塞型业务端点用 def 定义（FastAPI 自动放入线程池，
不阻塞事件循环），SSE 事件流用 async def + asyncio.to_thread 轮询 hooks 事件总线，
把审查过程中的 Agent 事件实时推给浏览器。
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from legalworkbench.documents import ContractDocumentStore
from legalworkbench.feishu_events import FeishuEventBridge
from legalworkbench.fs import atomic_write_text
from legalworkbench.lark_mcp import DEFAULT_LEGAL_TOOLS, configure_lark_mcp, lark_mcp_status
from legalworkbench.models import LegalSkill
from legalworkbench.paths import settings_path, skills_path
from legalworkbench.rag import LegalRagService, clear_rag_service_cache, lightweight_rag_status
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
        record = documents.save_text(
            filename="pasted-contract.md",
            text=text,
            source="web_paste",
        )
        run = runtime.review(
            record["path"], connect_mcp=bool(payload.get("connect_mcp"))
        )
        return run_summary(run)

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
        run = runtime.review(record["path"], connect_mcp=bool(payload.get("connect_mcp")))
        return run_summary(run)

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
        server_type = str(payload.get("type") or "mock").strip()
        if not name:
            return error("server name required")
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
    html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>企业法务 Agent 执行工作台</title>
<style>
:root{--bg:#eef2f6;--panel:#ffffff;--panel-soft:#f8fafc;--line:#d7e0ea;--line-strong:#c6d1df;--text:#111827;--muted:#647084;--blue:#1d4ed8;--green:#047857;--red:#b91c1c;--amber:#b45309;--dark:#172033;--shadow:0 1px 2px rgba(16,24,40,.06);--header-h:84px}
*{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}body{height:100vh;margin:0;overflow:hidden;background:var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{min-height:var(--header-h);background:var(--dark);color:white;padding:16px 24px;display:flex;justify-content:space-between;gap:18px;align-items:center;border-bottom:1px solid rgba(255,255,255,.08)}h1{margin:0;font-size:22px;line-height:1.25}h2{font-size:16px;margin:0 0 12px;line-height:1.3}h3{font-size:14px;margin:16px 0 8px;line-height:1.35}.sub{color:#cbd5e1;font-size:13px;margin-top:3px}.status{background:#0f766e;color:white;border-radius:999px;padding:6px 11px;font-size:12px;font-weight:800;box-shadow:inset 0 0 0 1px rgba(255,255,255,.16)}
main{height:calc(100vh - var(--header-h));display:grid;grid-template-columns:minmax(320px,350px) minmax(460px,1fr) minmax(320px,350px);gap:16px;align-items:stretch;max-width:1720px;margin:0 auto;padding:16px;overflow:hidden}section,.panel{min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;box-shadow:var(--shadow)}main>section{height:100%;min-height:0;overflow-y:auto;overscroll-behavior:contain}.stack{display:grid;gap:14px}.workspace-panel,.system-panel{align-content:start}.review-stage{display:flex;min-height:0;flex-direction:column;gap:14px;overflow:hidden}
.workspace-panel>div,.system-panel>div{min-width:0}.workspace-panel>div+div,.system-panel>div+div{border-top:1px solid var(--line);padding-top:14px}.system-panel>div>input,.system-panel>div>select,.system-panel>div>textarea,.workspace-panel>div>input,.workspace-panel>div>select,.workspace-panel>div>textarea{margin-bottom:8px}.system-panel .row{grid-template-columns:1fr;gap:8px;margin-bottom:8px}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.metric{border:1px solid var(--line);border-radius:8px;padding:11px 12px;background:var(--panel-soft)}.metric strong{display:block;font-size:23px;line-height:1.05;letter-spacing:0}.metric span{display:block;margin-top:4px;color:var(--muted);font-size:12px}.tabs{display:flex;gap:4px;min-width:0;overflow-x:auto;border-bottom:1px solid var(--line);padding:0 2px}.tab{flex:0 0 auto;border:0;border-radius:7px 7px 0 0;background:transparent;color:var(--muted);padding:9px 10px;cursor:pointer;font-weight:800}.tab.active{color:var(--blue);background:#eef5ff;border-bottom:2px solid var(--blue)}
textarea,input,select{width:100%;max-width:100%;border:1px solid var(--line-strong);border-radius:7px;padding:9px 10px;background:white;color:var(--text);font:13px/1.45 ui-sans-serif,system-ui}input[type="file"]{padding:10px;background:#fbfdff}textarea{min-height:116px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}#contract{min-height:260px;height:34vh;max-height:500px}#skillDesc,#larkTools{min-height:96px;max-height:180px}.row{display:grid;grid-template-columns:1fr 1fr;gap:9px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}button{max-width:100%;min-height:37px;border:0;border-radius:7px;background:var(--blue);color:white;padding:9px 12px;font-weight:800;cursor:pointer;white-space:nowrap}button:hover{filter:brightness(.96)}button.secondary{background:#475569}button.green{background:var(--green)}button.ghost{background:#eef2f7;color:#1f2937;border:1px solid #dce4ed}
.item{min-width:0;overflow:hidden;border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:8px;background:var(--panel-soft)}.item:hover{border-color:#9fb0c4}.item strong,.doc-title{display:block;min-width:0;overflow-wrap:anywhere;word-break:break-word;line-height:1.3}.doc-title{font-weight:800}.doc-meta{overflow-wrap:anywhere;word-break:break-word}.doc-actions{margin-top:9px}.badge{display:inline-block;border-radius:999px;background:#dbeafe;color:#1d4ed8;font-size:12px;font-weight:800;padding:3px 8px}.badge.red{background:#fee2e2;color:var(--red)}.badge.green{background:#dcfce7;color:var(--green)}.muted{color:var(--muted);font-size:13px}.small{font-size:12px}.split{display:grid;grid-template-columns:1fr 1fr;gap:12px}.hidden{display:none!important}.tabpane{min-width:0}.review-stage #report:not(.hidden){display:flex;flex:1;min-height:0}pre{width:100%;max-width:100%;min-height:0;margin:0;overflow:auto;white-space:pre-wrap;word-break:break-word;background:#fbfdff;color:#111827;border:1px solid var(--line);border-radius:8px;padding:14px;font:13px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace}.doc-list,.run-list,.event-list{max-height:330px;overflow-y:auto;overflow-x:hidden}.toolbar{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-bottom:8px}input[type="checkbox"]{width:auto;min-width:16px;height:16px;margin:0;vertical-align:middle}label.muted.small{display:flex;align-items:center;gap:8px;margin-top:8px;line-height:1.35}
@media(max-width:1220px){body{height:auto;min-height:100vh;overflow-y:auto}main{height:auto;min-height:calc(100vh - var(--header-h));grid-template-columns:minmax(300px,360px) minmax(0,1fr);align-items:start;overflow:visible}main>section{height:auto;overflow:visible}main>section:nth-child(3){grid-column:1/-1;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.review-stage{min-height:560px;overflow:visible}pre{min-height:420px}}
@media(max-width:900px){header{padding:14px 16px;align-items:flex-start;flex-direction:column}main{grid-template-columns:1fr;padding:10px}main>section:nth-child(3){display:grid;grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.split,.row{grid-template-columns:1fr}#contract{height:320px}.review-stage{min-height:auto}pre{min-height:360px}}
@media(max-width:560px){h1{font-size:20px}.metrics{grid-template-columns:1fr}.actions{display:grid;grid-template-columns:1fr}.actions button{width:100%}.toolbar button,.tab{width:auto}section,.panel{padding:12px}}
</style></head><body>
<header><div><h1>企业法务 Agent 执行工作台</h1><div class="sub">合同上传、RAG 检索、风险复核、报告生成、Skills 与 MCP 配置</div></div><div id="status" class="status">Ready</div></header>
<main>
<section class="stack workspace-panel">
  <div><h2>合同工作区</h2><input id="fileInput" type="file" accept=".txt,.md,.markdown,.pdf,.docx"><div class="actions"><button class="green" onclick="uploadAndReviewFile()">上传并审查</button><button class="ghost" onclick="uploadFile()">仅上传入库</button><button class="ghost" onclick="initSamples()">初始化样例</button></div><div class="muted small">支持 txt、md、pdf、docx；上传后会抽取正文并进入同一套 Agent 审查链路。</div></div>
  <div><h3>粘贴合同</h3><textarea id="contract">__SAMPLE__</textarea><label class="muted small"><input id="connectMcp" type="checkbox"> 审查时尝试连接 MCP</label><div class="actions"><button class="green" onclick="runReview()">审查粘贴文本</button><button class="secondary" onclick="runEval()">运行评测</button></div></div>
  <div><div class="toolbar"><h2>合同库</h2><button class="ghost" onclick="loadState()">刷新</button></div><div id="documents" class="doc-list"></div></div>
</section>
<section class="review-stage">
  <div class="metrics"><div class="metric"><strong id="runsCount">0</strong><span>审查任务</span></div><div class="metric"><strong id="riskCount">0</strong><span>风险发现</span></div><div class="metric"><strong id="memoryCount">0</strong><span>记忆命中</span></div><div class="metric"><strong id="toolCount">0</strong><span>工具调用</span></div></div>
  <div class="tabs"><button class="tab active" onclick="showTab('report')">报告</button><button class="tab" onclick="showTab('runsTab')">审查记录</button><button class="tab" onclick="showTab('workflowTab')">工作流</button><button class="tab" onclick="showTab('eventsTab')">审计事件</button></div>
  <div id="report" class="tabpane"><pre id="output">选择合同并开始审查，报告会显示在这里。</pre></div>
  <div id="runsTab" class="tabpane hidden"><div id="runs" class="run-list"></div></div>
  <div id="workflowTab" class="tabpane hidden"><div id="workflow"></div></div>
  <div id="eventsTab" class="tabpane hidden"><div id="events" class="event-list"></div></div>
</section>
<section class="stack system-panel">
  <div><h2>RAG / Milvus</h2><div id="ragStatus" class="item muted">加载中</div><div class="row"><select id="vectorBackend"><option value="local">local</option><option value="milvus">milvus</option></select><input id="milvusUri" value="http://127.0.0.1:19530"></div><input id="collection" value="legal_clause_knowledge"><div class="row"><select id="embeddingProvider"><option value="hashing">hashing fallback</option><option value="bge">BGE / sentence-transformers</option></select><input id="embeddingModel" value="BAAI/bge-small-zh-v1.5"></div><div class="row"><select id="embeddingDevice"><option value="cpu">cpu</option><option value="mps">mps</option><option value="cuda">cuda</option></select><label class="muted small"><input id="embeddingFallback" type="checkbox" checked> embedding 失败时降级</label></div><div class="actions"><button onclick="saveRag()">保存 RAG 配置</button></div></div>
  <div><h2>Skills</h2><input id="skillName" placeholder="skill name，例如 saas_agreement_review"><div class="row"><input id="skillType" placeholder="合同类型，例如 SaaS"><input id="skillStyle" placeholder="报告风格" value="risk-first"></div><div class="row"><input id="skillPriority" type="number" value="50" placeholder="优先级"><input id="skillTopK" type="number" value="10" placeholder="RAG TopK"></div><input id="skillFocus" placeholder="重点条款，例如 liability,data_security,sla"><input id="skillRules" placeholder="重点风险，例如 unlimited_liability,data_security"><textarea id="skillDesc" placeholder="技能描述 / 审查步骤；用分号或换行分隔"></textarea><div class="actions"><button onclick="addSkill()">添加 Skill</button></div><div id="skills"></div></div>
  <div><h2>飞书 / Lark MCP</h2><div id="larkStatus" class="item muted">未检查</div><input id="larkAppId" placeholder="App ID，例如 cli_xxxx"><input id="larkAppSecret" type="password" placeholder="App Secret，仅保存到本地 secrets"><div class="row"><select id="larkAuth"><option value="auto">应用身份 / auto</option><option value="oauth">用户 OAuth</option><option value="tenant_access_token">tenant_access_token</option><option value="user_access_token">user_access_token</option></select><input id="larkDomain" value="https://open.feishu.cn"></div><textarea id="larkTools">__LARK_TOOLS__</textarea><div class="actions"><button onclick="saveLarkMcp()">配置真实飞书 MCP</button><button class="secondary" onclick="mcpContext()">连接检查</button></div></div>
  <div><h2>MCP / 企业连接器</h2><input id="mcpName" placeholder="server name，例如 feishu_legal_prod"><div class="row"><select id="mcpType"><option value="mock">mock</option><option value="http">http</option><option value="stdio">stdio</option></select><input id="mcpUrl" placeholder="url 或 command"></div><input id="mcpDesc" placeholder="用途描述"><div class="actions"><button onclick="addMcp()">添加连接器</button><button class="secondary" onclick="mcpContext()">查看 MCP 状态</button></div><div id="connectors"></div></div>
  <div><h2>任务队列</h2><div id="taskSummary" class="item muted">暂无队列状态</div><input id="taskTitle" placeholder="选择合同后可改任务标题"><div class="actions"><button class="secondary" onclick="runWorkerOnce()">执行一个任务</button><button class="ghost" onclick="cleanupTasks()">清理无效失败任务</button></div><div class="muted small">用于大文件、批量合同、飞书附件等后台审查；任务必须绑定具体合同。</div><div id="tasks"></div></div>
</section>
</main>
<script>
let currentState = {};
async function api(path, options={}){const res=await fetch(path,{headers:{'content-type':'application/json'},...options});if(!res.ok)throw new Error(await res.text());return res.json();}
function setStatus(text){document.getElementById('status').textContent=text;}
function showTab(id){document.querySelectorAll('.tabpane').forEach(x=>x.classList.add('hidden'));document.getElementById(id).classList.remove('hidden');document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));event.target.classList.add('active');}
async function loadState(){currentState=await api('/api/state');renderAll(currentState);}
function renderAll(state){renderRuns(state.runs||[]);renderDocuments(state.documents||[]);renderWorkflow(state.workflow||[]);renderSkills(state.skills||[]);renderConnectors(state.connectors||{});renderTaskSummary(state.task_summary||summarizeTasks(state.tasks||[]));renderTasks(state.tasks||[]);renderEvents(state.events||[]);renderRag(state.rag||{});renderLark(state.lark||{});}
function renderRuns(runItems){runsCount.textContent=runItems.length;riskCount.textContent=runItems.reduce((a,r)=>a+r.findings,0);memoryCount.textContent=runItems.reduce((a,r)=>a+r.memory_hits,0);toolCount.textContent=runItems.reduce((a,r)=>a+r.tool_calls,0);document.getElementById('runs').innerHTML=runItems.map(r=>`<div class="item"><code>${r.review_run_id}</code> <span class="badge ${r.status==='completed'?'green':''}">${r.status}</span><div class="muted">${r.contract_type} · risks=${r.findings} · high=${r.high_risks} · memory=${r.memory_hits} · tools=${r.tool_calls} · reflection=${r.reflection_checks}</div><button class="ghost" onclick="loadReport('${r.review_run_id}')">查看报告</button></div>`).join('')||'<p class="muted">暂无审查记录</p>';}
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function displayDocName(d){const name=String(d.filename||'合同');if(name.startsWith('feishu_om_'))return `飞书消息合同 · ${d.document_id}`;return name;}
function renderDocuments(docs){documents.innerHTML=docs.map(d=>`<div class="item"><strong class="doc-title" title="${escapeHtml(d.filename)}">${escapeHtml(displayDocName(d))}</strong><div class="muted doc-meta">${escapeHtml(d.document_id)} · ${Number(d.chars||0)} chars · ${escapeHtml(d.status||'')}</div><div class="doc-actions actions"><button class="green" onclick="reviewDocument('${escapeHtml(d.document_id)}')">立即审查</button><button class="ghost" onclick="queueDocument('${escapeHtml(d.document_id)}')">加入队列</button></div></div>`).join('')||'<p class="muted">暂无上传合同</p>';}
function renderWorkflow(steps){workflow.innerHTML=steps.map(s=>`<div class="item"><strong>${s.role}</strong> <span class="badge">${s.tool}</span><div class="muted">${s.description}</div></div>`).join('');}
function renderSkills(skills){document.getElementById('skills').innerHTML=skills.map(s=>`<div class="item"><strong>${s.name}</strong><div class="muted">${s.contract_type} · topK=${s.retrieval_top_k||10} · ${s.risk_rules.join(', ')||'无重点风险'}</div><div class="small muted">${(s.focus_clause_types||[]).join(', ')}</div></div>`).join('');}
function renderConnectors(c){const tools=[...(c.builtin_tools||[]),...(c.tools||[])];const failed=(c.failed||[]).map(f=>`<div class="badge red">${f.server}: ${f.error}</div>`).join(' ');connectors.innerHTML=`<div class="muted">configured=${(c.configured_servers||[]).length} · connected=${(c.connected||[]).length} · mocked=${(c.mocked||[]).length||0} · tools=${tools.length}</div>${failed}`+tools.slice(0,8).map(t=>`<div class="item"><strong>${t.server}.${t.name}</strong><div class="muted">${t.description}</div></div>`).join('');}
function renderLark(l){const node=l.node||{};const ok=l.configured&&l.app_secret_configured&&l.npx_path&&node.ok;const cls=ok?'green':'red';larkStatus.innerHTML=`<span class="badge ${cls}">${ok?'ready':'needs config'}</span><div class="muted">configured=${!!l.configured} · secret=${!!l.app_secret_configured} · npx=${l.npx_path||'missing'} · node=${node.version||'missing'}</div><div class="muted">auth=${l.auth_mode||'mock'} · tools=${(l.tools||[]).length}</div><div class="small">${(l.next_actions||[]).join('；')}</div>`;larkTools.value=(l.tools&&l.tools.length?l.tools:__LARK_TOOLS_JS__).join('\\n');larkDomain.value=l.domain||'https://open.feishu.cn';}
function summarizeTasks(items){const summary={total:items.length,pending:0,running:0,completed:0,failed:0,remaining:0,next_tasks:[],failed_tasks:[],recent_completed:[]};items.forEach(t=>{const s=String(t.status||'unknown');summary[s]=(summary[s]||0)+1;});summary.remaining=(summary.pending||0)+(summary.running||0);summary.next_tasks=items.filter(t=>['running','pending'].includes(String(t.status||''))).slice(0,5);summary.failed_tasks=items.filter(t=>String(t.status||'')==='failed').slice(0,5);summary.recent_completed=items.filter(t=>String(t.status||'')==='completed').slice(0,5);return summary;}
function taskStatusLabel(status){return ({pending:'待执行',running:'执行中',completed:'已完成',failed:'失败',idle:'空闲'})[status]||status;}
function taskBadge(status){const cls=status==='completed'?'green':(status==='failed'?'red':'');return `<span class="badge ${cls}">${escapeHtml(taskStatusLabel(status))}</span>`;}
function renderTaskSummary(summary){const next=summary.next_tasks||[];const failed=summary.failed_tasks||[];const nextHtml=next.length?next.map(t=>`<div class="small">${taskBadge(t.status)} ${escapeHtml(t.title||t.task_id)} <span class="muted">${escapeHtml(t.task_id||'')}</span></div>`).join(''):'<div class="small muted">没有待执行任务</div>';const failedHtml=failed.length?`<div class="small">${failed.length} 个失败任务需要处理</div>`:'';taskSummary.innerHTML=`<div><strong>剩余 ${Number(summary.remaining||0)} 个</strong> <span class="muted">总计 ${Number(summary.total||0)} · 待执行 ${Number(summary.pending||0)} · 执行中 ${Number(summary.running||0)} · 失败 ${Number(summary.failed||0)} · 已完成 ${Number(summary.completed||0)}</span></div><div class="muted small" style="margin-top:6px">下一批：</div>${nextHtml}${failedHtml}`;}
function renderTasks(items){const order={running:0,pending:1,failed:2,completed:3};const sorted=[...items].sort((a,b)=>(order[String(a.status||'')]??9)-(order[String(b.status||'')]??9)||Number(b.updated_at||0)-Number(a.updated_at||0));tasks.innerHTML=sorted.map(t=>{const hasContract=!!String(t.contract_path||'');const legacy=!hasContract&&!t.document_id;const run=t.review_run_id?`<button class="ghost" onclick="loadReport('${escapeHtml(t.review_run_id)}')">查看报告</button>`:'';const execute=t.status==='pending'?'<button class="secondary" onclick="runWorkerOnce()">执行队列</button>':'';const remove=`<button class="ghost" onclick="deleteTask('${escapeHtml(t.task_id)}')">删除</button>`;const err=t.error?`<div class="badge red">${escapeHtml(t.error)}</div>`:'';const attempts=(t.attempts===undefined||t.max_attempts===undefined)?'历史任务':`attempts=${t.attempts}/${t.max_attempts}`;const hint=legacy?'<div class="muted small">旧任务未绑定合同，请从合同库选择“加入队列”。</div>':'';return `<div class="item"><strong>${escapeHtml(t.title)}</strong><div class="muted">${escapeHtml(t.task_id)} · ${taskBadge(t.status)} · p${t.priority} · ${attempts}</div><div class="muted doc-meta">${hasContract?escapeHtml(t.contract_path):'未绑定合同文件'} ${escapeHtml(t.document_id||'')}</div>${hint}${err}<div class="actions">${execute}${run}${remove}</div></div>`}).join('')||'<p class="muted">暂无任务</p>';}
function renderEvents(items){events.innerHTML=items.map(e=>`<div class="item"><strong>${e.name}</strong><div class="muted">${e.review_run_id}</div><code class="small">${JSON.stringify(e.payload)}</code></div>`).join('')||'<p class="muted">暂无事件</p>';}
function renderRag(rag){const store=rag.vector_store||{};const err=rag.embedding_error?`<div class="badge red">${rag.embedding_error}</div>`:'';ragStatus.innerHTML=`<strong>${store.backend||'unknown'}</strong><div>embedding=${rag.embedding_model||''} · indexed=${rag.indexed_entries||0} · connected=${store.connected}</div><div class="muted">${store.uri||''} ${store.collection||''}</div>${err}`;const cfg=rag.config||{};vectorBackend.value=cfg.vector_backend||'local';milvusUri.value=cfg.milvus_uri||'http://127.0.0.1:19530';collection.value=cfg.collection||'legal_clause_knowledge';embeddingProvider.value=cfg.embedding_provider||'hashing';embeddingModel.value=cfg.embedding_model||'BAAI/bge-small-zh-v1.5';embeddingDevice.value=cfg.embedding_device||'cpu';embeddingFallback.checked=cfg.embedding_fallback!==false;}
async function initSamples(){setStatus('初始化中');output.textContent=JSON.stringify(await api('/api/init',{method:'POST',body:'{}'}),null,2);await loadState();setStatus('Ready');}
async function runReview(){setStatus('审查中');const r=await api('/api/review',{method:'POST',body:JSON.stringify({contract_text:contract.value,connect_mcp:connectMcp.checked})});await loadState();await loadReport(r.review_run_id);setStatus('完成 '+r.review_run_id);}
async function reviewDocument(id){setStatus('审查上传合同');const r=await api('/api/review-document',{method:'POST',body:JSON.stringify({document_id:id,connect_mcp:connectMcp.checked})});await loadState();await loadReport(r.review_run_id);setStatus('完成 '+r.review_run_id);}
async function readSelectedFile(){const file=fileInput.files[0];if(!file)throw new Error('请选择文件');const textTypes=['.txt','.md','.markdown'];const lower=file.name.toLowerCase();if(textTypes.some(x=>lower.endsWith(x))){return {filename:file.name,text:await file.text()};}const buf=await file.arrayBuffer();let binary='';new Uint8Array(buf).forEach(b=>binary+=String.fromCharCode(b));return {filename:file.name,content_base64:btoa(binary)};}
async function uploadFile(){try{setStatus('上传中');const record=await api('/api/upload',{method:'POST',body:JSON.stringify(await readSelectedFile())});output.textContent=JSON.stringify(record,null,2);await loadState();setStatus('上传完成 '+record.document_id);}catch(e){setStatus('上传失败');output.textContent=e.message;}}
async function uploadAndReviewFile(){try{setStatus('上传并审查中');const record=await api('/api/upload',{method:'POST',body:JSON.stringify(await readSelectedFile())});await loadState();const r=await api('/api/review-document',{method:'POST',body:JSON.stringify({document_id:record.document_id,connect_mcp:connectMcp.checked})});await loadState();await loadReport(r.review_run_id);setStatus('完成 '+r.review_run_id);}catch(e){setStatus('上传或审查失败');output.textContent=e.message;}}
async function loadReport(id){showReport();const r=await api('/api/report/'+id);output.textContent=r.markdown;}
function showReport(){document.querySelectorAll('.tabpane').forEach(x=>x.classList.add('hidden'));report.classList.remove('hidden');document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.querySelector('.tab').classList.add('active');}
async function runEval(){setStatus('评测中');output.textContent=JSON.stringify(await api('/api/eval',{method:'POST',body:'{}'}),null,2);setStatus('Ready');}
async function saveRag(){output.textContent=JSON.stringify(await api('/api/rag-config',{method:'POST',body:JSON.stringify({vector_backend:vectorBackend.value,milvus_uri:milvusUri.value,collection:collection.value,embedding_provider:embeddingProvider.value,embedding_model:embeddingModel.value,embedding_device:embeddingDevice.value,embedding_fallback:embeddingFallback.checked})}),null,2);await loadState();}
async function addSkill(){const payload={name:skillName.value,contract_type:skillType.value,report_style:skillStyle.value,priority:Number(skillPriority.value||50),retrieval_top_k:Number(skillTopK.value||10),focus_clause_types:skillFocus.value,risk_rules:skillRules.value,description:skillDesc.value,review_playbook:skillDesc.value};output.textContent=JSON.stringify(await api('/api/skills',{method:'POST',body:JSON.stringify(payload)}),null,2);await loadState();}
async function addMcp(){const payload={name:mcpName.value,type:mcpType.value,url:mcpUrl.value,description:mcpDesc.value};output.textContent=JSON.stringify(await api('/api/mcp-server',{method:'POST',body:JSON.stringify(payload)}),null,2);await loadState();}
async function saveLarkMcp(){const auth=larkAuth.value;const payload={server_name:'feishu_legal_workspace',app_id:larkAppId.value,app_secret:larkAppSecret.value,domain:larkDomain.value,tools:larkTools.value,oauth:auth==='oauth',token_mode:auth==='oauth'?'auto':auth};output.textContent=JSON.stringify(await api('/api/lark-mcp',{method:'POST',body:JSON.stringify(payload)}),null,2);larkAppSecret.value='';await loadState();}
async function mcpContext(){output.textContent=JSON.stringify(await api('/api/mcp-context',{method:'POST',body:JSON.stringify({connect:true})}),null,2);}
async function addTask(){output.textContent=JSON.stringify(await api('/api/tasks',{method:'POST',body:JSON.stringify({title:taskTitle.value,source:'web',priority:50})}),null,2);taskTitle.value='';await loadState();}
async function queueDocument(id){setStatus('加入队列');const payload={document_id:id,title:taskTitle.value,source:'web',priority:50,connect_mcp:connectMcp.checked};const task=await api('/api/tasks',{method:'POST',body:JSON.stringify(payload)});output.textContent=JSON.stringify(task,null,2);taskTitle.value='';await loadState();setStatus('已入队 '+task.task_id+'，点击任务队列里的“执行队列”');}
async function runWorkerOnce(){setStatus('执行队列中');const result=await api('/api/worker/run-once',{method:'POST',body:JSON.stringify({connect_mcp:connectMcp.checked})});output.textContent=JSON.stringify(result,null,2);await loadState();const summary=result.queue_summary||currentState.task_summary||{};const remaining=Number(summary.remaining||0);if(result.review_run_id){await loadReport(result.review_run_id);setStatus('队列完成 '+result.review_run_id+' · 剩余 '+remaining+' 个');}else{setStatus(remaining?'队列待处理 '+remaining+' 个':'队列空闲');}}
async function deleteTask(id){setStatus('删除任务');output.textContent=JSON.stringify(await api('/api/tasks/delete',{method:'POST',body:JSON.stringify({task_id:id})}),null,2);await loadState();setStatus('已删除 '+id);}
async function cleanupTasks(){setStatus('清理任务');output.textContent=JSON.stringify(await api('/api/tasks/cleanup',{method:'POST',body:'{}'}),null,2);await loadState();setStatus('清理完成');}
loadState().catch(e=>setStatus(e.message));
try{const es=new EventSource('/api/events/stream');es.addEventListener('events',e=>{try{const d=JSON.parse(e.data);renderEvents((d.events||[]).slice().reverse());}catch(_){}});}catch(_){}
</script></body></html>"""
    tools_text = "\n".join(DEFAULT_LEGAL_TOOLS)
    return (
        html.replace("__SAMPLE__", sample)
        .replace("__LARK_TOOLS__", tools_text)
        .replace("__LARK_TOOLS_JS__", json.dumps(DEFAULT_LEGAL_TOOLS, ensure_ascii=False))
    )
