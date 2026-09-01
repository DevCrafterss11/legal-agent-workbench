"""Build bounded, provenance-aware context packets for LLM calls.

ReviewRun remains the source of truth.  This module creates an ephemeral view of
that state for one inference, so agents do not need to duplicate token budgeting
and memory filtering logic in each prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Iterable

from legalworkbench.models import ContractClause, LegalMemory, RetrievedEvidence, ReviewRun
from legalworkbench.observability.tokens import estimate_tokens


@dataclass(frozen=True)
class ContextItem:
    """One candidate context segment and the reason it was selected."""

    kind: str
    source: str
    content: str
    priority: int
    tokens: int


@dataclass(frozen=True)
class ContextPacket:
    """The bounded context sent to one model call."""

    task: str
    text: str
    token_budget: int
    used_tokens: int
    selected: tuple[ContextItem, ...]
    omitted: tuple[dict[str, object], ...] = ()

    @property
    def utilization(self) -> float:
        return round(self.used_tokens / max(1, self.token_budget), 4)

    def trace(self) -> dict[str, object]:
        """Return safe metadata for ReviewRun traces without raw contract text."""

        return {
            "task": self.task,
            "token_budget": self.token_budget,
            "used_tokens": self.used_tokens,
            "utilization": self.utilization,
            "selected": [
                {"kind": item.kind, "source": item.source, "tokens": item.tokens}
                for item in self.selected
            ],
            "omitted": list(self.omitted),
        }


class ContextManager:
    """Select and order trusted state under a deterministic token budget."""

    DEFAULT_BUDGET = 2_400

    def __init__(self, *, default_budget: int = DEFAULT_BUDGET) -> None:
        self.default_budget = max(128, int(default_budget))

    def build_for_clause(
        self,
        run: ReviewRun,
        clause: ContractClause,
        *,
        task: str,
        evidence: Iterable[RetrievedEvidence] = (),
        memories: Iterable[LegalMemory] = (),
        token_budget: int | None = None,
    ) -> ContextPacket:
        """Build context for a clause-level inference.

        Selection is priority ordered: the current clause is mandatory, then
        evidence, current findings, and only approved/active tenant-local memory.
        Duplicate segments are removed before applying the budget.
        """

        budget = max(128, int(token_budget or self.default_budget))
        candidates: list[ContextItem] = []
        candidates.append(
            self._item("current_clause", clause.clause_id, f"标题：{clause.title}\n原文：{clause.text}", 100)
        )
        for index, item in enumerate(evidence):
            candidates.append(
                self._item(
                    "rag_evidence",
                    item.entry_id or f"evidence_{index}",
                    f"来源：{item.source}\n标题：{item.title}\n证据：{item.body_preview}",
                    90,
                )
            )
        for finding in run.findings:
            if finding.clause_id == clause.clause_id or finding.blocked:
                continue
            candidates.append(
                self._item(
                    "current_finding",
                    finding.finding_id,
                    f"{finding.risk_type}/{finding.risk_level}：{finding.summary}",
                    60,
                )
            )
        for memory in memories:
            if memory.tenant_id != run.tenant_id or memory.status not in {"approved", "active"}:
                continue
            advice = memory.approved_advice.strip()
            candidates.append(
                self._item(
                    "long_term_memory",
                    memory.memory_id,
                    f"记忆摘要：{memory.summary}" + (f"\n已批准建议：{advice}" if advice else ""),
                    70,
                )
            )

        selected: list[ContextItem] = []
        omitted: list[dict[str, object]] = []
        used = 0
        seen: set[str] = set()
        for item in sorted(candidates, key=lambda value: (-value.priority, value.kind, value.source)):
            digest = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
            if digest in seen:
                omitted.append({"kind": item.kind, "source": item.source, "reason": "duplicate"})
                continue
            seen.add(digest)
            if item.tokens <= budget - used:
                selected.append(item)
                used += item.tokens
                continue
            if item.kind == "current_clause" and not selected:
                clipped = self._clip(item, budget)
                selected.append(clipped)
                used = clipped.tokens
                continue
            omitted.append({"kind": item.kind, "source": item.source, "tokens": item.tokens, "reason": "budget"})

        text = "\n\n".join(f"[{item.kind} | {item.source}]\n{item.content}" for item in selected)
        return ContextPacket(
            task=task,
            text=text,
            token_budget=budget,
            used_tokens=used,
            selected=tuple(selected),
            omitted=tuple(omitted),
        )

    def record(self, run: ReviewRun, packet: ContextPacket) -> None:
        """Append safe selection metadata to the run for observability."""

        traces = run.mcp_context.setdefault("context_packets", [])
        if isinstance(traces, list):
            traces.append(packet.trace())

    @staticmethod
    def _item(kind: str, source: str, content: str, priority: int) -> ContextItem:
        return ContextItem(kind=kind, source=source, content=content, priority=priority, tokens=estimate_tokens(content))

    @staticmethod
    def _clip(item: ContextItem, budget: int) -> ContextItem:
        # Character clipping is intentionally conservative; estimate_tokens is
        # mixed Chinese/English aware but not a provider tokenizer.
        content = item.content
        while len(content) > 32 and estimate_tokens(content) > budget:
            content = content[: max(32, int(len(content) * 0.85))]
        return ContextItem(item.kind, item.source, content, item.priority, min(budget, estimate_tokens(content)))
