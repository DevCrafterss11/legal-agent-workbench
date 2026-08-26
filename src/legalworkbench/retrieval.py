"""Hybrid retrieval and memory recall."""

from __future__ import annotations

import math
import re
from collections import Counter

from legalworkbench.models import KnowledgeEntry, LegalMemory, RetrievedEvidence


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    ascii_tokens = re.findall(r"[a-z0-9_]+", lowered)
    han_tokens = re.findall(r"[\u4e00-\u9fff]", text)
    return [token for token in ascii_tokens if len(token) >= 2] + han_tokens


class HybridClauseRetriever:
    """Local hybrid retrieval: BM25-style lexical + lightweight semantic overlap."""

    def __init__(self, entries: list[KnowledgeEntry]) -> None:
        self.entries = entries
        self.docs = [tokenize(entry_text(entry)) for entry in entries]
        self.avgdl = sum(len(doc) for doc in self.docs) / max(1, len(self.docs))
        self.df: Counter[str] = Counter()
        for doc in self.docs:
            for token in set(doc):
                self.df[token] += 1

    def search(self, query: str, *, contract_type: str = "general", top_k: int = 10, rerank: bool = True) -> list[RetrievedEvidence]:
        query_tokens = tokenize(query)
        query_set = set(query_tokens)
        scored: list[tuple[float, KnowledgeEntry, str]] = []
        for entry, doc in zip(self.entries, self.docs):
            lexical = self._bm25(query_tokens, doc)
            overlap = len(query_set & set(doc)) / max(1, len(query_set))
            metadata = 0.35 if entry.contract_type.lower() == contract_type.lower() else 0.0
            if entry.contract_type == "general":
                metadata += 0.1
            score = lexical + overlap * 1.5 + metadata + _trusted_source_boost(entry) + _risk_intent_boost(entry.risk_type, query)
            if score <= 0:
                continue
            reason = f"lexical={lexical:.2f}, overlap={overlap:.2f}, metadata={metadata:.2f}"
            scored.append((score, entry, reason))
        scored.sort(key=lambda item: item[0], reverse=True)
        candidates = [
            RetrievedEvidence(
                entry_id=entry.id,
                title=entry.title,
                source=entry.source,
                score=round(score, 4),
                reason=reason,
                body_preview=entry.body[:240],
                risk_type=entry.risk_type,
                risk_level=entry.risk_level,
                rerank_score=0.0,
            )
            for score, entry, reason in scored[: max(top_k, 128)]
        ]
        if rerank:
            candidates = rerank_evidence(query, candidates, contract_type=contract_type)
        return diversify_evidence(candidates, top_k=top_k)

    def _bm25(self, query_tokens: list[str], doc: list[str]) -> float:
        if not doc:
            return 0.0
        tf = Counter(doc)
        k1 = 1.5
        b = 0.75
        n_docs = max(1, len(self.docs))
        total = 0.0
        for token in query_tokens:
            freq = tf[token]
            if freq == 0:
                continue
            df = self.df[token]
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            denom = freq + k1 * (1 - b + b * len(doc) / max(1.0, self.avgdl))
            total += idf * (freq * (k1 + 1)) / denom
        return total


def retrieve_memories(
    memories: list[LegalMemory],
    query: str,
    *,
    contract_type: str,
    top_k: int = 5,
    tenant_id: str = "local",
) -> list[LegalMemory]:
    """Recall ranking = 相关性 + 企业上下文匹配 + 使用强化 + 时间衰减。

    时间衰减只作用于携带时间戳的记忆（半衰期 180 天），旧数据（created_at=0）
    不衰减，保证升级兼容。
    """

    import math
    import time as _time

    now = _time.time()
    tokens = set(tokenize(query))
    scored: list[tuple[float, LegalMemory]] = []
    for memory in memories:
        if memory.tenant_id != tenant_id:
            continue
        # PROPOSED/REJECTED/STALE/ARCHIVED 只用于治理和审计，不能进入 Agent 上下文。
        if memory.status not in {"approved", "active"}:
            continue
        text = " ".join([memory.summary, memory.approved_advice, memory.contract_type, memory.clause_type, memory.risk_type, " ".join(memory.tags)])
        doc_tokens = set(tokenize(text))
        score = len(tokens & doc_tokens) / max(1, len(tokens))
        if memory.contract_type.lower() == contract_type.lower():
            score += 0.25
        if memory.approved_by_human:
            score += 0.15
        score += min(memory.use_count, 5) * 0.03
        score += min(memory.reinforce_count, 3) * 0.04
        reference = memory.last_used_at or memory.created_at
        if reference > 0:
            age_days = max(0.0, (now - reference) / 86_400)
            score *= math.pow(0.5, age_days / 180)
        if score > 0:
            scored.append((score, memory))
    scored.sort(key=lambda item: (item[0], item[1].confidence), reverse=True)
    return [memory for _, memory in scored[:top_k]]


def entry_text(entry: KnowledgeEntry) -> str:
    return " ".join([entry.title, entry.body, entry.contract_type, entry.clause_type, entry.risk_type, entry.risk_level, " ".join(entry.tags)])


def semantic_overlap_score(query: str, text: str) -> float:
    """A deterministic semantic proxy used when no embedding server is configured."""

    query_tokens = set(tokenize(query))
    text_tokens = set(tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    overlap = len(query_tokens & text_tokens) / max(1, len(query_tokens))
    containment = len(query_tokens & text_tokens) / max(1, min(len(query_tokens), len(text_tokens)))
    return round(overlap * 0.65 + containment * 0.35, 4)


def rerank_evidence(query: str, evidence: list[RetrievedEvidence], *, contract_type: str) -> list[RetrievedEvidence]:
    """Rerank evidence with semantic, metadata, and risk-priority signals."""

    risk_priority = {"high": 0.18, "medium": 0.1, "low": 0.03}
    reranked: list[RetrievedEvidence] = []
    for item in evidence:
        semantic = semantic_overlap_score(query, " ".join([item.title, item.body_preview, item.risk_type]))
        source_boost = 0.08 if item.source.startswith(("company_policy", "playbook")) else 0.0
        contract_boost = 0.05 if contract_type.lower() in item.source.lower() or contract_type.lower() in item.title.lower() else 0.0
        rerank_score = item.score * 0.55 + semantic * 8.0 + source_boost + contract_boost + risk_priority.get(item.risk_level, 0.0)
        updated = item.model_copy(update={"rerank_score": round(rerank_score, 4), "reason": f"{item.reason}, semantic={semantic:.2f}, rerank={rerank_score:.2f}"})
        reranked.append(updated)
    reranked.sort(key=lambda item: (item.rerank_score, item.score), reverse=True)
    return reranked


def diversify_evidence(evidence: list[RetrievedEvidence], *, top_k: int) -> list[RetrievedEvidence]:
    """Keep top relevance while preventing one risk type from crowding out others."""

    selected: list[RetrievedEvidence] = []
    seen_risks: set[str] = set()
    for item in evidence:
        if item.risk_type == "general" or item.risk_type in seen_risks:
            continue
        selected.append(item)
        seen_risks.add(item.risk_type)
        if len(selected) >= max(1, top_k // 2):
            break
    selected_ids = {item.entry_id for item in selected}
    for item in evidence:
        if item.entry_id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item.entry_id)
        if len(selected) >= top_k:
            break
    return selected[:top_k]


def _trusted_source_boost(entry: KnowledgeEntry) -> float:
    if entry.source.startswith("company_policy"):
        return 35.0
    if entry.source.startswith(("playbook", "template")):
        return 24.0
    return 0.0


def _risk_intent_boost(risk_type: str, query: str) -> float:
    keywords = {
        "auto_renewal": ("自动续约", "续约"),
        "unlimited_liability": ("全部损失", "责任上限", "无限责任", "不设赔偿责任上限", "间接损失", "预期利润"),
        "data_security": ("客户数据", "个人信息", "数据泄露", "安全措施", "通知时限"),
        "payment_acceptance": ("付款", "支付", "验收", "交付", "发票"),
        "payment_cycle": ("签署后 5 日", "预付全部", "一次性支付全部", "付款周期"),
        "ip_ownership": ("知识产权", "交付成果", "归乙方所有", "成果归乙方"),
        "sla_remedy": ("SLA", "服务可用性", "故障等级", "响应时间", "服务抵扣"),
        "jurisdiction": ("管辖", "法院", "仲裁", "争议"),
        "confidentiality": ("保密", "商业秘密", "秘密信息", "保密期限", "保密范围"),
        "termination_notice": ("单方解除", "任意解除", "提前终止", "书面通知", "整改期"),
        "force_majeure": ("不可抗力", "及时通知", "证明", "减损"),
        "deposit_return": ("押金", "保证金", "返还", "扣除", "退还期限"),
        "prepaid_refund": ("预付式", "预付款", "储值", "充值", "退款", "退费"),
    }
    if any(item in query for item in keywords.get(risk_type, ())):
        return 18.0
    return 0.0
