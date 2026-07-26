"""Vector store abstractions and local/Milvus implementations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from legalworkbench.models import KnowledgeEntry
from legalworkbench.rag.embeddings import cosine_similarity


@dataclass(frozen=True)
class VectorHit:
    entry: KnowledgeEntry
    score: float
    metadata: dict[str, Any]


class VectorStore:
    name = "vector-store"

    def upsert(self, entries: list[KnowledgeEntry], vectors: list[list[float]]) -> None:
        raise NotImplementedError

    def search(self, vector: list[float], *, top_k: int, filters: dict[str, str] | None = None) -> list[VectorHit]:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError


class InMemoryVectorStore(VectorStore):
    name = "in-memory-vector-store"

    def __init__(self) -> None:
        self._rows: list[tuple[KnowledgeEntry, list[float]]] = []

    def upsert(self, entries: list[KnowledgeEntry], vectors: list[list[float]]) -> None:
        self._rows = list(zip(entries, vectors))

    def search(self, vector: list[float], *, top_k: int, filters: dict[str, str] | None = None) -> list[VectorHit]:
        filters = filters or {}
        hits: list[VectorHit] = []
        for entry, stored in self._rows:
            if any(str(getattr(entry, key, "")).lower() != value.lower() for key, value in filters.items() if value):
                continue
            hits.append(VectorHit(entry=entry, score=cosine_similarity(vector, stored), metadata={"backend": self.name}))
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:top_k]

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "entries": len(self._rows), "connected": True}


class MilvusVectorStore(VectorStore):
    """Milvus adapter with graceful fallback when pymilvus is unavailable."""

    name = "milvus"

    def __init__(self, *, uri: str = "http://127.0.0.1:19530", collection: str = "legal_clause_knowledge", timeout: float = 1.0) -> None:
        self.uri = uri
        self.collection = collection
        self._fallback = InMemoryVectorStore()
        self._client: Any | None = None
        self._connect_error = ""
        try:
            from pymilvus import MilvusClient  # type: ignore

            self._client = MilvusClient(uri=uri, timeout=timeout)
        except Exception as exc:  # pragma: no cover - depends on optional service
            self._connect_error = str(exc)

    def upsert(self, entries: list[KnowledgeEntry], vectors: list[list[float]]) -> None:
        self._fallback.upsert(entries, vectors)
        if self._client is None:
            return
        try:
            self._ensure_collection(dim=len(vectors[0]) if vectors else 384)
            rows = [
                {
                    "id": _stable_int_id(entry.id),
                    "entry_id": entry.id,
                    "title": entry.title,
                    "body": entry.body,
                    "contract_type": entry.contract_type,
                    "clause_type": entry.clause_type,
                    "risk_type": entry.risk_type,
                    "risk_level": entry.risk_level,
                    "source": entry.source,
                    "tags_json": json.dumps(entry.tags, ensure_ascii=False),
                    "vector": vector,
                }
                for entry, vector in zip(entries, vectors)
            ]
            if rows:
                self._client.upsert(collection_name=self.collection, data=rows)
        except Exception as exc:  # pragma: no cover - depends on optional service
            self._connect_error = str(exc)

    def search(self, vector: list[float], *, top_k: int, filters: dict[str, str] | None = None) -> list[VectorHit]:
        if self._client is None:
            return self._fallback.search(vector, top_k=top_k, filters=filters)
        try:
            expr = _filter_expr(filters or {})
            results = self._client.search(
                collection_name=self.collection,
                data=[vector],
                limit=top_k,
                filter=expr or "",
                output_fields=[
                    "entry_id",
                    "title",
                    "body",
                    "contract_type",
                    "clause_type",
                    "risk_type",
                    "risk_level",
                    "source",
                    "tags_json",
                ],
            )
            hits: list[VectorHit] = []
            for item in results[0] if results else []:
                entity = item.get("entity", item)
                entry = KnowledgeEntry(
                    id=str(entity.get("entry_id") or entity.get("id")),
                    title=str(entity.get("title") or ""),
                    body=str(entity.get("body") or ""),
                    contract_type=str(entity.get("contract_type") or "general"),
                    clause_type=str(entity.get("clause_type") or "general"),
                    risk_type=str(entity.get("risk_type") or "general"),
                    risk_level=str(entity.get("risk_level") or "medium"),
                    source=str(entity.get("source") or "milvus"),
                    tags=json.loads(entity.get("tags_json") or "[]"),
                )
                hits.append(VectorHit(entry=entry, score=float(item.get("distance") or item.get("score") or 0.0), metadata={"backend": self.name}))
            return hits
        except Exception as exc:  # pragma: no cover - depends on optional service
            self._connect_error = str(exc)
            return self._fallback.search(vector, top_k=top_k, filters=filters)

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "uri": self.uri,
            "collection": self.collection,
            "connected": self._client is not None,
            "fallback": self._fallback.status(),
            "error": self._connect_error,
        }

    def _ensure_collection(self, *, dim: int) -> None:
        if self._client is None:
            return
        try:
            if self._client.has_collection(collection_name=self.collection):
                return
            self._client.create_collection(
                collection_name=self.collection,
                dimension=dim,
                metric_type="COSINE",
                auto_id=False,
            )
        except TypeError:
            self._client.create_collection(collection_name=self.collection, dimension=dim)


def _stable_int_id(value: str) -> int:
    import hashlib

    return int(hashlib.sha1(value.encode("utf-8")).hexdigest()[:15], 16)


def _filter_expr(filters: dict[str, str]) -> str:
    parts = []
    for key, value in filters.items():
        if value:
            escaped = value.replace('"', '\\"')
            parts.append(f'{key} == "{escaped}"')
    return " and ".join(parts)
