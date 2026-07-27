"""Workbench cache: Redis-backed cache-aside layer with in-process fallback.

用途与语义：

- LLM 响应缓存：cache-aside（先查缓存，miss 再调远端并回填），key 为
  ``sha256(model|system|user)``，带 TTL，天然防穿透（同 prompt 幂等）。
- 幂等去重：``set_if_absent`` 即 SET NX EX，用于飞书消息去重和任务入队去重，
  claim 成功者才继续处理，避免"先查后写"的竞态窗口。
- 降级：Redis 不可用时退化为进程内 dict（带过期），单进程语义不变，
  跨进程去重由文件审计记录兜底。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from legalworkbench.mq.bus import QueueConfig


class WorkbenchCache:
    """Small cache facade so callers never branch on the backend."""

    def __init__(self, *, client: Any = None, prefix: str = "lawbench:cache", fallback_reason: str = "") -> None:
        self.client = client
        self.prefix = prefix
        self.backend = "redis" if client is not None else "memory"
        self.fallback_reason = fallback_reason
        self.hits = 0
        self.misses = 0
        self._memory: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        full = self._key(key)
        if self.client is not None:
            try:
                value = self.client.get(full)
            except Exception:  # noqa: BLE001 - 运行中 Redis 掉线按 miss 处理
                value = None
        else:
            value = self._memory_get(full)
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        return str(value)

    def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        full = self._key(key)
        if self.client is not None:
            try:
                self.client.set(full, value, ex=ttl_seconds)
                return
            except Exception:  # noqa: BLE001
                pass
        self._memory_set(full, value, ttl_seconds)

    def set_if_absent(self, key: str, value: str, *, ttl_seconds: int | None = None) -> bool:
        full = self._key(key)
        if self.client is not None:
            try:
                return bool(self.client.set(full, value, nx=True, ex=ttl_seconds))
            except Exception:  # noqa: BLE001
                pass
        if self._memory_get(full) is not None:
            return False
        self._memory_set(full, value, ttl_seconds)
        return True

    def get_json(self, key: str) -> dict[str, Any] | None:
        raw = self.get(key)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def set_json(self, key: str, value: dict[str, Any], *, ttl_seconds: int | None = None) -> None:
        self.set(key, json.dumps(value, ensure_ascii=False), ttl_seconds=ttl_seconds)

    def stats(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "hits": self.hits,
            "misses": self.misses,
            "fallback_reason": self.fallback_reason,
            "memory_entries": len(self._memory),
        }

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def _memory_get(self, full: str) -> str | None:
        entry = self._memory.get(full)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at and expires_at < time.time():
            self._memory.pop(full, None)
            return None
        return value

    def _memory_set(self, full: str, value: str, ttl_seconds: int | None) -> None:
        if len(self._memory) > 4096:
            now = time.time()
            self._memory = {k: v for k, v in self._memory.items() if not v[1] or v[1] >= now}
        self._memory[full] = (value, time.time() + ttl_seconds if ttl_seconds else 0.0)


def create_cache(cwd: str | Path | None = None) -> WorkbenchCache:
    config = QueueConfig.load(cwd)
    if config.backend != "redis":
        return WorkbenchCache()
    try:
        import redis

        client = redis.Redis.from_url(
            config.redis_url,
            decode_responses=True,
            socket_connect_timeout=config.connect_timeout,
            socket_timeout=config.socket_timeout,
        )
        client.ping()
        return WorkbenchCache(client=client, prefix=f"{config.stream_prefix}:cache")
    except Exception as exc:  # noqa: BLE001
        return WorkbenchCache(fallback_reason=f"redis unavailable: {exc}")


def content_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()
