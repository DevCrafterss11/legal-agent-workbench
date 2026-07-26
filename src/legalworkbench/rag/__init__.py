"""RAG retrieval package."""

from legalworkbench.rag.embeddings import HashingEmbeddingModel
from legalworkbench.rag.retriever import HybridClauseRetriever, RetrievalBundle, retrieve_memories
from legalworkbench.rag.service import LegalRagService, RagConfig, clear_rag_service_cache, get_rag_service, lightweight_rag_status
from legalworkbench.rag.vector_store import InMemoryVectorStore, MilvusVectorStore

__all__ = [
    "HashingEmbeddingModel",
    "HybridClauseRetriever",
    "InMemoryVectorStore",
    "LegalRagService",
    "MilvusVectorStore",
    "RagConfig",
    "RetrievalBundle",
    "clear_rag_service_cache",
    "get_rag_service",
    "lightweight_rag_status",
    "retrieve_memories",
]
