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

    def can_reuse(self, entries: list[KnowledgeEntry], *, dimension: int) -> bool:
        """Return whether an already-built persistent index matches this corpus."""

        return False


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
            if any(
                key != "tenant_id"
                and str(getattr(entry, key, "")).lower() != value.lower()
                for key, value in filters.items()
                if value
            ):
                continue
            tenant_filter = str(filters.get("tenant_id") or "").lower()
            if tenant_filter and entry.tenant_id.lower() not in {"shared", tenant_filter}:
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
            self._client = None

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
                    "tenant_id": entry.tenant_id,
                    "tags_json": json.dumps(entry.tags, ensure_ascii=False),
                    "vector": vector,
                }
                for entry, vector in zip(entries, vectors)
            ]
            for offset in range(0, len(rows), 256):
                self._client.upsert(
                    collection_name=self.collection,
                    data=rows[offset : offset + 256],
                )
        except Exception as exc:  # pragma: no cover - depends on optional service
            self._connect_error = str(exc)

    def hydrate_fallback(self, entries: list[KnowledgeEntry]) -> bool:
        """Load persisted Milvus vectors into the local failover index.

        This is only used after a validated index reuse. It avoids a second
        corpus embedding pass while ensuring a later Milvus outage has useful
        retrieval results.
        """

        if self._client is None or not entries:
            return False
        try:
            ids = [_stable_int_id(entry.id) for entry in entries]
            rows: list[dict[str, Any]] = []
            for offset in range(0, len(ids), 256):
                rows.extend(
                    self._client.query(
                        collection_name=self.collection,
                        filter=f"id in [{','.join(str(value) for value in ids[offset : offset + 256])}]",
                        output_fields=["id", "vector"],
                        limit=min(256, len(ids) - offset),
                    )
                )
            by_id = {int(row.get("id")): row.get("vector") for row in rows}
            vectors = [by_id.get(_stable_int_id(entry.id)) for entry in entries]
            if any(not isinstance(vector, list) or not vector for vector in vectors):
                return False
            self._fallback.upsert(entries, vectors)  # type: ignore[arg-type]
            return True
        except Exception as exc:  # pragma: no cover - depends on optional service
            self._connect_error = str(exc)
            self._client = None
            return False

    def can_reuse(self, entries: list[KnowledgeEntry], *, dimension: int) -> bool:
        """Validate a Milvus collection without scanning or rebuilding every vector.

        A persisted fingerprint in ``LegalRagService`` is the primary freshness
        check. This method additionally verifies collection size, vector dimension,
        and stable IDs sampled across the current corpus. The sampling check lets an
        existing pre-fingerprint collection be adopted safely enough for a one-time
        migration instead of forcing an expensive cold rebuild.
        """

        if self._client is None or not entries:
            return False
        try:
            if not self._client.has_collection(collection_name=self.collection):
                return False
            stats = self._client.get_collection_stats(collection_name=self.collection)
            if int(stats.get("row_count") or 0) < len(entries):
                return False
            description = self._client.describe_collection(collection_name=self.collection)
            vector_dimension = _vector_dimension(description)
            if vector_dimension and vector_dimension != dimension:
                return False
            indexes = sorted({0, len(entries) // 4, len(entries) // 2, (len(entries) * 3) // 4, len(entries) - 1})
            expected = {_stable_int_id(entries[index].id): entries[index].id for index in indexes}
            rows = self._client.query(
                collection_name=self.collection,
                filter=f"id in [{','.join(str(value) for value in expected)}]",
                output_fields=["id", "entry_id"],
                limit=len(expected),
            )
            actual = {int(row.get("id")): str(row.get("entry_id") or "") for row in rows}
            return all(actual.get(row_id) == entry_id for row_id, entry_id in expected.items())
        except Exception as exc:  # pragma: no cover - depends on optional service
            self._connect_error = str(exc)
            return False

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
                    "tenant_id",
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
                    tenant_id=str(entity.get("tenant_id") or "shared"),
                    tags=json.loads(entity.get("tags_json") or "[]"),
                )
                hits.append(VectorHit(entry=entry, score=float(item.get("distance") or item.get("score") or 0.0), metadata={"backend": self.name}))
            return hits
        except Exception as exc:  # pragma: no cover - depends on optional service
            self._connect_error = str(exc)
            self._client = None
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


def _vector_dimension(description: dict[str, Any]) -> int:
    for field in description.get("fields", []):
        if field.get("name") != "vector":
            continue
        raw = (field.get("params") or {}).get("dim") or field.get("dimension")
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _filter_expr(filters: dict[str, str]) -> str:
    parts = []
    for key, value in filters.items():
        if value:
            escaped = value.replace('"', '\\"')
            if key == "tenant_id":
                parts.append(f'(tenant_id == "shared" or tenant_id == "{escaped}")')
            else:
                parts.append(f'{key} == "{escaped}"')
    return " and ".join(parts)
