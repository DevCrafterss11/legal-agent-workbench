"""Hybrid RAG retrieval for contract clauses and review memories."""

from __future__ import annotations

from dataclasses import dataclass

from legalworkbench.models import LegalMemory, RetrievedEvidence
from legalworkbench.retrieval import HybridClauseRetriever, retrieve_memories


@dataclass(frozen=True)
class RetrievalBundle:
    """Evidence and memory returned for one clause query."""

    evidence: list[RetrievedEvidence]
    memory_hits: list[LegalMemory]


__all__ = ["HybridClauseRetriever", "RetrievalBundle", "retrieve_memories"]
