"""CLI for the independent Legal Agent Workbench project."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from legalworkbench.evals import BaselineEvaluator, format_baseline_table
from legalworkbench.fs import atomic_write_text
from legalworkbench.feishu_events import FeishuEventBridge, write_event_setup_guide
from legalworkbench.feishu_stream import FeishuLongConnectionListener
from legalworkbench.lark_mcp import DEFAULT_LEGAL_TOOLS, configure_lark_mcp, lark_login_command, lark_mcp_status
from legalworkbench.mcp import McpConnectorRegistry
from legalworkbench.memory import LegalMemoryStore
from legalworkbench.rag import LegalRagService
from legalworkbench.rag.health import rag_health
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.paths import settings_path
from legalworkbench.secrets import load_secrets, save_secrets
from legalworkbench.security_cli import register_security_commands
from legalworkbench.tasks import ReviewTaskQueue, ReviewTaskWorker
from legalworkbench.web import LegalWorkbenchServer

app = typer.Typer(name="legal-agent", help="Enterprise Legal Agent Workbench", add_completion=False)
register_security_commands(app)


@app.command("init")
def init_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    force: bool = typer.Option(False, "--force", help="Overwrite sample data"),
) -> None:
    paths = LegalAgentRuntime(cwd).init_samples(force=force)
    print("Initialized Enterprise Legal Agent Workbench:")
    for key, value in paths.items():
        print(f"  {key}: {value}")


@app.command("review")
def review_cmd(
    contract: str = typer.Argument(..., help="Contract file path"),
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    connect_mcp: bool = typer.Option(False, "--connect-mcp", help="Use configured MCP connectors"),
) -> None:
    run = LegalAgentRuntime(cwd).review(contract, connect_mcp=connect_mcp)
    print(f"{run.review_run_id} -> {run.status}")
    print(f"contract_type: {run.contract_type}")
    print(f"findings: {len(run.findings)}")
    print(f"memory_hits: {len(run.memory_hits)}")
    print(f"tool_calls: {len(run.tool_calls)}")
    print(f"report: {run.report_path}")


@app.command("runs")
def runs_cmd(cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root")) -> None:
    runs = LegalAgentRuntime(cwd).store.list_runs()
    if not runs:
        print("No review runs.")
        return
    for run in runs:
        print(f"{run.review_run_id} [{run.status}] {run.contract_type} risks={len(run.findings)} report={run.report_path}")


@app.command("report")
def report_cmd(
    run_id: str = typer.Argument(..., help="Review run id"),
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
) -> None:
    run = LegalAgentRuntime(cwd).store.load_run(run_id)
    if run is None:
        raise typer.BadParameter(f"Review run not found: {run_id}")
    print(run.report_markdown)


@app.command("eval")
def eval_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    scaled: bool = typer.Option(False, "--scaled", help="Generate 80+/300+ scaled benchmark before running"),
    human: bool = typer.Option(False, "--human", help="Run 30-contract human-annotated benchmark"),
) -> None:
    runtime = LegalAgentRuntime(cwd)
    if scaled:
        path = runtime.store.write_scaled_benchmark()
        print(f"Generated scaled benchmark: {path}")
    result = runtime.human_benchmark() if human else runtime.benchmark()
    print("Legal benchmark result:")
    for key, value in result.model_dump().items():
        print(f"  {key}: {value}")


@app.command("eval-baseline")
def eval_baseline_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    dataset: str = typer.Option("both", "--dataset", help="synthetic, human, or both"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    if dataset not in {"synthetic", "human", "both"}:
        raise typer.BadParameter("--dataset must be synthetic, human, or both")
    rows = BaselineEvaluator(cwd).run(dataset=dataset)  # type: ignore[arg-type]
    if json_output:
        print(json.dumps([row.to_dict() for row in rows], ensure_ascii=False, indent=2))
        return
    print("Baseline comparison:")
    print(format_baseline_table(rows))


@app.command("tools")
def tools_cmd(cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root")) -> None:
    runtime = LegalAgentRuntime(cwd)
    for tool in runtime.tools.list_tools():
        print(f"{tool.name}: {tool.description}")


@app.command("rag-status")
def rag_status_cmd(cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root")) -> None:
    print(json.dumps(LegalRagService(cwd).status(), ensure_ascii=False, indent=2))


@app.command("rag-config")
def rag_config_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    vector_backend: str = typer.Option("milvus", "--vector-backend", help="local or milvus"),
    milvus_uri: str = typer.Option("http://127.0.0.1:19530", "--milvus-uri", help="Milvus URI"),
    collection: str = typer.Option("legal_clause_knowledge", "--collection", help="Milvus collection"),
    embedding_provider: str = typer.Option("bge", "--embedding-provider", help="hashing or bge"),
    embedding_model: str = typer.Option("BAAI/bge-small-zh-v1.5", "--embedding-model", help="Sentence-Transformers model"),
    embedding_device: str = typer.Option("cpu", "--embedding-device", help="cpu, cuda, or mps"),
    no_embedding_fallback: bool = typer.Option(False, "--no-embedding-fallback", help="Fail instead of falling back to hashing"),
    rerank_provider: str = typer.Option("formula", "--rerank-provider", help="formula or cross_encoder"),
    rerank_model: str = typer.Option("BAAI/bge-reranker-base", "--rerank-model", help="Cross-encoder rerank model"),
    fusion: str = typer.Option("score", "--fusion", help="score (加权融合) or rrf (reciprocal rank fusion)"),
) -> None:
    current = _load_settings(cwd)
    current["rag"] = {
        **(current.get("rag", {}) if isinstance(current.get("rag"), dict) else {}),
        "vector_backend": vector_backend,
        "milvus_uri": milvus_uri,
        "collection": collection,
        "embedding_provider": embedding_provider,
        "embedding_model": embedding_model,
        "embedding_device": embedding_device,
        "embedding_normalize": True,
        "embedding_fallback": not no_embedding_fallback,
        "rerank_provider": rerank_provider,
        "rerank_model": rerank_model,
        "fusion": fusion,
    }
    atomic_write_text(settings_path(cwd), json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    print(f"Updated RAG config: {settings_path(cwd)}")
    print(json.dumps(LegalRagService(cwd).status(), ensure_ascii=False, indent=2))


@app.command("rag-health")
def rag_health_cmd(cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root")) -> None:
    print(json.dumps(rag_health(cwd), ensure_ascii=False, indent=2))


@app.command("workflow")
def workflow_cmd(cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root")) -> None:
    runtime = LegalAgentRuntime(cwd)
    for step in runtime.workflow.describe():
        print(f"{step['name']}: {step['role']} -> {step['tool']} ({step['description']})")


@app.command("sessions")
def sessions_cmd(cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root")) -> None:
    sessions = LegalAgentRuntime(cwd).sessions.list_sessions()
    if not sessions:
        print("No review sessions.")
        return
    for session in sessions:
        print(
            f"{session['session_id']} [{session['status']}] "
            f"{session['contract_type']} risks={session['finding_count']} tools={session['tool_call_count']}"
        )


@app.command("events")
def events_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    limit: int = typer.Option(20, "--limit", help="Number of events"),
) -> None:
    runtime = LegalAgentRuntime(cwd)
    events = runtime.hooks.tail(limit=limit)
    if not events:
        print("No hook events.")
        return
    for event in events:
        print(f"{event['review_run_id']} {event['name']} {event.get('payload', {})}")


@app.command("compact")
def compact_cmd(
    run_id: str = typer.Argument(..., help="Review run id"),
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
) -> None:
    runtime = LegalAgentRuntime(cwd)
    run = runtime.store.load_run(run_id)
    if run is None:
        raise typer.BadParameter(f"Review run not found: {run_id}")
    if run.compact_snapshot is None:
        print("No compact snapshot.")
        return
    snapshot = run.compact_snapshot
    print(f"{snapshot.snapshot_id}: {snapshot.source_tokens} -> {snapshot.retained_tokens} retention={snapshot.retention_rate}")
    print(snapshot.summary)


@app.command("memory")
def memory_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    export_jsonl: bool = typer.Option(False, "--export-jsonl", help="Export memory as jsonl"),
) -> None:
    memory = LegalMemoryStore(cwd)
    if export_jsonl:
        print(f"Exported memory: {memory.export_jsonl()}")
        return
    index = memory.write_index()
    print(f"Memory entries: {len(memory.list())}")
    print(f"Memory index: {index}")


@app.command("tasks")
def tasks_cmd(
    title: str | None = typer.Argument(None, help="Optional task title to add"),
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    source: str = typer.Option("manual", "--source", help="Task source"),
) -> None:
    queue = ReviewTaskQueue(cwd)
    if title:
        task = queue.add(title=title, source=source)
        print(f"Added task: {task['task_id']}")
    summary = queue.summary()
    print(
        "Queue summary: "
        f"remaining={summary['remaining']} "
        f"pending={summary['pending']} "
        f"running={summary['running']} "
        f"failed={summary['failed']} "
        f"completed={summary['completed']} "
        f"total={summary['total']}"
    )
    for task in queue.list():
        print(f"{task['task_id']} [{task['status']}] p{task['priority']} {task['title']}")


@app.command("worker")
def worker_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    once: bool = typer.Option(False, "--once", help="Process one pending task and exit"),
    connect_mcp: bool = typer.Option(False, "--connect-mcp", help="Use configured MCP connectors"),
) -> None:
    worker = ReviewTaskWorker(cwd)
    if once:
        result = worker.run_once(connect_mcp=connect_mcp)
        print(json.dumps(result or {"status": "idle"}, ensure_ascii=False, indent=2))
        return
    worker.run_loop(connect_mcp=connect_mcp)


@app.command("llm-config")
def llm_config_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    provider: str = typer.Option("ollama", "--provider", help="local, ollama, or openai_compatible"),
    model: str = typer.Option("qwen2.5:7b", "--model", help="Model name"),
    base_url: str = typer.Option("", "--base-url", help="OpenAI-compatible base URL (ollama defaults to http://127.0.0.1:11434/v1)"),
    api_key: str = typer.Option("", "--api-key", help="API key, stored in secrets.json (not settings.json)"),
    timeout: float = typer.Option(30.0, "--timeout", help="Request timeout seconds"),
) -> None:
    if provider not in {"local", "ollama", "openai_compatible"}:
        raise typer.BadParameter("--provider must be local, ollama, or openai_compatible")
    if provider == "openai_compatible" and not base_url:
        raise typer.BadParameter("--base-url is required for openai_compatible")
    current = _load_settings(cwd)
    current["llm"] = {"provider": provider, "model": model, "base_url": base_url, "timeout_seconds": timeout}
    atomic_write_text(settings_path(cwd), json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    if api_key:
        secrets = load_secrets(cwd)
        secrets["llm_api_key"] = api_key
        save_secrets(secrets, cwd)
    from legalworkbench.llm import LlmClient, load_llm_config

    client = LlmClient(load_llm_config(cwd))
    print(f"Updated LLM config: provider={provider} model={model}")
    print(f"remote_endpoint: {client.remote_endpoint() or 'local deterministic fallback'}")
    try:
        probe = client.decide(task="plan_review", payload={"contract_type": "SaaS", "clause_titles": ["测试"]}, fallback={"adjust": False})
        print(f"probe decision_source: {probe.get('decision_source', 'unknown')}")
    except Exception as exc:  # noqa: BLE001
        print(f"probe failed: {exc}")


@app.command("queue-config")
def queue_config_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    backend: str = typer.Option("redis", "--backend", help="local or redis"),
    redis_url: str = typer.Option("redis://127.0.0.1:6379/0", "--redis-url", help="Redis URL"),
    max_attempts: int = typer.Option(3, "--max-attempts", help="Max delivery attempts before DLQ"),
    consumer_group: str = typer.Option("review_workers", "--consumer-group", help="Stream consumer group"),
    claim_idle_ms: int = typer.Option(900_000, "--claim-idle-ms", help="Reclaim un-ACKed messages after this idle time"),
    dedup_ttl: int = typer.Option(86_400, "--dedup-ttl", help="Enqueue dedup key TTL in seconds"),
) -> None:
    if backend not in {"local", "redis"}:
        raise typer.BadParameter("--backend must be local or redis")
    current = _load_settings(cwd)
    current["redis"] = {**(current.get("redis", {}) if isinstance(current.get("redis"), dict) else {}), "url": redis_url}
    current["queue"] = {
        **(current.get("queue", {}) if isinstance(current.get("queue"), dict) else {}),
        "backend": backend,
        "consumer_group": consumer_group,
        "max_attempts": max_attempts,
        "claim_idle_ms": claim_idle_ms,
        "dedup_ttl_seconds": dedup_ttl,
    }
    atomic_write_text(settings_path(cwd), json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    print(f"Updated queue config: {settings_path(cwd)}")
    from legalworkbench.mq import create_task_bus

    print(json.dumps(create_task_bus(cwd).health(), ensure_ascii=False, indent=2))


@app.command("queue-health")
def queue_health_cmd(cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root")) -> None:
    from legalworkbench.mq import create_task_bus

    bus = create_task_bus(cwd)
    payload = {"bus": bus.health(), "task_store": ReviewTaskQueue(cwd).summary()}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("queue-dlq")
def queue_dlq_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    limit: int = typer.Option(20, "--limit", help="Number of DLQ entries to show"),
    requeue_all: bool = typer.Option(False, "--requeue-all", help="Requeue all dead-lettered tasks"),
) -> None:
    from legalworkbench.mq import create_task_bus

    bus = create_task_bus(cwd)
    if requeue_all:
        print(f"Requeued {bus.dlq_requeue_all()} dead-lettered task(s).")
        return
    entries = bus.dlq_list(limit=limit)
    if not entries:
        print("Dead letter queue is empty.")
        return
    for entry in entries:
        print(json.dumps(entry, ensure_ascii=False))


@app.command("dashboard")
def dashboard_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    output: str | None = typer.Option(None, "--output", help="Output JSON path"),
) -> None:
    path = LegalAgentRuntime(cwd).export_dashboard(output)
    print(f"Exported dashboard: {path}")
    print(f"HTML dashboard: {path.with_suffix('.html')}")


@app.command("mcp-context")
def mcp_context_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    connect: bool = typer.Option(False, "--connect", help="Preview connector tools/resources"),
) -> None:
    runtime = LegalAgentRuntime(cwd)
    print(json.dumps(runtime.connectors.context(connect_mcp=connect), ensure_ascii=False, indent=2))


@app.command("lark-mcp")
def lark_mcp_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    app_id: str = typer.Option("", "--app-id", help="Feishu/Lark Open Platform App ID"),
    app_secret: str = typer.Option("", "--app-secret", help="Feishu/Lark Open Platform App Secret"),
    server_name: str = typer.Option("feishu_legal_workspace", "--server-name", help="MCP server name"),
    domain: str = typer.Option("https://open.feishu.cn", "--domain", help="Feishu/Lark API domain"),
    tools: str = typer.Option(",".join(DEFAULT_LEGAL_TOOLS), "--tools", help="Comma-separated Lark MCP tools"),
    oauth: bool = typer.Option(False, "--oauth", help="Use OAuth user authorization"),
    token_mode: str = typer.Option("auto", "--token-mode", help="auto, tenant_access_token, or user_access_token"),
    user_access_token: str = typer.Option("", "--user-access-token", help="Optional short-lived user token"),
    connect: bool = typer.Option(False, "--connect", help="Try to connect after configuration"),
) -> None:
    if app_id and app_secret:
        status = configure_lark_mcp(
            cwd,
            server_name=server_name,
            app_id=app_id,
            app_secret=app_secret,
            domain=domain,
            tools=[item.strip() for item in tools.split(",") if item.strip()],
            oauth=oauth,
            token_mode=token_mode,
            user_access_token=user_access_token,
        )
        print("Configured Feishu/Lark MCP:")
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if oauth:
            print("Login command:")
            print(" ".join(lark_login_command(app_id=app_id, app_secret="<APP_SECRET>")))
    else:
        print(json.dumps(lark_mcp_status(cwd, server_name=server_name), ensure_ascii=False, indent=2))
    if connect:
        print("Connector catalog:")
        print(json.dumps(McpConnectorRegistry(cwd).context(connect=True), ensure_ascii=False, indent=2))


@app.command("feishu-event")
def feishu_event_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    text: str = typer.Option("", "--text", help="Simulate a Feishu bot text message"),
    setup_guide: bool = typer.Option(False, "--setup-guide", help="Write callback setup guide"),
) -> None:
    if setup_guide:
        print(f"Feishu event setup guide: {write_event_setup_guide(cwd)}")
        return
    if not text:
        raise typer.BadParameter("--text is required unless --setup-guide is used")
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "cli_test_open_id"}},
            "message": {
                "message_id": "cli_test_message",
                "chat_id": "cli_test_chat",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }
    print(json.dumps(FeishuEventBridge(cwd).handle(payload), ensure_ascii=False, indent=2))


@app.command("feishu-listen")
def feishu_listen_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    server_name: str = typer.Option("feishu_legal_workspace", "--server-name", help="Configured Feishu MCP server name"),
    status: bool = typer.Option(False, "--status", help="Show long-connection status and exit"),
) -> None:
    listener = FeishuLongConnectionListener(
        cwd,
        server_name=server_name,
        on_result=lambda result: print(json.dumps(result, ensure_ascii=False, indent=2)),
    )
    if status:
        print(json.dumps(listener.status(), ensure_ascii=False, indent=2))
        return
    print("Starting Feishu long-connection listener for im.message.receive_v1.")
    print("This mode does not need a public callback domain. Press Ctrl+C to stop.")
    listener.start()


@app.command("serve")
def serve_cmd(
    cwd: str = typer.Option(str(Path.cwd()), "--cwd", help="Project root"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host"),
    port: int = typer.Option(5180, "--port", help="Bind port"),
) -> None:
    LegalWorkbenchServer(cwd, host=host, port=port).serve_forever()


def _load_settings(cwd: str) -> dict:
    path = settings_path(cwd)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}
