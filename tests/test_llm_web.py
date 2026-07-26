"""Tests for LLM decision points, reranker, hardened benchmark, and FastAPI web layer."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from legalworkbench.data.benchmark_factory import CASE_TEMPLATES
from legalworkbench.governance import RiskRuleEngine
from legalworkbench.llm.client import LlmClient, LlmConfig, LlmResponse, _extract_json_object
from legalworkbench.models import RetrievedEvidence
from legalworkbench.rag.reranker import CrossEncoderReranker, build_reranker
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.web import create_app


class FakeRemoteLlm(LlmClient):
    """LlmClient whose remote call is replaced with a canned reply."""

    def __init__(self, reply_text: str) -> None:
        super().__init__(LlmConfig(provider="openai_compatible", model="fake", base_url="http://fake", api_key="k"))
        self.reply_text = reply_text
        self.calls = 0

    def _openai_compatible(self, *, system: str, user: str) -> LlmResponse:
        self.calls += 1
        return LlmResponse(text=self.reply_text, model="fake")


def test_extract_json_object_tolerates_fences_and_prose() -> None:
    assert _extract_json_object('{"a": 1}') == {"a": 1}
    assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json_object('好的，结论如下：{"adjust": true, "retrieval_top_k": 12} 供参考') == {
        "adjust": True,
        "retrieval_top_k": 12,
    }
    assert _extract_json_object("完全不是 JSON") is None
    assert _extract_json_object("[1, 2]") is None


def test_decide_local_provider_is_deterministic() -> None:
    client = LlmClient(LlmConfig(provider="local"))
    plan = client.decide(task="plan_review", payload={"contract_type": "SaaS"}, fallback={"adjust": False})
    assert plan["adjust"] is False
    assert plan["decision_source"] == "local_rules"
    refine_empty = client.decide(task="refine_query", payload={"evidence_count": 0, "clause_title": "赔偿"}, fallback={})
    assert refine_empty["refine"] is True
    assert "赔偿" in refine_empty["query"]
    refine_ok = client.decide(task="refine_query", payload={"evidence_count": 5}, fallback={})
    assert refine_ok["refine"] is False


def test_decide_remote_parses_model_reply_and_falls_back_on_garbage() -> None:
    good = FakeRemoteLlm('```json\n{"adjust": true, "retrieval_top_k": 15}\n```')
    decision = good.decide(task="plan_review", payload={}, fallback={"adjust": False})
    assert decision["adjust"] is True
    assert decision["decision_source"] == "model"

    garbage = FakeRemoteLlm("我觉得都挺好的")
    decision = garbage.decide(task="plan_review", payload={}, fallback={"adjust": False})
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
        json.dumps({"adjust": True, "retrieval_top_k": 99, "extra_risk_focus": ["auto_renewal", "hallucinated_type"]})
    )
    run = runtime.review(paths["contract"])
    profile = run.mcp_context["skill_profile"]
    assert profile["retrieval_top_k"] == 20
    assert "auto_renewal" in profile["risk_focus"]
    assert "hallucinated_type" not in profile["risk_focus"]
    assert run.mcp_context["llm_plan"]["decision_source"] == "model"


def test_cross_encoder_rerank_rescores_and_resorts() -> None:
    class StubModel:
        def predict(self, pairs):
            # 反转原有排序：给最后一个候选最高分
            return list(range(len(pairs)))

    reranker = object.__new__(CrossEncoderReranker)
    reranker.name = "stub"
    reranker.model = StubModel()
    evidence = [
        RetrievedEvidence(entry_id=f"e{i}", title=f"t{i}", source="s", score=10 - i, reason="", body_preview="b", risk_type="general", risk_level="low", rerank_score=10 - i)
        for i in range(3)
    ]
    result = reranker.rerank("q", evidence)
    assert result[0].entry_id == "e2"
    assert "cross_encoder=" in result[0].reason


def test_build_reranker_formula_provider_keeps_formula_path() -> None:
    reranker, err = build_reranker("formula", "BAAI/bge-reranker-base")
    assert reranker is None
    assert err == ""


def test_hard_benchmark_cases_dodge_rule_engine() -> None:
    rules = RiskRuleEngine()
    hard = [tpl for tpl in CASE_TEMPLATES if "补偿金额不受合同总额约束" in tpl[1] or "顺延" in tpl[1] or "延展" in tpl[1]]
    assert len(hard) >= 3
    for _, text, expected in hard:
        rule_risks = {hit.risk_type for hit in rules.evaluate(text)}
        assert not (rule_risks & set(expected)), f"hard case leaked into rules: {expected}"


def test_fastapi_app_end_to_end(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/init").status_code == 200
        review = client.post(
            "/api/review",
            json={"contract_text": "## 赔偿责任\n乙方承担全部损失且不设赔偿责任上限。"},
        )
        assert review.status_code == 200
        body = review.json()
        assert body["status"] in {"completed", "blocked"}
        assert body["findings"] >= 1

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
        client.post("/api/review", json={"contract_text": "## 管辖\n由乙方所在地人民法院管辖。"})
        response = client.get("/api/events/stream", params={"cycles": 1})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: events" in response.text
        assert "review.completed" in response.text
