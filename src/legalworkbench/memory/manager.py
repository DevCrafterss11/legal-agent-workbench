"""Memory management for enterprise legal review decisions.

记忆生命周期（面向"记忆怎么处理"的完整回答）：

- 状态治理：PROPOSED -> APPROVED -> ACTIVE；也可进入 REJECTED、STALE、ARCHIVED。
  只有 APPROVED / ACTIVE 能参与召回。高风险、Prompt Injection 和 LLM-only
  语义候选必须先进入 PROPOSED，禁止直接污染跨 Session 上下文。
- 写入门槛：置信度阈值 + 必须有证据来源（MemoryWritePolicy）。
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
from legalworkbench.models import LegalMemory, MemoryStatus, ReviewRun
from legalworkbench.paths import memory_path, workspace_dir
from legalworkbench.privacy import mask_value
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
        from legalworkbench.storage.postgres import postgres_backend

        self._postgres = postgres_backend(self.cwd)

    @property
    def index_path(self) -> Path:
        return workspace_dir(self.cwd) / "MEMORY.md"

    def list(self, *, tenant_id: str | None = None) -> list[LegalMemory]:
        if self._postgres is not None:
            return self._postgres.load_memory(tenant_id=tenant_id)
        memories = load_model_list(memory_path(self.cwd), LegalMemory)
        if tenant_id is None:
            return memories
        return [item for item in memories if item.tenant_id == tenant_id]

    def save(self, memories: list[LegalMemory]) -> None:
        safe_memories = [
            LegalMemory.model_validate(mask_value(item.model_dump(mode="json")))
            for item in memories
        ]
        if self._postgres is not None:
            self._postgres.save_memory(safe_memories)
        else:
            write_model_list(memory_path(self.cwd), safe_memories)
        self.write_index(safe_memories)

    def recall(
        self,
        query: str,
        *,
        contract_type: str,
        top_k: int = 5,
        tenant_id: str = "local",
    ) -> list[LegalMemory]:
        return retrieve_memories(
            self.list(tenant_id=tenant_id),
            query,
            contract_type=contract_type,
            top_k=top_k,
            tenant_id=tenant_id,
        )

    def consolidate_from_run(self, run: ReviewRun) -> list[LegalMemory]:
        memories = self.list()
        by_key = {
            (*_memory_key(item.tenant_id, item.contract_type, item.risk_type, item.summary), _status_bucket(item.status)): item
            for item in memories
            if _status_bucket(item.status)
        }
        created: list[LegalMemory] = []
        reinforced = 0
        now = time.time()
        for finding in run.findings:
            if self.policy.skip_blocked_findings and finding.blocked:
                continue
            if self.policy.require_source and not finding.evidence:
                continue
            requires_approval = self._requires_human_approval(run, finding)
            confidence = 0.76 if requires_approval else 0.9
            if confidence < self.policy.min_confidence:
                continue
            status: MemoryStatus = "proposed" if requires_approval else "active"
            key = (*_memory_key(run.tenant_id, run.contract_type, finding.risk_type, finding.summary), _status_bucket(status))
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
                        "proposed_advice": (
                            finding.suggestion if status == "proposed" else existing.proposed_advice
                        ),
                        "approved_advice": (
                            finding.suggestion if status == "active" else existing.approved_advice
                        ),
                        "last_used_at": now,
                        "status_changed_at": existing.status_changed_at or now,
                    }
                )
                memories[index] = updated
                by_key[key] = updated
                reinforced += 1
                continue
            memory = LegalMemory(
                memory_id=f"mem_{uuid4().hex[:10]}",
                type="episodic" if finding.rule_hits else "semantic",
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                contract_type=run.contract_type,
                clause_type=finding.risk_type,
                risk_type=finding.risk_type,
                risk_level=finding.risk_level,
                summary=finding.summary,
                proposed_advice=finding.suggestion if status == "proposed" else "",
                approved_advice=finding.suggestion if status == "active" else "",
                source_review_run_id=run.review_run_id,
                status=status,
                approved_by_human=False,
                confidence=confidence,
                tags=[finding.clause_title, *finding.rule_hits],
                created_at=now,
                status_changed_at=now,
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

    def approve(self, memory_id: str, *, approver: str) -> LegalMemory:
        """Record human approval; APPROVED memories may be recalled before activation."""

        if not approver.strip():
            raise ValueError("approver is required")
        return self._transition(memory_id, "approved", approver=approver.strip())

    def activate(self, memory_id: str) -> LegalMemory:
        """Promote an approved memory into the normal ACTIVE lifecycle state."""

        return self._transition(memory_id, "active")

    def reject(self, memory_id: str, *, approver: str) -> LegalMemory:
        """Reject a proposed memory and keep it only as an auditable record."""

        if not approver.strip():
            raise ValueError("approver is required")
        return self._transition(memory_id, "rejected", approver=approver.strip())

    def mark_stale(self, memory_id: str) -> LegalMemory:
        return self._transition(memory_id, "stale")

    def archive(self, memory_id: str) -> LegalMemory:
        return self._transition(memory_id, "archived")

    def _transition(
        self,
        memory_id: str,
        target: MemoryStatus,
        *,
        approver: str = "",
    ) -> LegalMemory:
        allowed: dict[MemoryStatus, set[MemoryStatus]] = {
            "proposed": {"approved", "rejected"},
            "approved": {"active", "rejected"},
            "active": {"stale", "archived"},
            "stale": {"active", "archived"},
            "rejected": {"archived"},
            "archived": set(),
        }
        memories = self.list()
        for index, memory in enumerate(memories):
            if memory.memory_id != memory_id:
                continue
            if target not in allowed[memory.status]:
                raise ValueError(f"invalid memory transition: {memory.status} -> {target}")
            now = time.time()
            update: dict[str, object] = {
                "status": target,
                "status_changed_at": now,
            }
            if target == "approved":
                update.update(
                    {
                        "approved_by_human": True,
                        "approved_by": approver,
                        "approved_at": now,
                        "approved_advice": memory.proposed_advice,
                    }
                )
            elif target == "rejected":
                update.update(
                    {
                        "approved_by_human": False,
                        "approved_by": approver,
                        "approved_advice": "",
                    }
                )
            updated = memory.model_copy(update=update)
            memories[index] = updated
            self.save(memories)
            return updated
        raise KeyError(f"memory not found: {memory_id}")

    def _requires_human_approval(self, run: ReviewRun, finding: object) -> bool:
        risk_level = str(getattr(finding, "risk_level", ""))
        rule_hits = set(getattr(finding, "rule_hits", []) or [])
        injection = run.mcp_context.get("injection", {})
        injection_detected = bool(
            isinstance(injection, dict) and injection.get("detected")
        )
        return bool(
            getattr(finding, "requires_human_review", False)
            or (self.policy.require_human_for_high_risk and risk_level == "high")
            or "llm_semantic_candidate" in rule_hits
            or injection_detected
        )

    def mark_used(self, memory_ids: list[str]) -> int:
        """Reinforce recalled memories: usage feeds back into recall ranking and eviction."""

        if not memory_ids:
            return 0
        wanted = set(memory_ids)
        memories = self.list()
        now = time.time()
        touched = 0
        for index, memory in enumerate(memories):
            if memory.memory_id in wanted and memory.status in {"approved", "active"}:
                memories[index] = memory.model_copy(update={"use_count": memory.use_count + 1, "last_used_at": now})
                touched += 1
        if touched:
            self.save(memories)
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
        rows = [
            json.dumps(
                mask_value(
                    item.model_copy(
                        update={"status": "archived", "status_changed_at": time.time()}
                    ).model_dump(mode="json")
                ),
                ensure_ascii=False,
            )
            for item in evicted
        ]
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        atomic_write_text(path, previous + "\n".join(rows) + "\n")

    def write_index(self, memories: list[LegalMemory] | None = None) -> Path:
        memories = self.list() if memories is None else memories
        grouped: dict[str, list[LegalMemory]] = {}
        for memory in memories:
            grouped.setdefault(
                f"{memory.tenant_id}/{memory.contract_type}", []
            ).append(memory)
        lines = ["# Legal Review Memory Index", ""]
        for contract_type in sorted(grouped):
            lines.append(f"## {contract_type}")
            for memory in sorted(grouped[contract_type], key=lambda item: item.memory_id):
                lines.append(
                    f"- `{memory.memory_id}` [{memory.status}/{memory.type}/{memory.risk_type}/{memory.risk_level}] "
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


def _memory_key(
    tenant_id: str, contract_type: str, risk_type: str, summary: str
) -> tuple[str, str, str, str]:
    return tenant_id, contract_type, risk_type, _normalize(summary)


def _status_bucket(status: MemoryStatus) -> str:
    if status == "proposed":
        return "pending"
    if status in {"approved", "active"}:
        return "trusted"
    return ""
