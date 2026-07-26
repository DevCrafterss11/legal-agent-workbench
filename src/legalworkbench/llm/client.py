"""OpenAI-compatible LLM client with deterministic local fallback."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


OLLAMA_DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"


def load_llm_config(cwd: Any = None) -> "LlmConfig":
    """Resolve LLM config: env vars > settings.json llm section > local fallback.

    api_key 只从环境变量或 secrets.json 读取，settings.json 不落敏感信息。
    """

    settings: dict[str, Any] = {}
    secrets: dict[str, Any] = {}
    try:
        from legalworkbench.paths import secrets_path, settings_path

        for target, path in ((settings, settings_path(cwd)), (secrets, secrets_path(cwd))):
            if path.exists():
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    target.update(parsed)
    except (OSError, json.JSONDecodeError):
        pass
    section = settings.get("llm") if isinstance(settings.get("llm"), dict) else {}
    return LlmConfig(
        provider=os.environ.get("LEGAL_WORKBENCH_LLM_PROVIDER") or str(section.get("provider") or "local"),
        model=os.environ.get("LEGAL_WORKBENCH_LLM_MODEL") or str(section.get("model") or "local-legal-reviewer"),
        base_url=os.environ.get("LEGAL_WORKBENCH_LLM_BASE_URL") or str(section.get("base_url") or ""),
        api_key=os.environ.get("LEGAL_WORKBENCH_LLM_API_KEY") or str(secrets.get("llm_api_key") or ""),
        timeout_seconds=float(section.get("timeout_seconds") or 30.0),
        mask_pii=bool(section.get("mask_pii", True)),
    )


@dataclass(frozen=True)
class LlmConfig:
    provider: str = "local"
    model: str = "local-legal-reviewer"
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 30.0
    mask_pii: bool = True


@dataclass(frozen=True)
class LlmResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] | None = None


class LlmClient:
    """Small provider boundary for semantic judgment and generation."""

    def __init__(
        self,
        config: LlmConfig | None = None,
        *,
        cwd: Any = None,
        cache: Any = None,
        cache_ttl_seconds: int = 7 * 86_400,
    ) -> None:
        self.config = config or load_llm_config(cwd)
        self.cache = cache
        self.cache_ttl_seconds = cache_ttl_seconds

    def remote_endpoint(self) -> tuple[str, str] | None:
        """Return (base_url, api_key) when a remote provider is usable."""

        provider = self.config.provider
        if provider == "openai_compatible" and self.config.base_url and self.config.api_key:
            return self.config.base_url, self.config.api_key
        if provider == "ollama":
            # Ollama 暴露 OpenAI-compatible 接口且不校验 api_key
            return self.config.base_url or OLLAMA_DEFAULT_BASE_URL, self.config.api_key or "ollama"
        return None

    def complete(self, *, system: str, user: str) -> LlmResponse:
        if self.remote_endpoint() is not None:
            # 隐私边界：合同文本出境（远端 LLM）前先可逆脱敏，映射表只留在进程内；
            # 缓存 key 与缓存值均基于脱敏文本，PII 不落 Redis，回复占位符本地回填
            mapping: dict[str, str] = {}
            outbound_user = user
            if self.config.mask_pii:
                from legalworkbench.privacy import mask as mask_pii

                masked = mask_pii(user)
                outbound_user = masked.masked_text
                mapping = masked.mapping
            # cache-aside：仅缓存远端调用（temperature=0.1 接近幂等）；
            # 本地 deterministic fallback 无网络成本，不缓存
            cache_key = ""
            if self.cache is not None:
                from legalworkbench.cache import content_hash

                cache_key = f"llm:{content_hash(self.config.model, system, outbound_user)}"
                cached = self.cache.get_json(cache_key)
                if cached is not None:
                    from legalworkbench.privacy import restore

                    return LlmResponse(
                        text=restore(str(cached.get("text") or ""), mapping),
                        model=str(cached.get("model") or self.config.model),
                        prompt_tokens=int(cached.get("prompt_tokens") or 0),
                        completion_tokens=int(cached.get("completion_tokens") or 0),
                        raw={"cached": True, "pii_masked": bool(mapping)},
                    )
            response = self._openai_compatible(system=system, user=outbound_user)
            if self.cache is not None and cache_key:
                self.cache.set_json(
                    cache_key,
                    {
                        "text": response.text,
                        "model": response.model,
                        "prompt_tokens": response.prompt_tokens,
                        "completion_tokens": response.completion_tokens,
                    },
                    ttl_seconds=self.cache_ttl_seconds,
                )
            if mapping:
                from legalworkbench.privacy import restore

                response = LlmResponse(
                    text=restore(response.text, mapping),
                    model=response.model,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    raw={**(response.raw or {}), "pii_masked": True},
                )
            return response
        return self._local(system=system, user=user)

    def semantic_judgment(self, *, clause: str, risk_type: str, evidence: str) -> dict[str, Any]:
        prompt = {
            "task": "legal_risk_semantic_judgment",
            "risk_type": risk_type,
            "clause": clause,
            "evidence": evidence,
        }
        response = self.complete(
            system="你是企业法务合同审查助手，只能基于给定证据判断风险相关性。",
            user=json.dumps(prompt, ensure_ascii=False),
        )
        try:
            parsed = json.loads(response.text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"score": 0.5, "reason": response.text[:200], "model": response.model}

    def decide(self, *, task: str, payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        """Structured decision: ask the model for a JSON verdict, fall back on any failure.

        运行期决策点统一走这里：远端模型返回合法 JSON 就采纳；解析失败、网络失败
        或 local provider 都会回落到确定性 fallback，保证主链路不因决策层退化而中断。
        """

        prompt = {"task": task, "respond_with": "single JSON object only", **payload}
        try:
            response = self.complete(
                system=(
                    "你是企业法务 Agent Runtime 的决策模块。"
                    "根据输入 JSON 中的 task 做出决策，只输出一个 JSON 对象，不要输出任何其他文本。"
                ),
                user=json.dumps(prompt, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001 - 远端不可用不阻塞审查主链路
            return {**fallback, "decision_source": "fallback", "error": str(exc)[:200]}
        parsed = _extract_json_object(response.text)
        if parsed is None:
            return {**fallback, "decision_source": "fallback", "error": "unparseable model output"}
        parsed.setdefault("decision_source", "local_rules" if self.remote_endpoint() is None else "model")
        parsed.setdefault("model", response.model)
        return parsed

    def _openai_compatible(self, *, system: str, user: str) -> LlmResponse:
        base_url, api_key = self.remote_endpoint() or (self.config.base_url, self.config.api_key)
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            res = client.post(url, headers=headers, json=payload)
            res.raise_for_status()
            data = res.json()
        usage = data.get("usage") or {}
        text = data["choices"][0]["message"]["content"]
        return LlmResponse(
            text=text,
            model=self.config.model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            raw=data,
        )

    def _local(self, *, system: str, user: str) -> LlmResponse:
        """Deterministic task-aware fallback so the runtime works fully offline."""

        del system
        task = ""
        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(user)
            if isinstance(parsed, dict):
                payload = parsed
                task = str(parsed.get("task") or "")
        except json.JSONDecodeError:
            pass

        if task == "plan_review":
            # 本地规则不调整技能画像，保持既有确定性行为
            body: dict[str, Any] = {"adjust": False, "reason": "local deterministic planner keeps skill profile"}
        elif task == "refine_query":
            evidence_count = int(payload.get("evidence_count") or 0)
            if evidence_count == 0:
                clause_title = str(payload.get("clause_title") or "")
                body = {"refine": True, "query": f"{clause_title} 合同条款 风险 责任".strip()}
            else:
                body = {"refine": False, "reason": "local deterministic refiner only retries on empty evidence"}
        else:
            lowered = user.lower()
            score = 0.35
            if any(token in lowered for token in ("不设赔偿", "无限责任", "数据", "自动续约", "知识产权", "管辖")):
                score = 0.82
            body = {
                "score": score,
                "reason": "local deterministic legal semantic judgment",
                "requires_human_review": score >= 0.8,
            }
        text = json.dumps(body, ensure_ascii=False)
        return LlmResponse(text=text, model=self.config.model, prompt_tokens=len(user), completion_tokens=len(text))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object in a model reply, tolerating markdown fences."""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None
