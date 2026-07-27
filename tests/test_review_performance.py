"""Regression tests for review cold-start and duplicate submission behavior."""

from __future__ import annotations

from threading import Event, Lock
import time

from fastapi.testclient import TestClient

import legalworkbench.rag.service as rag_service
from legalworkbench.llm import LlmClient, LlmConfig
from legalworkbench.models import KnowledgeEntry
from legalworkbench.paths import knowledge_dir
from legalworkbench.rag.service import LegalRagService, RagConfig
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.store import write_model_list
from legalworkbench.web import create_app


class RecordingBatchEmbedding:
    name = "recording-batch"
    dimensions = 3

    def __init__(self, *, fail_on_batch: bool = False) -> None:
        self.batch_calls: list[tuple[int, int]] = []
        self.fail_on_batch = fail_on_batch

    def embed(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0, 0.0]

    def embed_many(self, texts: list[str], *, batch_size: int = 64) -> list[list[float]]:
        if self.fail_on_batch:
            raise AssertionError("persistent index reuse must not re-encode the corpus")
        self.batch_calls.append((len(texts), batch_size))
        return [[1.0, 0.0, 0.0] for _ in texts]


def seed_knowledge(tmp_path, *, count: int = 5) -> None:
    write_model_list(
        knowledge_dir(tmp_path) / "performance.json",
        [
            KnowledgeEntry(
                id=f"K{index:03d}",
                title=f"条款 {index}",
                body="违约责任和赔偿限制",
                contract_type="service",
                clause_type="liability",
                risk_type="unlimited_liability",
                risk_level="high",
                source="test",
            )
            for index in range(count)
        ]
    )


def test_local_reindex_uses_one_batch_embedding_call(tmp_path) -> None:
    seed_knowledge(tmp_path)
    embedding = RecordingBatchEmbedding()

    service = LegalRagService(
        tmp_path,
        embedding_model=embedding,
        config=RagConfig(vector_backend="local", embedding_batch_size=32),
    )

    assert embedding.batch_calls == [(5, 32)]
    assert service.status()["indexed_entries"] == 5
    assert service.status()["index_reused"] is False


def test_existing_milvus_index_skips_corpus_embedding(tmp_path, monkeypatch) -> None:
    seed_knowledge(tmp_path)

    class ReusableMilvus:
        name = "milvus"

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def can_reuse(self, entries, *, dimension: int) -> bool:
            return len(entries) == 5 and dimension == 3

        def upsert(self, entries, vectors) -> None:
            raise AssertionError("reusable Milvus collection must not be rewritten")

        def search(self, vector, *, top_k: int, filters=None):
            return []

        def status(self):
            return {"backend": "milvus", "connected": True}

    monkeypatch.setattr(rag_service, "MilvusVectorStore", ReusableMilvus)
    embedding = RecordingBatchEmbedding(fail_on_batch=True)

    service = LegalRagService(
        tmp_path,
        embedding_model=embedding,
        config=RagConfig(vector_backend="milvus"),
    )

    assert service.status()["index_reused"] is True
    assert service.status()["indexed_entries"] == 5
    assert (tmp_path / ".lawbench" / "rag_index_state.json").exists()


def test_duplicate_web_reviews_share_one_background_task(tmp_path, monkeypatch) -> None:
    original_review = LegalAgentRuntime.review
    started = Event()
    release = Event()
    calls = 0
    calls_lock = Lock()

    def slow_review(self, *args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=3)
        return original_review(self, *args, **kwargs)

    monkeypatch.setattr(LegalAgentRuntime, "review", slow_review)
    app = create_app(tmp_path)
    payload = {"contract_text": "## 赔偿责任\n乙方承担全部损失且不设责任上限。"}

    with TestClient(app) as client:
        first = client.post("/api/review", json=payload)
        assert first.status_code == 202
        assert started.wait(timeout=2)

        second = client.post("/api/review", json=payload)
        assert second.status_code == 202
        assert second.json()["task_id"] == first.json()["task_id"]
        assert second.json()["deduplicated"] is True
        assert calls == 1

        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = client.get(f"/api/tasks/{first.json()['task_id']}").json()
            if task["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert task["status"] == "completed"
        assert calls == 1


def test_rule_grounded_short_contract_skips_redundant_llm_calls(tmp_path) -> None:
    class CountingLlm(LlmClient):
        def __init__(self) -> None:
            super().__init__(LlmConfig(provider="local"))
            self.decision_tasks: list[str] = []
            self.discovery_calls = 0
            self.judgment_calls = 0

        def decide(self, *, task, payload, fallback):
            del payload
            self.decision_tasks.append(task)
            return {**fallback, "decision_source": "test"}

        def discover_risk_candidates(self, **kwargs):
            del kwargs
            self.discovery_calls += 1
            return {"candidates": [], "decision_source": "test"}

        def semantic_judgment(self, **kwargs):
            del kwargs
            self.judgment_calls += 1
            return {"score": 1.0}

    runtime = LegalAgentRuntime(tmp_path)
    runtime.init_samples()
    llm = CountingLlm()
    runtime.llm = llm
    contract = tmp_path / "rule-grounded.md"
    contract.write_text(
        "## 赔偿责任\n乙方承担全部损失且不设赔偿责任上限。",
        encoding="utf-8",
    )

    run = runtime.review(contract)

    assert run.status == "completed"
    assert run.findings
    assert "plan_review" not in llm.decision_tasks
    assert llm.discovery_calls == 0
    assert llm.judgment_calls == 0
