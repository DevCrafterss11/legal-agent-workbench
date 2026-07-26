"""Memory management for enterprise legal review decisions.

记忆生命周期（面向"记忆怎么处理"的完整回答）：

- 写入门槛：置信度阈值 + 必须有证据来源 + 高风险未复核降权（MemoryWritePolicy）。
- 冲突处理：同（合同类型, 风险类型, 归一化摘要）的再次出现不是跳过，而是强化——
  reinforce_count 自增、置信度小步上调、采纳更新的已复核建议。
- 召回强化：被审查命中的记忆记录 use_count / last_used_at，召回排序按
  相关性 + 使用频率 + 时间衰减综合打分（见 retrieval.retrieve_memories）。
- 遗忘：容量上限触发驱逐，保留分 = 置信度 + 使用强化 + 新近性，
  人工复核过的记忆优先保留；被驱逐记忆导出到归档文件，可审计不可召回。
- 溯源：每条记忆携带 source_review_run_id，可回溯到产生它的审查链路。
"""

from __future__ import annotations

import json
import math
import re
import time
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
    max_entries: int = 500
    reinforce_confidence_step: float = 0.02
    max_confidence: float = 0.98


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
        by_key = {(item.contract_type, item.risk_type, _normalize(item.summary)): item for item in memories}
        created: list[LegalMemory] = []
        reinforced = 0
        now = time.time()
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
            existing = by_key.get(key)
            if existing is not None:
                # 冲突处理：同一结论再次出现视为独立佐证 -> 强化而非丢弃
                index = memories.index(existing)
                updated = existing.model_copy(
                    update={
                        "reinforce_count": existing.reinforce_count + 1,
                        "confidence": min(
                            self.policy.max_confidence,
                            max(existing.confidence, confidence) + self.policy.reinforce_confidence_step,
                        ),
                        "approved_advice": finding.suggestion if not finding.requires_human_review else existing.approved_advice,
                        "last_used_at": now,
                    }
                )
                memories[index] = updated
                by_key[key] = updated
                reinforced += 1
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
                created_at=now,
            )
            memories.append(memory)
            created.append(memory)
            by_key[key] = memory
        memories = self._evict_if_needed(memories)
        if created or reinforced:
            self.save(memories)
        elif memories:
            self.write_index(memories)
        return created

    def mark_used(self, memory_ids: list[str]) -> int:
        """Reinforce recalled memories: usage feeds back into recall ranking and eviction."""

        if not memory_ids:
            return 0
        wanted = set(memory_ids)
        memories = self.list()
        now = time.time()
        touched = 0
        for index, memory in enumerate(memories):
            if memory.memory_id in wanted:
                memories[index] = memory.model_copy(update={"use_count": memory.use_count + 1, "last_used_at": now})
                touched += 1
        if touched:
            write_model_list(memory_path(self.cwd), memories)
        return touched

    def retention_score(self, memory: LegalMemory, *, now: float | None = None) -> float:
        """保留分：置信度为主，使用强化与新近性加成，人工复核额外加权。"""

        now = now or time.time()
        score = memory.confidence
        score += min(memory.use_count, 10) * 0.03
        score += min(memory.reinforce_count, 5) * 0.05
        if memory.approved_by_human:
            score += 0.2
        reference = memory.last_used_at or memory.created_at
        if reference > 0:
            age_days = max(0.0, (now - reference) / 86_400)
            score *= math.pow(0.5, age_days / 180)  # 半衰期 180 天
        return round(score, 4)

    def _evict_if_needed(self, memories: list[LegalMemory]) -> list[LegalMemory]:
        limit = self.policy.max_entries
        if limit <= 0 or len(memories) <= limit:
            return memories
        now = time.time()
        ranked = sorted(memories, key=lambda item: self.retention_score(item, now=now), reverse=True)
        keep, evicted = ranked[:limit], ranked[limit:]
        self._archive_evicted(evicted)
        return keep

    def _archive_evicted(self, evicted: list[LegalMemory]) -> None:
        if not evicted:
            return
        path = workspace_dir(self.cwd) / "memory_archive.jsonl"
        rows = [json.dumps(item.model_dump(mode="json"), ensure_ascii=False) for item in evicted]
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        atomic_write_text(path, previous + "\n".join(rows) + "\n")

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
