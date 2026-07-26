"""Memory management for enterprise legal review decisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from legalworkbench.fs import atomic_write_text
from legalworkbench.models import LegalMemory, ReviewRun
from legalworkbench.paths import memory_path, workspace_dir
from legalworkbench.retrieval import retrieve_memories
from legalworkbench.store import load_model_list, write_model_list


@dataclass(frozen=True)
class MemoryWritePolicy:
    """Rules that decide which review outcomes can become long-term memory."""

    min_confidence: float = 0.72
    require_source: bool = True
    skip_blocked_findings: bool = True
    require_human_for_high_risk: bool = True


class LegalMemoryStore:
    """File-backed semantic, episodic, procedural, and preference memory."""

    def __init__(self, cwd: str | Path | None = None, *, policy: MemoryWritePolicy | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.policy = policy or MemoryWritePolicy()

    @property
    def index_path(self) -> Path:
        return workspace_dir(self.cwd) / "MEMORY.md"

    def list(self) -> list[LegalMemory]:
        return load_model_list(memory_path(self.cwd), LegalMemory)

    def save(self, memories: list[LegalMemory]) -> None:
        write_model_list(memory_path(self.cwd), memories)
        self.write_index(memories)

    def recall(self, query: str, *, contract_type: str, top_k: int = 5) -> list[LegalMemory]:
        return retrieve_memories(self.list(), query, contract_type=contract_type, top_k=top_k)

    def consolidate_from_run(self, run: ReviewRun) -> list[LegalMemory]:
        memories = self.list()
        existing = {(item.contract_type, item.risk_type, _normalize(item.summary)) for item in memories}
        created: list[LegalMemory] = []
        for finding in run.findings:
            if self.policy.skip_blocked_findings and finding.blocked:
                continue
            if self.policy.require_source and not finding.evidence:
                continue
            if self.policy.require_human_for_high_risk and finding.risk_level == "high" and finding.requires_human_review:
                confidence = 0.76
            else:
                confidence = 0.9
            if confidence < self.policy.min_confidence:
                continue
            key = (run.contract_type, finding.risk_type, _normalize(finding.summary))
            if key in existing:
                continue
            memory = LegalMemory(
                memory_id=f"mem_{uuid4().hex[:10]}",
                type="episodic" if finding.rule_hits else "semantic",
                contract_type=run.contract_type,
                clause_type=finding.risk_type,
                risk_type=finding.risk_type,
                risk_level=finding.risk_level,
                summary=finding.summary,
                approved_advice=finding.suggestion,
                source_review_run_id=run.review_run_id,
                approved_by_human=not finding.requires_human_review,
                confidence=confidence,
                tags=[finding.clause_title, *finding.rule_hits],
            )
            memories.append(memory)
            created.append(memory)
            existing.add(key)
        if created:
            self.save(memories)
        elif memories:
            self.write_index(memories)
        return created

    def write_index(self, memories: list[LegalMemory] | None = None) -> Path:
        memories = self.list() if memories is None else memories
        grouped: dict[str, list[LegalMemory]] = {}
        for memory in memories:
            grouped.setdefault(memory.contract_type, []).append(memory)
        lines = ["# Legal Review Memory Index", ""]
        for contract_type in sorted(grouped):
            lines.append(f"## {contract_type}")
            for memory in sorted(grouped[contract_type], key=lambda item: item.memory_id):
                lines.append(
                    f"- `{memory.memory_id}` [{memory.type}/{memory.risk_type}/{memory.risk_level}] "
                    f"{memory.summary}"
                )
            lines.append("")
        atomic_write_text(self.index_path, "\n".join(lines).rstrip() + "\n")
        return self.index_path

    def export_jsonl(self) -> Path:
        path = workspace_dir(self.cwd) / "memory_export.jsonl"
        rows = [json.dumps(item.model_dump(mode="json"), ensure_ascii=False) for item in self.list()]
        atomic_write_text(path, "\n".join(rows).rstrip() + ("\n" if rows else ""))
        return path


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())
