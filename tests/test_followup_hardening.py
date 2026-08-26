"""Regression tests for the post-P0 runtime hardening work."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from legalworkbench.hooks import HookEvent, HookEventBus
from legalworkbench.llm import LlmConfig, LlmClient
from legalworkbench.models import KnowledgeEntry
from legalworkbench.paths import knowledge_dir
from legalworkbench.rag.service import LegalRagService, RagConfig
from legalworkbench.rag.vector_store import InMemoryVectorStore, MilvusVectorStore
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.store import write_model_list
from legalworkbench.web import create_app


def test_memory_off_is_a_real_ablation(tmp_path: Path) -> None:
    runtime = LegalAgentRuntime(tmp_path)
    paths = runtime.init_samples()
    before = json.loads((tmp_path / ".lawbench" / "memory.json").read_text(encoding="utf-8"))

    run = runtime.review(paths["contract"], memory_enabled=False)

    after = json.loads((tmp_path / ".lawbench" / "memory.json").read_text(encoding="utf-8"))
    assert run.mcp_context["memory_enabled"] is False
    assert run.memory_hits == []
    assert run.mcp_context["memory_ablation"] == "off"
    assert after == before


def test_rag_tenant_scope_excludes_private_entries(tmp_path: Path) -> None:
    write_model_list(
        knowledge_dir(tmp_path) / "tenant.json",
        [
            KnowledgeEntry(id="shared", title="赔偿", body="责任上限", tenant_id="shared", risk_type="unlimited_liability"),
            KnowledgeEntry(id="a", title="赔偿", body="甲方私有责任上限", tenant_id="tenant-a", risk_type="unlimited_liability"),
            KnowledgeEntry(id="b", title="赔偿", body="乙方私有责任上限", tenant_id="tenant-b", risk_type="unlimited_liability"),
        ],
    )
    service = LegalRagService(tmp_path, config=RagConfig(vector_backend="local"))

    entries = service.retrieve("赔偿责任上限", contract_type="general", tenant_id="tenant-a")

    assert {item.entry_id for item in entries} <= {"shared", "a"}
    assert "b" not in {item.entry_id for item in entries}


def test_reused_milvus_can_fail_over_to_hydrated_local_store() -> None:
    class Client:
        def query(self, **kwargs):
            import re

            match = re.search(r"id in \[(\d+)", str(kwargs.get("filter")))
            return [{"id": int(match.group(1)), "vector": [1.0, 0.0]}] if match else []

    store = object.__new__(MilvusVectorStore)
    store._client = Client()
    store._fallback = InMemoryVectorStore()
    store.collection = "test"
    store._connect_error = ""
    entry = KnowledgeEntry(id="x", title="x", body="x")
    assert store.hydrate_fallback([entry]) is True
    store._client = None
    hits = store.search([1.0, 0.0], top_k=1)
    assert hits and hits[0].entry.id == "x"


def test_sse_resumes_after_last_event_id(tmp_path: Path) -> None:
    bus = HookEventBus(tmp_path)
    bus.emit(HookEvent("old", "run-1"))
    first_id = bus.tail()[0]["event_id"]
    bus.emit(HookEvent("new", "run-1"))
    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            "/api/events/stream",
            params={"cycles": 1, "run_id": "run-1"},
            headers={"Last-Event-ID": first_id},
        )
    assert response.status_code == 200
    assert '"name": "new"' in response.text
    assert '"name": "old"' not in response.text
    assert "id:" in response.text


def test_model_route_selects_task_specific_model() -> None:
    client = LlmClient(LlmConfig(model="base", model_routes={"plan_review": "small"}))
    response = client._local(system="", user=json.dumps({"task": "plan_review"}))
    assert response.model == "base"  # direct local helper is intentionally un-routed
    client.complete(system="", user=json.dumps({"task": "plan_review"}), task="plan_review")
    assert client.call_traces[-1].model == "small"


def test_heldout_manifest_is_available() -> None:
    from legalworkbench.evals.real_benchmark import load_real_benchmark

    payload = load_real_benchmark(Path(__file__).parents[1], dataset="heldout")
    assert payload.get("split") == "heldout"
    assert payload.get("contracts")


def test_postgres_reconciliation_is_tenant_scoped() -> None:
    """The SQL adapter must not globally delete another tenant's rows."""
    from legalworkbench.storage.postgres import PostgresPersistence

    class Cursor:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((str(query), params))

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class Conn(Cursor):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def commit(self):
            self.calls.append(("COMMIT", None))

    conn = Conn()
    backend = object.__new__(PostgresPersistence)
    backend._Jsonb = lambda value: value
    backend._connect = lambda: conn
    from legalworkbench.models import LegalMemory

    backend.save_memory([LegalMemory(memory_id="m1", type="semantic", tenant_id="tenant-a", summary="x")])
    deletes = [query for query, _ in conn.calls if query.strip().startswith("DELETE FROM lawbench_memory")]
    assert deletes
    assert "tenant_id = ANY" in deletes[-1]
    assert "DELETE FROM lawbench_memory\n" not in deletes
