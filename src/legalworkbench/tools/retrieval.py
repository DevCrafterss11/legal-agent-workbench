"""Clause retrieval tool."""

from __future__ import annotations

from typing import Any

from legalworkbench.rag import get_rag_service
from legalworkbench.retrieval import retrieve_memories
from legalworkbench.store import WorkbenchStore
from legalworkbench.tools.base import ToolContext, ToolResult


class ClauseRetrieverTool:
    name = "clause_retriever"
    description = "Retrieve legal evidence and historical memory for a clause."

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        store = WorkbenchStore(context.cwd)
        query = str(arguments.get("query") or "")
        contract_type = str(arguments.get("contract_type") or "general")
        rag = get_rag_service(context.cwd)
        evidence = rag.retrieve(
            query,
            contract_type=contract_type,
            top_k=int(arguments.get("top_k") or 10),
        )
        memories = retrieve_memories(store.load_memory(), query, contract_type=contract_type)
        return ToolResult(
            output={"evidence": evidence, "memories": memories},
            summary=f"{len(evidence)} evidence, {len(memories)} memory hits",
            metadata={"rag": rag.status()},
        )
