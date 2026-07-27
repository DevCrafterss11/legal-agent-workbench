"""Embedding providers for legal RAG."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

from legalworkbench.retrieval import tokenize


class EmbeddingModel:
    """Embedding model protocol."""

    name: str
    dimensions: int

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_many(self, texts: list[str], *, batch_size: int = 64) -> list[list[float]]:
        """Encode a corpus in batches.

        Lightweight providers can keep the deterministic per-item fallback;
        transformer providers override this to avoid thousands of tiny model calls.
        """

        return [self.embed(text) for text in texts]


@dataclass
class HashingEmbeddingModel(EmbeddingModel):
    """Deterministic local embedding fallback.

    Production deployments can replace this with BGE, OpenAI embeddings, or an
    internal embedding service while preserving the same vector-store contract.
    """

    name: str = "local-hashing-embedding"
    dimensions: int = 384

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = tokenize(text)
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [round(value / norm, 6) for value in vector]


class SentenceTransformerEmbeddingModel(EmbeddingModel):
    """Real local embedding provider for BGE/Sentence-Transformers models."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        *,
        device: str = "cpu",
        normalize_embeddings: bool = True,
        trust_remote_code: bool = False,
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("sentence-transformers is not installed; install with `pip install -e '.[bge]'`.") from exc
        kwargs: dict[str, Any] = {"device": device}
        if trust_remote_code:
            kwargs["trust_remote_code"] = True
        self._model = SentenceTransformer(model_name, **kwargs)
        self._normalize_embeddings = normalize_embeddings
        self.name = f"sentence-transformers:{model_name}"
        if hasattr(self._model, "get_embedding_dimension"):
            dimension = self._model.get_embedding_dimension()
        else:
            dimension = self._model.get_sentence_embedding_dimension()
        self.dimensions = int(dimension or 0)

    def embed(self, text: str) -> list[float]:
        vector = self._model.encode(
            text,
            normalize_embeddings=self._normalize_embeddings,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [float(value) for value in vector.tolist()]

    def embed_many(self, texts: list[str], *, batch_size: int = 64) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            texts,
            batch_size=max(1, batch_size),
            normalize_embeddings=self._normalize_embeddings,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in row] for row in vectors.tolist()]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return numerator / (left_norm * right_norm)
