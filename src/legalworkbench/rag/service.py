"""Production-style legal RAG service."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Any

from legalworkbench.models import KnowledgeEntry, RetrievedEvidence
from legalworkbench.paths import settings_path
from legalworkbench.rag.embeddings import EmbeddingModel, HashingEmbeddingModel, SentenceTransformerEmbeddingModel
from legalworkbench.rag.reranker import build_reranker
from legalworkbench.rag.vector_store import InMemoryVectorStore, MilvusVectorStore, VectorStore
from legalworkbench.retrieval import HybridClauseRetriever, rerank_evidence
from legalworkbench.store import WorkbenchStore


@dataclass(frozen=True)
class RagConfig:
    vector_backend: str = "local"
    milvus_uri: str = "http://127.0.0.1:19530"
    collection: str = "legal_clause_knowledge"
    embedding_provider: str = "hashing"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_device: str = "cpu"
    embedding_normalize: bool = True
    embedding_fallback: bool = True
    lexical_top_k: int = 32
    vector_top_k: int = 32
    final_top_k: int = 10
    connect_timeout: float = 1.0
    rerank_provider: str = "formula"
    rerank_model: str = "BAAI/bge-reranker-base"
    fusion: str = "score"  # score: 加权分数融合; rrf: reciprocal rank fusion


class LegalRagService:
    """Hybrid BM25 + vector retrieval + rerank service."""

    def __init__(
        self,
        cwd: str | Path | None = None,
        *,
        embedding_model: EmbeddingModel | None = None,
        config: RagConfig | None = None,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.config = config or _load_rag_config(self.cwd)
        self.embedding_error = ""
        self.embedding_model = embedding_model or self._build_embedding_model()
        self.reranker, self.rerank_error = build_reranker(
            self.config.rerank_provider, self.config.rerank_model, device=self.config.embedding_device
        )
        self.store = WorkbenchStore(self.cwd)
        self.vector_store = self._build_vector_store()
        self._indexed_entries: list[KnowledgeEntry] = []
        self.reindex()

    def _build_vector_store(self) -> VectorStore:
        if self.config.vector_backend.lower() == "milvus":
            return MilvusVectorStore(uri=self.config.milvus_uri, collection=self.config.collection, timeout=self.config.connect_timeout)
        return InMemoryVectorStore()

    def _build_embedding_model(self) -> EmbeddingModel:
        provider = self.config.embedding_provider.lower()
        if provider in {"bge", "sentence-transformers", "sentence_transformers"}:
            try:
                return SentenceTransformerEmbeddingModel(
                    self.config.embedding_model,
                    device=self.config.embedding_device,
                    normalize_embeddings=self.config.embedding_normalize,
                )
            except Exception as exc:
                self.embedding_error = str(exc)
                if not self.config.embedding_fallback:
                    raise
        return HashingEmbeddingModel()

    def reindex(self) -> None:
        entries = self.store.load_knowledge()
        vectors = [self.embedding_model.embed(_entry_text(entry)) for entry in entries]
        self.vector_store.upsert(entries, vectors)
        self._indexed_entries = entries

    def retrieve(self, query: str, *, contract_type: str, top_k: int | None = None) -> list[RetrievedEvidence]:
        final_top_k = top_k or self.config.final_top_k
        lexical = HybridClauseRetriever(self._indexed_entries).search(
            query,
            contract_type=contract_type,
            top_k=self.config.lexical_top_k,
            rerank=False,
        )
        vector_hits = self.vector_store.search(
            self.embedding_model.embed(query),
            top_k=self.config.vector_top_k,
            filters={},
        )
        if self.config.fusion.lower() == "rrf":
            merged = self._fuse_rrf(lexical, vector_hits)
        else:
            merged = self._fuse_score(lexical, vector_hits)
        evidence = rerank_evidence(query, list(merged.values()), contract_type=contract_type)
        if self.reranker is not None:
            evidence = self.reranker.rerank(query, evidence)
        return evidence[:final_top_k]

    def _fuse_score(self, lexical: list[RetrievedEvidence], vector_hits: list[Any]) -> dict[str, RetrievedEvidence]:
        """加权分数融合：词法分与向量分直接相加。量纲敏感，但分数可解释。"""

        merged: dict[str, RetrievedEvidence] = {item.entry_id: item for item in lexical}
        for hit in vector_hits:
            entry = hit.entry
            existing = merged.get(entry.id)
            vector_score = round(hit.score * 10.0, 4)
            if existing is None:
                merged[entry.id] = _evidence_from_entry(entry, score=vector_score, reason=f"vector={hit.score:.3f}")
            else:
                merged[entry.id] = existing.model_copy(
                    update={
                        "score": round(existing.score + vector_score, 4),
                        "reason": f"{existing.reason}, vector={hit.score:.3f}",
                    }
                )
        return merged

    def _fuse_rrf(self, lexical: list[RetrievedEvidence], vector_hits: list[Any], *, k: int = 60) -> dict[str, RetrievedEvidence]:
        """Reciprocal Rank Fusion：只用名次不用分数，天然免疫两路召回的量纲差异。

        RRF(d) = Σ 1/(k + rank_i(d))，k=60 为经验值。BM25 分与向量余弦分不可比，
        加权融合需要调权重；RRF 无需调参，是多路召回融合的标准做法。
        """

        scores: dict[str, float] = {}
        catalog: dict[str, RetrievedEvidence] = {}
        for rank, item in enumerate(lexical, start=1):
            scores[item.entry_id] = scores.get(item.entry_id, 0.0) + 1.0 / (k + rank)
            catalog[item.entry_id] = item
        for rank, hit in enumerate(vector_hits, start=1):
            entry = hit.entry
            scores[entry.id] = scores.get(entry.id, 0.0) + 1.0 / (k + rank)
            if entry.id not in catalog:
                catalog[entry.id] = _evidence_from_entry(entry, score=0.0, reason=f"vector_rank={rank}")
        merged: dict[str, RetrievedEvidence] = {}
        for entry_id, rrf in scores.items():
            item = catalog[entry_id]
            merged[entry_id] = item.model_copy(
                update={"score": round(rrf * 100, 4), "reason": f"{item.reason}, rrf={rrf:.4f}"}
            )
        return merged

    def status(self) -> dict[str, Any]:
        return {
            "embedding_model": self.embedding_model.name,
            "dimensions": self.embedding_model.dimensions,
            "embedding_provider": self.config.embedding_provider,
            "embedding_error": self.embedding_error,
            "rerank_provider": self.config.rerank_provider if self.reranker is not None else "formula",
            "rerank_model": getattr(self.reranker, "name", ""),
            "rerank_error": self.rerank_error,
            "vector_store": self.vector_store.status(),
            "indexed_entries": len(self._indexed_entries),
            "config": self.config.__dict__,
        }


_SERVICE_CACHE: dict[tuple[str, str, str], LegalRagService] = {}
_SERVICE_CACHE_LOCK = threading.Lock()


def get_rag_service(cwd: str | Path | None = None) -> LegalRagService:
    """Return a process-local RAG service without reloading embeddings per clause."""

    root = Path(cwd or Path.cwd()).resolve()
    config = _load_rag_config(root)
    key = (str(root), json.dumps(config.__dict__, ensure_ascii=False, sort_keys=True), _knowledge_fingerprint(root))
    service = _SERVICE_CACHE.get(key)
    if service is not None:
        return service
    with _SERVICE_CACHE_LOCK:
        service = _SERVICE_CACHE.get(key)
        if service is None:
            service = LegalRagService(root, config=config)
            _SERVICE_CACHE[key] = service
        return service


def clear_rag_service_cache() -> None:
    with _SERVICE_CACHE_LOCK:
        _SERVICE_CACHE.clear()


def lightweight_rag_status(cwd: str | Path | None = None) -> dict[str, Any]:
    root = Path(cwd or Path.cwd()).resolve()
    config = _load_rag_config(root)
    key_prefix = str(root)
    cached = next((service for key, service in _SERVICE_CACHE.items() if key[0] == key_prefix), None)
    if cached is not None:
        status = cached.status()
        status["warm"] = True
        return status
    return {
        "embedding_model": config.embedding_model if config.embedding_provider != "hashing" else "local-hashing-embedding",
        "dimensions": 0,
        "embedding_provider": config.embedding_provider,
        "embedding_error": "",
        "vector_store": {
            "backend": config.vector_backend,
            "uri": config.milvus_uri,
            "collection": config.collection,
            "connected": False,
            "warm": False,
        },
        "indexed_entries": 0,
        "config": config.__dict__,
        "warm": False,
    }


def _evidence_from_entry(entry: KnowledgeEntry, *, score: float, reason: str) -> RetrievedEvidence:
    return RetrievedEvidence(
        entry_id=entry.id,
        title=entry.title,
        source=entry.source,
        score=score,
        reason=reason,
        body_preview=entry.body[:240],
        risk_type=entry.risk_type,
        risk_level=entry.risk_level,
        rerank_score=0.0,
    )


def _entry_text(entry: KnowledgeEntry) -> str:
    return " ".join([entry.title, entry.body, entry.contract_type, entry.clause_type, entry.risk_type, " ".join(entry.tags)])


def _knowledge_fingerprint(cwd: Path) -> str:
    store = WorkbenchStore(cwd)
    parts = []
    for path in sorted((store.root / "knowledge").glob("*.json")):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


def _load_rag_config(cwd: Path) -> RagConfig:
    path = settings_path(cwd)
    if not path.exists():
        return RagConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return RagConfig()
    rag = raw.get("rag", {}) if isinstance(raw, dict) else {}
    if not isinstance(rag, dict):
        return RagConfig()
    return RagConfig(
        vector_backend=str(rag.get("vector_backend") or "local"),
        milvus_uri=str(rag.get("milvus_uri") or "http://127.0.0.1:19530"),
        collection=str(rag.get("collection") or "legal_clause_knowledge"),
        embedding_provider=str(rag.get("embedding_provider") or "hashing"),
        embedding_model=str(rag.get("embedding_model") or "BAAI/bge-small-zh-v1.5"),
        embedding_device=str(rag.get("embedding_device") or "cpu"),
        embedding_normalize=bool(rag.get("embedding_normalize", True)),
        embedding_fallback=bool(rag.get("embedding_fallback", True)),
        lexical_top_k=int(rag.get("lexical_top_k") or 32),
        vector_top_k=int(rag.get("vector_top_k") or 32),
        final_top_k=int(rag.get("final_top_k") or 10),
        connect_timeout=float(rag.get("connect_timeout") or 1.0),
        rerank_provider=str(rag.get("rerank_provider") or "formula"),
        rerank_model=str(rag.get("rerank_model") or "BAAI/bge-reranker-base"),
        fusion=str(rag.get("fusion") or "score"),
    )
