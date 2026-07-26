"""Cross-encoder reranker with graceful fallback to the formula reranker.

两级重排：召回后的候选先过轻量公式 rerank（BM25 分数 + 语义重叠 + metadata），
配置 cross_encoder 后再对 top 候选做 query-doc 联合编码精排（BAAI/bge-reranker）。
模型不可用（依赖未装、下载失败）时静默降级回公式重排，错误暴露在 rag status 里。

公式分与 CE 分量纲不同：CE 输出为 logit，直接替换 rerank_score 并重新排序，
reason 字段保留两阶段分数便于解释排序来源。
"""

from __future__ import annotations

from typing import Protocol

from legalworkbench.models import RetrievedEvidence

RERANK_CANDIDATE_LIMIT = 32


class EvidenceReranker(Protocol):
    name: str

    def rerank(self, query: str, evidence: list[RetrievedEvidence]) -> list[RetrievedEvidence]: ...


class CrossEncoderReranker:
    """BAAI/bge-reranker style cross-encoder rerank over top candidates."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-base", *, device: str = "cpu") -> None:
        from sentence_transformers import CrossEncoder

        self.name = model_name
        self.model = CrossEncoder(model_name, device=device)

    def rerank(self, query: str, evidence: list[RetrievedEvidence]) -> list[RetrievedEvidence]:
        if not evidence:
            return evidence
        head = evidence[:RERANK_CANDIDATE_LIMIT]
        tail = evidence[RERANK_CANDIDATE_LIMIT:]
        pairs = [(query, f"{item.title} {item.body_preview}") for item in head]
        scores = self.model.predict(pairs)
        rescored = [
            item.model_copy(
                update={
                    "rerank_score": round(float(score), 4),
                    "reason": f"{item.reason}, cross_encoder={float(score):.4f}",
                }
            )
            for item, score in zip(head, scores)
        ]
        rescored.sort(key=lambda item: (item.rerank_score, item.score), reverse=True)
        return rescored + tail


def build_reranker(provider: str, model_name: str, *, device: str = "cpu") -> tuple[EvidenceReranker | None, str]:
    """Return (reranker, error). provider 'formula' -> (None, '') keeps the formula path."""

    if provider.lower() not in {"cross_encoder", "bge_reranker", "cross-encoder"}:
        return None, ""
    try:
        return CrossEncoderReranker(model_name, device=device), ""
    except Exception as exc:  # noqa: BLE001 - 缺依赖/下载失败时降级公式重排
        return None, str(exc)
