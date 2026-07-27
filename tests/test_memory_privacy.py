"""Tests for memory lifecycle, PII masking, and RRF fusion."""

from __future__ import annotations

import base64
import time
from pathlib import Path

from legalworkbench.llm.client import LlmClient, LlmConfig, LlmResponse
from legalworkbench.memory import LegalMemoryStore
from legalworkbench.memory.manager import MemoryWritePolicy
from legalworkbench.models import LegalMemory, RetrievedEvidence, ReviewRun, RiskFinding
from legalworkbench.privacy import (
    mask,
    mask_value,
    restore,
    scan,
    valid_bank_card,
    valid_id_card,
)
from legalworkbench.privacy_migration import migrate_private_storage
from legalworkbench.rag.service import LegalRagService, RagConfig
from legalworkbench.retrieval import retrieve_memories
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.secure_storage import (
    MAGIC,
    secure_read_text,
    secure_write_text,
)
from legalworkbench.tools.base import ToolContext, ToolRegistry, ToolResult


def make_finding(
    risk_type: str = "unlimited_liability", summary: str = "责任无上限"
) -> RiskFinding:
    return RiskFinding(
        finding_id="F001",
        clause_id="c1",
        clause_title="赔偿",
        risk_type=risk_type,
        risk_level="medium",
        summary=summary,
        suggestion="加上限",
        evidence=[
            RetrievedEvidence(
                entry_id="e1",
                title="t",
                source="company_policy",
                score=1.0,
                reason="",
                body_preview="b",
                risk_type=risk_type,
                risk_level="medium",
                rerank_score=1.0,
            )
        ],
        requires_human_review=False,
    )


def make_run(run_id: str = "law_test1") -> ReviewRun:
    now = time.time()
    return ReviewRun(
        review_run_id=run_id,
        contract_path="x.md",
        status="completed",
        created_at=now,
        updated_at=now,
        contract_type="SaaS",
        findings=[make_finding()],
    )


def test_memory_reinforces_instead_of_duplicating(tmp_path: Path) -> None:
    store = LegalMemoryStore(tmp_path)
    created_first = store.consolidate_from_run(make_run("law_a"))
    assert len(created_first) == 1
    base_confidence = created_first[0].confidence

    created_second = store.consolidate_from_run(make_run("law_b"))
    assert created_second == []  # 不产生重复记忆
    memories = store.list()
    assert len(memories) == 1
    assert memories[0].reinforce_count == 1
    assert memories[0].confidence > base_confidence
    assert memories[0].source_review_run_id == "law_a"  # 溯源保留首次来源


def test_memory_mark_used_feeds_recall_ranking(tmp_path: Path) -> None:
    store = LegalMemoryStore(tmp_path)
    store.consolidate_from_run(make_run())
    memory_id = store.list()[0].memory_id
    assert store.mark_used([memory_id]) == 1
    updated = store.list()[0]
    assert updated.use_count == 1
    assert updated.last_used_at > 0

    fresh_used = updated
    stale = updated.model_copy(
        update={
            "memory_id": "mem_stale",
            "use_count": 0,
            "last_used_at": 0.0,
            "created_at": time.time() - 400 * 86_400,
        }
    )
    ranked = retrieve_memories(
        [stale, fresh_used], "赔偿 责任 上限", contract_type="SaaS", top_k=2
    )
    assert ranked[0].memory_id == fresh_used.memory_id  # 使用强化 + 时间衰减决定排序


def test_memory_eviction_archives_lowest_retention(tmp_path: Path) -> None:
    store = LegalMemoryStore(tmp_path, policy=MemoryWritePolicy(max_entries=2))
    now = time.time()
    memories = [
        LegalMemory(
            memory_id=f"mem_{i}",
            type="semantic",
            summary=f"s{i}",
            confidence=0.8,
            created_at=now,
            use_count=i,
        )
        for i in range(3)
    ]
    kept = store._evict_if_needed(memories)
    assert len(kept) == 2
    assert {m.memory_id for m in kept} == {"mem_1", "mem_2"}  # use_count 最低的被驱逐
    archive = tmp_path / ".lawbench" / "memory_archive.jsonl"
    assert archive.exists() and "mem_0" in archive.read_text(encoding="utf-8")


def test_pii_scan_mask_restore_roundtrip() -> None:
    text = (
        "甲方联系人：张三，身份证号 11010519491231002X，手机 13812345678，"
        "邮箱 zhangsan@example.com，收款账户 4111111111111111。手机再次出现：13812345678。"
    )
    counts = scan(text)
    assert counts == {
        "id_card": 1,
        "phone": 2,
        "email": 1,
        "bank_card": 1,
        "person_name": 1,
    }

    result = mask(text)
    assert "11010519491231002X" not in result.masked_text
    assert "13812345678" not in result.masked_text
    assert result.masked_text.count("[PII_PHONE_1]") == 2  # 同值同占位符
    assert restore(result.masked_text, result.mapping) == text


def test_local_name_and_address_entity_recognition() -> None:
    text = "法定代表人：张三，送达地址：北京市朝阳区建国路88号。"

    assert scan(text) == {"person_name": 1, "address": 1}
    result = mask(text)

    assert "张三" not in result.masked_text
    assert "北京市朝阳区建国路88号" not in result.masked_text
    assert "[PII_PERSON_NAME_1]" in result.masked_text
    assert "[PII_ADDRESS_1]" in result.masked_text
    assert restore(result.masked_text, result.mapping) == text


def test_name_entity_recognition_avoids_contact_field_labels() -> None:
    assert scan("联系人手机 13812345678") == {"phone": 1}


def test_envelope_encryption_and_migration(tmp_path: Path, monkeypatch) -> None:
    key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("LEGAL_WORKBENCH_ENCRYPTION_PROVIDER", "env")
    monkeypatch.setenv("LEGAL_WORKBENCH_ENCRYPTION_KEY", key)
    encrypted = tmp_path / "secret.txt"

    secure_write_text(encrypted, "联系人：张三", cwd=tmp_path, purpose="test")

    assert encrypted.read_bytes().startswith(MAGIC)
    assert "张三".encode() not in encrypted.read_bytes()
    assert secure_read_text(encrypted, cwd=tmp_path) == "联系人：张三"

    run_path = tmp_path / ".lawbench" / "runs" / "legacy.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(
        '{"contact":"姓名：李四，地址：上海市浦东新区世纪大道100号。"}',
        encoding="utf-8",
    )
    upload = tmp_path / ".lawbench" / "uploads" / "legacy.md"
    upload.parent.mkdir(parents=True)
    upload.write_text("姓名：王五", encoding="utf-8")

    result = migrate_private_storage(tmp_path)

    assert result["masked_files"] == 1
    assert "李四" not in run_path.read_text(encoding="utf-8")
    assert upload.read_bytes().startswith(MAGIC)
    assert secure_read_text(upload, cwd=tmp_path) == "姓名：王五"


def test_mask_value_recursively_sanitizes_structured_data() -> None:
    payload = {
        "clause": "联系人 13812345678",
        "nested": ["11010519491231002X", {"email": "legal@example.com"}],
        "count": 2,
    }

    masked = mask_value(payload)

    assert masked["clause"] == "联系人 [PII_PHONE_1]"
    assert masked["nested"][0] == "[PII_ID_CARD_1]"
    assert masked["nested"][1]["email"] == "[PII_EMAIL_1]"
    assert masked["count"] == 2


def test_tool_trace_masks_input_and_output_summaries(tmp_path: Path) -> None:
    class EchoTool:
        name = "echo"
        description = "test"

        def execute(self, arguments, context):
            del context
            return ToolResult(output=arguments, summary="结果手机 13812345678")

    registry = ToolRegistry()
    registry.register(EchoTool())
    _, trace = registry.execute(
        "echo",
        {"text": "联系人 13812345678"},
        ToolContext(cwd=tmp_path, review_run_id="law_private"),
    )

    assert "13812345678" not in trace.input_summary
    assert "13812345678" not in trace.output_summary
    assert "[PII_PHONE_1]" in trace.input_summary
    assert "[PII_PHONE_1]" in trace.output_summary


def test_pii_validators_reject_random_numbers() -> None:
    assert valid_id_card("11010519491231002X") is True
    assert valid_id_card("110105194912310021") is False  # 校验位错误
    assert valid_bank_card("4111111111111111") is True
    assert valid_bank_card("1234567890123456") is False  # Luhn 失败
    assert scan("合同编号 20240001，金额 1000000 元") == {}  # 普通数字不误报


def test_remote_llm_sends_masked_text_and_restores_reply() -> None:
    captured = {}

    class CapturingLlm(LlmClient):
        def _openai_compatible(self, *, system: str, user: str) -> LlmResponse:
            captured["user"] = user
            return LlmResponse(
                text=f'{{"score": 0.9, "contact": "[PII_PHONE_1]"}}', model="fake"
            )

    client = CapturingLlm(
        LlmConfig(
            provider="openai_compatible",
            model="m",
            base_url="http://fake",
            api_key="k",
            mask_pii=True,
        )
    )
    response = client.complete(system="s", user="联系人手机 13812345678，请判断风险。")
    assert "13812345678" not in captured["user"]  # 明文不出境
    assert "[PII_PHONE_1]" in captured["user"]
    assert "13812345678" in response.text  # 本地回填


def test_review_records_privacy_scan(tmp_path: Path) -> None:
    runtime = LegalAgentRuntime(tmp_path)
    runtime.init_samples()
    contract = tmp_path / "sensitive.md"
    contract.write_text(
        "## 联系方式\n甲方联系人手机 13812345678。\n## 赔偿责任\n乙方承担全部损失且不设赔偿责任上限。\n",
        encoding="utf-8",
    )
    run = runtime.review(contract)
    privacy = run.mcp_context["privacy"]
    assert privacy["sensitive"] is True
    assert privacy["pii_counts"] == {"phone": 1}
    assert all("13812345678" not in item.input_summary for item in run.tool_calls)

    persisted = [
        tmp_path / ".lawbench" / "events.jsonl",
        tmp_path / ".lawbench" / "runs" / f"{run.review_run_id}.json",
        tmp_path / ".lawbench" / "sessions" / f"session-{run.review_run_id}.json",
        tmp_path / ".lawbench" / "sessions" / "latest.json",
        Path(run.report_path),
    ]
    for path in persisted:
        content = path.read_text(encoding="utf-8")
        assert "13812345678" not in content, path
    assert "[PII_PHONE_1]" in persisted[1].read_text(encoding="utf-8")


def test_rrf_fusion_returns_ranked_evidence(tmp_path: Path) -> None:
    runtime = LegalAgentRuntime(tmp_path)
    runtime.init_samples()
    service = LegalRagService(tmp_path, config=RagConfig(fusion="rrf"))
    evidence = service.retrieve(
        "乙方承担全部损失且不设赔偿责任上限", contract_type="SaaS", top_k=5
    )
    assert evidence
    assert any("rrf=" in item.reason for item in evidence)
    assert any(item.risk_type == "unlimited_liability" for item in evidence)
