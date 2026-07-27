"""Tests for LLM decision points, reranker, hardened benchmark, and FastAPI web layer."""

from __future__ import annotations

import json
from pathlib import Path
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from legalworkbench.agents.risk_reviewer import _validate_semantic_candidate
from legalworkbench.data.benchmark_factory import CASE_TEMPLATES
from legalworkbench.governance import RiskRuleEngine
from legalworkbench.llm.client import (
    LlmClient,
    LlmConfig,
    LlmResponse,
    _extract_json_object,
)
from legalworkbench.models import RetrievedEvidence
from legalworkbench.rag.reranker import CrossEncoderReranker, build_reranker
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.web import create_app


class FakeRemoteLlm(LlmClient):
    """LlmClient whose remote call is replaced with a canned reply."""

    def __init__(self, reply_text: str) -> None:
        super().__init__(
            LlmConfig(
                provider="openai_compatible",
                model="fake",
                base_url="http://fake",
                api_key="k",
            )
        )
        self.reply_text = reply_text
        self.calls = 0

    def _openai_compatible(self, *, system: str, user: str) -> LlmResponse:
        self.calls += 1
        return LlmResponse(text=self.reply_text, model="fake")


def wait_for_review_task(client: TestClient, task_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] in {"completed", "failed"}:
            return task
        time.sleep(0.02)
    raise AssertionError(f"review task did not finish: {task_id}")


class IndependentCandidateLlm(LlmClient):
    def __init__(self, *, verification_score: float = 0.9) -> None:
        super().__init__(LlmConfig(provider="local"))
        self.verification_score = verification_score

    def discover_risk_candidates(self, **kwargs):
        del kwargs
        return {
            "decision_source": "model",
            "candidates": [
                {
                    "risk_type": "unlimited_liability",
                    "risk_level": "high",
                    "adverse_party": "乙方",
                    "evidence_quote": "补偿范围不受已支付服务费用限制",
                    "rationale": "赔偿范围未受合同价款约束，可能形成开放式责任。",
                    "confidence": 0.9,
                }
            ],
        }

    def semantic_judgment(self, **kwargs):
        del kwargs
        return {
            "score": self.verification_score,
            "reason": "证据条件下的二次语义核验",
        }


def test_extract_json_object_tolerates_fences_and_prose() -> None:
    assert _extract_json_object('{"a": 1}') == {"a": 1}
    assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json_object(
        '好的，结论如下：{"adjust": true, "retrieval_top_k": 12} 供参考'
    ) == {
        "adjust": True,
        "retrieval_top_k": 12,
    }
    assert _extract_json_object("完全不是 JSON") is None
    assert _extract_json_object("[1, 2]") is None


def test_decide_local_provider_is_deterministic() -> None:
    client = LlmClient(LlmConfig(provider="local"))
    plan = client.decide(
        task="plan_review",
        payload={"contract_type": "SaaS"},
        fallback={"adjust": False},
    )
    assert plan["adjust"] is False
    assert plan["decision_source"] == "local_rules"
    refine_empty = client.decide(
        task="refine_query",
        payload={"evidence_count": 0, "clause_title": "赔偿"},
        fallback={},
    )
    assert refine_empty["refine"] is True
    assert "赔偿" in refine_empty["query"]
    refine_ok = client.decide(
        task="refine_query", payload={"evidence_count": 5}, fallback={}
    )
    assert refine_ok["refine"] is False
    discovery = client.discover_risk_candidates(
        clause="双方应依法履行合同。",
        contract_type="general",
        allowed_risk_types=["unlimited_liability"],
    )
    assert discovery["candidates"] == []
    assert discovery["decision_source"] == "local_rules"


def test_decide_remote_parses_model_reply_and_falls_back_on_garbage() -> None:
    good = FakeRemoteLlm('```json\n{"adjust": true, "retrieval_top_k": 15}\n```')
    decision = good.decide(task="plan_review", payload={}, fallback={"adjust": False})
    assert decision["adjust"] is True
    assert decision["decision_source"] == "model"

    garbage = FakeRemoteLlm("我觉得都挺好的")
    decision = garbage.decide(
        task="plan_review", payload={}, fallback={"adjust": False}
    )
    assert decision["adjust"] is False
    assert decision["decision_source"] == "fallback"


def test_ollama_provider_has_default_endpoint() -> None:
    client = LlmClient(LlmConfig(provider="ollama", model="qwen2.5:7b"))
    base_url, api_key = client.remote_endpoint()
    assert base_url.startswith("http://127.0.0.1:11434")
    assert api_key
    assert LlmClient(LlmConfig(provider="local")).remote_endpoint() is None


def test_planner_llm_decision_is_bounded_and_whitelisted(tmp_path: Path) -> None:
    runtime = LegalAgentRuntime(tmp_path)
    paths = runtime.init_samples()
    # 模型试图给出越界 top_k 和不存在的风险类型：必须被代码边界拦住
    runtime.llm = FakeRemoteLlm(
        json.dumps(
            {
                "adjust": True,
                "retrieval_top_k": 99,
                "extra_risk_focus": ["auto_renewal", "hallucinated_type"],
            }
        )
    )
    run = runtime.review(paths["contract"])
    profile = run.mcp_context["skill_profile"]
    assert profile["retrieval_top_k"] == 20
    assert "auto_renewal" in profile["risk_focus"]
    assert "hallucinated_type" not in profile["risk_focus"]
    assert run.mcp_context["llm_plan"]["decision_source"] == "model"


def test_semantic_judgment_degrades_on_remote_failure() -> None:
    class FlakyLlm(LlmClient):
        calls = 0

        def _openai_compatible(self, *, system: str, user: str) -> LlmResponse:
            self.calls += 1
            raise ConnectionError("SSL: UNEXPECTED_EOF_WHILE_READING")

    client = FlakyLlm(
        LlmConfig(
            provider="openai_compatible", model="m", base_url="http://x", api_key="k"
        )
    )
    judgment = client.semantic_judgment(
        clause="乙方承担全部损失且不设赔偿责任上限",
        risk_type="unlimited_liability",
        evidence="e",
    )
    # 远端故障不抛异常：降级到本地确定性打分并标记 degraded，主链路继续
    assert judgment["score"] == 0.82
    assert "remote llm unavailable" in judgment["degraded"]

    # The first failure opens the circuit; later clauses fall back immediately.
    second = client.semantic_judgment(
        clause="乙方承担全部损失且不设赔偿责任上限",
        risk_type="unlimited_liability",
        evidence="e",
    )
    assert second["score"] == judgment["score"]
    assert "circuit open" in second["degraded"]
    assert client.calls == 1


def test_llm_timeout_is_not_retried(monkeypatch) -> None:
    calls: list[float] = []

    class TimeoutClient:
        def __init__(self, *, timeout: float) -> None:
            calls.append(timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

        def post(self, *args, **kwargs):
            del args, kwargs
            raise httpx.ReadTimeout("provider did not respond")

    monkeypatch.setattr(httpx, "Client", TimeoutClient)
    client = LlmClient(
        LlmConfig(
            provider="openai_compatible",
            model="m",
            base_url="http://x",
            api_key="k",
            timeout_seconds=0.01,
        )
    )

    with pytest.raises(httpx.ReadTimeout):
        client.complete(system="s", user="u")

    assert calls == [0.01]


def test_semantic_candidate_validation_requires_grounded_whitelisted_output() -> None:
    clause = "补偿范围不受已支付服务费用限制。"
    valid = {
        "risk_type": "unlimited_liability",
        "risk_level": "high",
        "adverse_party": "乙方",
        "evidence_quote": "补偿范围不受已支付服务费用限制",
        "rationale": "赔偿范围没有合同金额上限。",
        "confidence": 0.88,
    }
    assert _validate_semantic_candidate(valid, clause) is not None
    assert (
        _validate_semantic_candidate({**valid, "risk_type": "invented_risk"}, clause)
        is None
    )
    assert (
        _validate_semantic_candidate(
            {**valid, "evidence_quote": "合同中不存在的原文"}, clause
        )
        is None
    )
    assert _validate_semantic_candidate({**valid, "confidence": 0.4}, clause) is None


def test_llm_independent_candidate_can_create_grounded_finding(tmp_path: Path) -> None:
    runtime = LegalAgentRuntime(tmp_path)
    runtime.init_samples()
    runtime.llm = IndependentCandidateLlm()
    contract = tmp_path / "semantic-risk.md"
    contract.write_text(
        "# 服务协议\n\n## 责任承担\n因履行本协议产生的相关损失均由乙方负责补足，补偿范围不受已支付服务费用限制。\n",
        encoding="utf-8",
    )
    assert RiskRuleEngine().evaluate(contract.read_text(encoding="utf-8")) == []

    run = runtime.review(contract)

    finding = next(
        item for item in run.findings if item.risk_type == "unlimited_liability"
    )
    assert "llm_semantic_candidate" in finding.rule_hits
    assert finding.evidence
    assert all(item.risk_type == "unlimited_liability" for item in finding.evidence)
    assert finding.requires_human_review is True
    semantic_steps = [
        step
        for step in run.mcp_context["agent_steps"]
        if step["agent"] == "risk_reviewer_agent"
        and step["action"] == "semantic_candidates"
    ]
    assert any(
        step["payload"]["risk_types"] == ["unlimited_liability"]
        for step in semantic_steps
    )


def test_llm_independent_candidate_requires_second_semantic_verification(
    tmp_path: Path,
) -> None:
    runtime = LegalAgentRuntime(tmp_path)
    runtime.init_samples()
    runtime.llm = IndependentCandidateLlm(verification_score=0.2)
    contract = tmp_path / "rejected-semantic-risk.md"
    contract.write_text(
        "# 服务协议\n\n## 责任承担\n因履行本协议产生的相关损失均由乙方负责补足，补偿范围不受已支付服务费用限制。\n",
        encoding="utf-8",
    )

    run = runtime.review(contract)

    assert not run.findings
    rejected_steps = [
        step
        for step in run.mcp_context["agent_steps"]
        if step["action"] == "semantic_candidate_rejected"
    ]
    assert (
        rejected_steps[0]["payload"]["reason"]
        == "semantic_verification_below_threshold"
    )


def test_cross_encoder_rerank_rescores_and_resorts() -> None:
    class StubModel:
        def predict(self, pairs):
            # 反转原有排序：给最后一个候选最高分
            return list(range(len(pairs)))

    reranker = object.__new__(CrossEncoderReranker)
    reranker.name = "stub"
    reranker.model = StubModel()
    evidence = [
        RetrievedEvidence(
            entry_id=f"e{i}",
            title=f"t{i}",
            source="s",
            score=10 - i,
            reason="",
            body_preview="b",
            risk_type="general",
            risk_level="low",
            rerank_score=10 - i,
        )
        for i in range(3)
    ]
    result = reranker.rerank("q", evidence)
    assert result[0].entry_id == "e2"
    assert "cross_encoder=" in result[0].reason


def test_build_reranker_formula_provider_keeps_formula_path() -> None:
    reranker, err = build_reranker("formula", "BAAI/bge-reranker-base")
    assert reranker is None
    assert err == ""


def test_rule_engine_stays_silent_on_balanced_clauses() -> None:
    """精确率护栏：不利模式规则不得在均衡/留白条款上触发。

    旧版话题关键词规则在真实示范文本上误报泛滥（real benchmark 实测
    rule_only precision 0.14），重写为不利模式检测后，这些真实合同里的
    均衡表述必须零命中。
    """

    rules = RiskRuleEngine()
    balanced_clauses = [
        # 双向对等违约赔偿（真实合同最常见的"提到赔偿但均衡"）
        "任何一方违反本合同约定给对方造成损失的，应当依法承担相应的赔偿责任。",
        # 常规付款约定（提到付款/支付但无前置风险）
        "甲方应按照本合同约定的时间和方式向乙方支付合同价款，乙方开具合法有效发票。",
        # 留白选择式争议解决
        "因本合同发生争议，双方协商解决；协商不成的，按下列第____种方式解决：（一）提交____仲裁委员会仲裁；（二）依法向人民法院起诉。",
        # 双向不可抗力（含通知与减损义务）
        "因不可抗力不能履行合同的，受影响方应当及时通知对方并提供证明，双方均不承担违约责任，并应采取措施防止损失扩大。",
        # 常规保密义务（双向、有期限）
        "双方对在履行本合同过程中知悉的对方商业秘密承担保密义务，保密期限为合同终止后三年。",
        # 常规押金返还约定
        "合同期满后，乙方应在核算相关费用后七日内将剩余押金无息返还甲方。",
        # 法定任意解除权复述（民法典委托合同）
        "委托人或者受托人可以随时解除委托合同。因解除合同造成对方损失的，除不可归责于该当事人的事由外，应当赔偿损失。",
    ]
    for text in balanced_clauses:
        hits = rules.evaluate(text)
        assert not hits, (
            f"rule fired on balanced clause: {[h.rule_id for h in hits]} <- {text[:30]}"
        )


def test_rule_engine_catches_adverse_patterns_including_paraphrase() -> None:
    """不利模式规则要接住合成集里的显式与改写措辞风险条款。"""

    rules = RiskRuleEngine()
    covered = 0
    for _, text, expected in CASE_TEMPLATES:
        rule_risks = {hit.risk_type for hit in rules.evaluate(text)}
        covered += len(rule_risks & set(expected))
    total = sum(len(expected) for _, _, expected in CASE_TEMPLATES)
    assert covered / total >= 0.9, f"rule coverage dropped: {covered}/{total}"


def test_fastapi_app_end_to_end(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/init").status_code == 200
        review = client.post(
            "/api/review",
            json={"contract_text": "## 赔偿责任\n乙方承担全部损失且不设赔偿责任上限。"},
        )
        assert review.status_code == 202
        accepted = review.json()
        assert accepted["status"] in {"pending", "running"}
        body = wait_for_review_task(client, accepted["task_id"])
        assert body["status"] == "completed"
        assert body["review_run_id"].startswith("law_")

        report = client.get(f"/api/report/{body['review_run_id']}")
        assert report.status_code == 200
        assert "Agent" in report.json()["markdown"]

        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["runs"]

        assert client.post("/api/review", json={}).status_code == 400
        assert client.get("/api/report/nonexistent").status_code == 404

        page = client.get("/")
        assert page.status_code == 200
        assert "EventSource" in page.text


def test_fastapi_sse_stream_pushes_events(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/init")
        accepted = client.post(
            "/api/review", json={"contract_text": "## 管辖\n由乙方所在地人民法院管辖。"}
        ).json()
        wait_for_review_task(client, accepted["task_id"])
        response = client.get("/api/events/stream", params={"cycles": 1})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: events" in response.text
        assert "review.completed" in response.text
