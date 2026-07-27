"""Redis Streams task bus with explicit delivery semantics.

设计取舍（面向单机可运行、语义完整、可平滑替换 Kafka 的场景）：

- at-least-once 投递：消息只有在业务处理成功后才 XACK；worker 在 ACK 前崩溃，
  消息会留在 consumer group 的 pending list 中，由其他消费者按 ``claim_idle_ms``
  通过 XPENDING + XCLAIM 认领重投（Redis >= 6.2 可用原子的 XAUTOCLAIM 等价替换）。
- 幂等消费：任务状态以文件任务表（task store）中 ``task_id`` 对应的记录为准，
  重复投递的已完成任务直接 ACK 跳过；入队按 ``task_id`` 做 SET NX 去重。
- 有限重试 + 死信队列：消费失败时带着自增的 attempts 重新入队，超过
  ``max_attempts`` 后写入 DLQ stream，等待人工检视后重投。
- 双优先级：Redis Streams 是流内 FIFO，没有原生优先级，因此用
  ``high`` / ``normal`` 两条 stream 建模两级优先级，消费时先取 high。
- 双写一致性：文件任务表先落盘、再向 Redis 发布（任务表兼任本地消息表 /
  outbox）；发布失败由 worker 空闲时的 outbox 补偿扫描重投，靠入队幂等去重。
- 降级：Redis 不可用时工厂函数回退到文件队列轮询，保持整条链路可用。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from legalworkbench.paths import settings_path
from legalworkbench.tasks.queue import ReviewTaskQueue


@dataclass(frozen=True)
class QueueConfig:
    backend: str = "local"
    redis_url: str = "redis://127.0.0.1:6379/0"
    stream_prefix: str = "lawbench"
    consumer_group: str = "review_workers"
    max_attempts: int = 3
    claim_idle_ms: int = 900_000
    dedup_ttl_seconds: int = 86_400
    maxlen: int = 10_000
    high_priority_threshold: int = 80
    connect_timeout: float = 2.0
    socket_timeout: float = 5.0

    @staticmethod
    def load(cwd: str | Path | None = None) -> "QueueConfig":
        raw: dict[str, Any] = {}
        path = settings_path(cwd)
        if path.exists():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    raw = parsed
            except json.JSONDecodeError:
                raw = {}
        queue = raw.get("queue") if isinstance(raw.get("queue"), dict) else {}
        redis_section = raw.get("redis") if isinstance(raw.get("redis"), dict) else {}
        return QueueConfig(
            backend=os.environ.get("LEGAL_WORKBENCH_QUEUE_BACKEND", str(queue.get("backend") or "local")),
            redis_url=os.environ.get("LEGAL_WORKBENCH_REDIS_URL", str(redis_section.get("url") or "redis://127.0.0.1:6379/0")),
            stream_prefix=str(queue.get("stream_prefix") or "lawbench"),
            consumer_group=str(queue.get("consumer_group") or "review_workers"),
            max_attempts=int(queue.get("max_attempts") or 3),
            claim_idle_ms=int(queue.get("claim_idle_ms") or 900_000),
            dedup_ttl_seconds=int(queue.get("dedup_ttl_seconds") or 86_400),
            maxlen=int(queue.get("maxlen") or 10_000),
            high_priority_threshold=int(queue.get("high_priority_threshold") or 80),
            connect_timeout=float(redis_section.get("connect_timeout") or 2.0),
            socket_timeout=float(redis_section.get("socket_timeout") or 5.0),
        )


@dataclass
class QueueMessage:
    stream: str
    entry_id: str
    task_id: str
    attempts: int
    payload: dict[str, Any] = field(default_factory=dict)
    redelivered: bool = False


class RedisTaskBus:
    """Deliver review tasks through Redis Streams consumer groups."""

    backend = "redis"
    fallback_reason = ""

    def __init__(self, cwd: str | Path | None = None, *, config: QueueConfig | None = None, client: Any = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.config = config or QueueConfig.load(self.cwd)
        if client is None:
            import redis

            client = redis.Redis.from_url(
                self.config.redis_url,
                decode_responses=True,
                socket_connect_timeout=self.config.connect_timeout,
                socket_timeout=self.config.socket_timeout,
            )
        self.client = client

    @property
    def stream_high(self) -> str:
        return f"{self.config.stream_prefix}:tasks:high"

    @property
    def stream_normal(self) -> str:
        return f"{self.config.stream_prefix}:tasks:normal"

    @property
    def stream_dlq(self) -> str:
        return f"{self.config.stream_prefix}:tasks:dlq"

    @property
    def streams(self) -> list[str]:
        return [self.stream_high, self.stream_normal]

    def ensure_groups(self) -> None:
        for stream in self.streams:
            try:
                self.client.xgroup_create(stream, self.config.consumer_group, id="0", mkstream=True)
            except Exception as exc:  # noqa: BLE001 - BUSYGROUP means the group already exists
                if "BUSYGROUP" not in str(exc):
                    raise

    def publish(self, task: dict[str, Any], *, dedup_key: str = "") -> dict[str, Any]:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            return {"backend": self.backend, "published": False, "error": "task_id required"}
        # 入队幂等：task_id 级 SET NX 保证 outbox 补偿重投不会产生重复消息；
        # 业务级 dedup_key（如飞书 message_id）在其之上再挡一层。
        claimed_keys: list[str] = []
        for key in filter(None, [dedup_key, f"task:{task_id}"]):
            full_key = f"{self.config.stream_prefix}:dedup:{key}"
            claimed = self.client.set(full_key, task_id, nx=True, ex=self.config.dedup_ttl_seconds)
            if not claimed:
                return {"backend": self.backend, "published": False, "deduplicated": True, "dedup_key": key}
            claimed_keys.append(full_key)
        stream = self.stream_high if int(task.get("priority") or 0) >= self.config.high_priority_threshold else self.stream_normal
        try:
            entry_id = self.client.xadd(
                stream,
                {
                    "task_id": task_id,
                    "attempts": "1",
                    "payload": json.dumps(task, ensure_ascii=False),
                    "enqueued_at": f"{time.time():.3f}",
                },
                maxlen=self.config.maxlen,
                approximate=True,
            )
        except Exception:
            # 释放刚占用的去重键，否则 XADD 失败后 outbox 补偿会被去重挡住，
            # 消息要等 dedup TTL 过期才能重投
            for full_key in claimed_keys:
                try:
                    self.client.delete(full_key)
                except Exception:  # noqa: BLE001 - 连接已断时留给 TTL 兜底
                    pass
            raise
        return {"backend": self.backend, "published": True, "stream": stream, "entry_id": str(entry_id)}

    def consume(self, *, consumer: str, block_ms: int = 1000) -> QueueMessage | None:
        message = self._claim_stale(consumer)
        if message is not None:
            return message
        response = self.client.xreadgroup(
            self.config.consumer_group,
            consumer,
            {self.stream_high: ">", self.stream_normal: ">"},
            count=1,
            block=block_ms,
        )
        if not response:
            return None
        by_stream = {str(stream): entries for stream, entries in response if entries}
        for stream in self.streams:
            entries = by_stream.get(stream)
            if entries:
                entry_id, fields = entries[0]
                return self._build_message(stream, entry_id, fields)
        return None

    def ack(self, message: QueueMessage) -> None:
        self.client.xack(message.stream, self.config.consumer_group, message.entry_id)

    def fail(self, message: QueueMessage, *, error: str, max_attempts: int | None = None) -> dict[str, Any]:
        limit = max(1, max_attempts if max_attempts is not None else self.config.max_attempts)
        if message.attempts >= limit:
            self.client.xadd(
                self.stream_dlq,
                {
                    "task_id": message.task_id,
                    "payload": json.dumps(message.payload, ensure_ascii=False),
                    "error": error[:500],
                    "failed_attempts": str(message.attempts),
                    "origin_stream": message.stream,
                    "dead_lettered_at": f"{time.time():.3f}",
                },
                maxlen=self.config.maxlen,
                approximate=True,
            )
            self.ack(message)
            return {"action": "dead_lettered", "attempts": message.attempts, "dlq": self.stream_dlq}
        self.client.xadd(
            message.stream,
            {
                "task_id": message.task_id,
                "attempts": str(message.attempts + 1),
                "payload": json.dumps(message.payload, ensure_ascii=False),
                "enqueued_at": f"{time.time():.3f}",
            },
            maxlen=self.config.maxlen,
            approximate=True,
        )
        self.ack(message)
        return {"action": "requeued", "next_attempt": message.attempts + 1}

    def health(self) -> dict[str, Any]:
        try:
            self.client.ping()
        except Exception as exc:  # noqa: BLE001
            return {"backend": self.backend, "ok": False, "error": str(exc)}
        streams: dict[str, Any] = {}
        for stream in self.streams:
            summary = self.client.xpending(stream, self.config.consumer_group)
            pending = summary.get("pending", 0) if isinstance(summary, dict) else 0
            streams[stream] = {"length": self.client.xlen(stream), "pending": int(pending or 0)}
        return {
            "backend": self.backend,
            "ok": True,
            "consumer_group": self.config.consumer_group,
            "streams": streams,
            "dlq": {"stream": self.stream_dlq, "length": self.client.xlen(self.stream_dlq)},
            "max_attempts": self.config.max_attempts,
            "claim_idle_ms": self.config.claim_idle_ms,
        }

    def dlq_list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        entries = self.client.xrange(self.stream_dlq, count=limit)
        return [
            {
                "entry_id": str(entry_id),
                "task_id": fields.get("task_id", ""),
                "error": fields.get("error", ""),
                "failed_attempts": fields.get("failed_attempts", ""),
                "origin_stream": fields.get("origin_stream", ""),
            }
            for entry_id, fields in entries
        ]

    def dlq_requeue_all(self) -> int:
        requeued = 0
        for entry_id, fields in self.client.xrange(self.stream_dlq):
            payload = _parse_payload(fields.get("payload"))
            task_id = str(fields.get("task_id") or payload.get("task_id") or "")
            if not task_id:
                self.client.xdel(self.stream_dlq, entry_id)
                continue
            stream = str(fields.get("origin_stream") or self.stream_normal)
            self.client.xadd(
                stream,
                {
                    "task_id": task_id,
                    "attempts": "1",
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "enqueued_at": f"{time.time():.3f}",
                },
                maxlen=self.config.maxlen,
                approximate=True,
            )
            self.client.xdel(self.stream_dlq, entry_id)
            requeued += 1
        return requeued

    def _claim_stale(self, consumer: str) -> QueueMessage | None:
        # 认领超时未 ACK 的消息（worker 崩溃恢复）；attempts 取消息自带计数与
        # PEL 投递计数的较大值，防止“反复崩溃却永不进 DLQ”的毒消息循环。
        for stream in self.streams:
            try:
                pending = self.client.xpending_range(
                    stream, self.config.consumer_group, min="-", max="+", count=10
                )
            except Exception:  # noqa: BLE001 - group 尚未创建等情况
                continue
            for item in pending:
                idle = int(item.get("time_since_delivered") or 0)
                if idle < self.config.claim_idle_ms:
                    continue
                entry_id = item.get("message_id")
                claimed = self.client.xclaim(
                    stream,
                    self.config.consumer_group,
                    consumer,
                    min_idle_time=self.config.claim_idle_ms,
                    message_ids=[entry_id],
                )
                if not claimed:
                    continue
                claimed_id, fields = claimed[0]
                message = self._build_message(stream, claimed_id, fields, redelivered=True)
                message.attempts = max(message.attempts, int(item.get("times_delivered") or 1))
                return message
        return None

    def _build_message(self, stream: str, entry_id: Any, fields: dict[str, Any], *, redelivered: bool = False) -> QueueMessage:
        return QueueMessage(
            stream=stream,
            entry_id=str(entry_id),
            task_id=str(fields.get("task_id") or ""),
            attempts=int(fields.get("attempts") or 1),
            payload=_parse_payload(fields.get("payload")),
            redelivered=redelivered,
        )


class LocalTaskBus:
    """File-queue polling fallback that keeps the worker interface identical."""

    backend = "local"

    def __init__(self, cwd: str | Path | None = None, *, fallback_reason: str = "") -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.queue = ReviewTaskQueue(self.cwd)
        self.fallback_reason = fallback_reason

    def publish(self, task: dict[str, Any], *, dedup_key: str = "") -> dict[str, Any]:
        del task, dedup_key
        return {"backend": self.backend, "published": False, "reason": "local backend delivers via file queue polling"}

    def consume(self, *, consumer: str, block_ms: int = 1000) -> QueueMessage | None:
        del consumer, block_ms
        self.queue.recover_stale_running()
        task = self.queue.next_pending()
        if task is None:
            return None
        attempts = int(task.get("attempts") or 0)
        return QueueMessage(
            stream="local",
            entry_id=str(task.get("task_id") or ""),
            task_id=str(task.get("task_id") or ""),
            attempts=attempts + 1,
            payload=task,
            redelivered=attempts > 0,
        )

    def ack(self, message: QueueMessage) -> None:
        del message

    def fail(self, message: QueueMessage, *, error: str, max_attempts: int | None = None) -> dict[str, Any]:
        del error
        limit = max(1, max_attempts if max_attempts is not None else int(message.payload.get("max_attempts") or 1))
        if message.attempts >= limit:
            return {"action": "dead_lettered", "attempts": message.attempts, "dlq": "file:tasks.json(status=failed)"}
        return {"action": "requeued", "next_attempt": message.attempts + 1}

    def health(self) -> dict[str, Any]:
        summary = self.queue.summary()
        return {
            "backend": self.backend,
            "ok": True,
            "fallback_reason": self.fallback_reason,
            "pending": summary["pending"],
            "running": summary["running"],
            "failed": summary["failed"],
            "completed": summary["completed"],
        }

    def dlq_list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {"entry_id": task.get("task_id", ""), "task_id": task.get("task_id", ""), "error": task.get("error", "")}
            for task in self.queue.list()
            if task.get("status") == "failed"
        ][:limit]

    def dlq_requeue_all(self) -> int:
        requeued = 0
        for task in self.queue.list():
            if task.get("status") == "failed":
                self.queue.update(str(task.get("task_id")), status="pending", attempts=0, error="")
                requeued += 1
        return requeued


def create_task_bus(cwd: str | Path | None = None) -> RedisTaskBus | LocalTaskBus:
    config = QueueConfig.load(cwd)
    if config.backend != "redis":
        return LocalTaskBus(cwd)
    try:
        bus = RedisTaskBus(cwd, config=config)
        bus.client.ping()
        bus.ensure_groups()
        return bus
    except Exception as exc:  # noqa: BLE001 - ImportError / ConnectionError / TimeoutError
        return LocalTaskBus(cwd, fallback_reason=f"redis unavailable: {exc}")


def _parse_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
