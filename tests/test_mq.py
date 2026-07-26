"""Task bus / cache tests: delivery semantics that must survive interview grilling."""

from __future__ import annotations

import json
import time
from pathlib import Path

import fakeredis

from legalworkbench.cache import WorkbenchCache, content_hash
from legalworkbench.llm.client import LlmClient, LlmConfig, LlmResponse
from legalworkbench.mq import LocalTaskBus, QueueConfig, RedisTaskBus, create_task_bus
from legalworkbench.runtime import LegalAgentRuntime
from legalworkbench.tasks import ReviewTaskQueue, ReviewTaskWorker


def make_bus(tmp_path: Path, **overrides) -> RedisTaskBus:
    config = QueueConfig(backend="redis", **overrides)
    bus = RedisTaskBus(tmp_path, config=config, client=fakeredis.FakeRedis(decode_responses=True))
    bus.ensure_groups()
    return bus


def make_task(task_id: str = "task_abc123", priority: int = 50) -> dict:
    return {"task_id": task_id, "title": "t", "priority": priority, "max_attempts": 2}


def test_publish_consume_ack_roundtrip(tmp_path: Path) -> None:
    bus = make_bus(tmp_path)
    result = bus.publish(make_task())
    assert result["published"] is True
    assert result["stream"] == bus.stream_normal

    message = bus.consume(consumer="w1", block_ms=1)
    assert message is not None
    assert message.task_id == "task_abc123"
    assert message.attempts == 1
    assert message.payload["title"] == "t"

    bus.ack(message)
    assert bus.consume(consumer="w1", block_ms=1) is None
    health = bus.health()
    assert health["ok"] is True
    assert health["streams"][bus.stream_normal]["pending"] == 0


def test_high_priority_stream_drained_first(tmp_path: Path) -> None:
    bus = make_bus(tmp_path)
    bus.publish(make_task("task_normal", priority=50))
    bus.publish(make_task("task_urgent", priority=90))
    first = bus.consume(consumer="w1", block_ms=1)
    assert first is not None
    assert first.task_id == "task_urgent"
    assert first.stream == bus.stream_high


def test_enqueue_dedup_by_task_and_business_key(tmp_path: Path) -> None:
    bus = make_bus(tmp_path)
    assert bus.publish(make_task(), dedup_key="feishu:msg_1")["published"] is True
    # 同一 task_id 重发（outbox 补偿场景）被幂等挡住
    dup = bus.publish(make_task())
    assert dup["deduplicated"] is True
    # 同一业务键、不同 task_id（飞书重复回调场景）也被挡住
    dup2 = bus.publish(make_task("task_other"), dedup_key="feishu:msg_1")
    assert dup2["deduplicated"] is True


def test_failed_delivery_requeues_then_dead_letters(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, max_attempts=2)
    bus.publish(make_task())

    first = bus.consume(consumer="w1", block_ms=1)
    outcome = bus.fail(first, error="boom", max_attempts=2)
    assert outcome["action"] == "requeued"
    assert outcome["next_attempt"] == 2

    second = bus.consume(consumer="w1", block_ms=1)
    assert second.attempts == 2
    outcome = bus.fail(second, error="boom again", max_attempts=2)
    assert outcome["action"] == "dead_lettered"

    assert bus.consume(consumer="w1", block_ms=1) is None
    entries = bus.dlq_list()
    assert len(entries) == 1
    assert entries[0]["task_id"] == "task_abc123"
    assert entries[0]["error"] == "boom again"

    assert bus.dlq_requeue_all() == 1
    assert bus.dlq_list() == []
    requeued = bus.consume(consumer="w1", block_ms=1)
    assert requeued is not None
    assert requeued.task_id == "task_abc123"
    assert requeued.attempts == 1


def test_stale_pending_message_claimed_by_another_consumer(tmp_path: Path) -> None:
    # worker A 消费后未 ACK 即"崩溃"：claim_idle_ms=0 时 worker B 立即认领
    bus = make_bus(tmp_path, claim_idle_ms=0)
    bus.publish(make_task())
    taken = bus.consume(consumer="worker-a", block_ms=1)
    assert taken is not None

    reclaimed = bus.consume(consumer="worker-b", block_ms=1)
    assert reclaimed is not None
    assert reclaimed.task_id == taken.task_id
    assert reclaimed.redelivered is True
    bus.ack(reclaimed)
    assert bus.consume(consumer="worker-b", block_ms=1) is None


def test_publish_rolls_back_dedup_key_when_xadd_fails(tmp_path: Path) -> None:
    bus = make_bus(tmp_path)
    original_xadd = bus.client.xadd
    calls = {"n": 0}

    def flaky_xadd(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("connection dropped after dedup claim")
        return original_xadd(*args, **kwargs)

    bus.client.xadd = flaky_xadd
    try:
        bus.publish(make_task())
        raise AssertionError("publish should propagate the xadd failure")
    except ConnectionError:
        pass
    # 去重键已回滚，重试发布不会被自己挡住
    retry = bus.publish(make_task())
    assert retry["published"] is True


def test_create_task_bus_falls_back_to_local_when_redis_down(tmp_path: Path) -> None:
    workspace = tmp_path / ".lawbench"
    workspace.mkdir(parents=True)
    (workspace / "settings.json").write_text(
        json.dumps(
            {
                "queue": {"backend": "redis"},
                "redis": {"url": "redis://127.0.0.1:1/0", "connect_timeout": 0.2},
            }
        ),
        encoding="utf-8",
    )
    bus = create_task_bus(tmp_path)
    assert bus.backend == "local"
    assert "redis unavailable" in bus.fallback_reason
    assert bus.health()["ok"] is True


def test_worker_consumes_from_redis_stream_end_to_end(tmp_path: Path) -> None:
    runtime = LegalAgentRuntime(tmp_path)
    paths = runtime.init_samples()
    queue = ReviewTaskQueue(tmp_path)
    task = queue.add(title="redis review", source="test", contract_path=str(paths["contract"]), publish=False)

    bus = make_bus(tmp_path)
    bus.publish(task)
    worker = ReviewTaskWorker(tmp_path, bus=bus)
    result = worker.run_once(block_ms=1)
    assert result is not None
    assert result["status"] == "completed"
    assert result["review_run_id"]
    assert bus.health()["streams"][bus.stream_normal]["pending"] == 0
    assert worker.run_once(block_ms=1) is None


def test_worker_skips_redelivered_completed_task(tmp_path: Path) -> None:
    runtime = LegalAgentRuntime(tmp_path)
    paths = runtime.init_samples()
    queue = ReviewTaskQueue(tmp_path)
    task = queue.add(title="dup delivery", source="test", contract_path=str(paths["contract"]), publish=False)
    queue.update(str(task["task_id"]), status="completed", review_run_id="law_done")

    bus = make_bus(tmp_path)
    bus.publish(task)
    worker = ReviewTaskWorker(tmp_path, bus=bus)
    result = worker.run_once(block_ms=1)
    assert result["status"] == "completed"
    assert result["review_run_id"] == "law_done"
    # 幂等跳过：消息被 ACK，任务没有被重跑
    assert bus.health()["streams"][bus.stream_normal]["pending"] == 0


def test_worker_orphaned_message_is_acked(tmp_path: Path) -> None:
    bus = make_bus(tmp_path)
    bus.publish(make_task("task_ghost"))
    worker = ReviewTaskWorker(tmp_path, bus=bus)
    result = worker.run_once(block_ms=1)
    assert result == {"task_id": "task_ghost", "status": "orphaned"}
    assert bus.health()["streams"][bus.stream_normal]["pending"] == 0


def test_worker_failure_path_updates_task_store(tmp_path: Path) -> None:
    queue = ReviewTaskQueue(tmp_path)
    # contract_path 缺失 -> 处理必然失败；max_attempts=2 -> 先重投再死信
    task = queue.add(title="broken", source="test", max_attempts=2, publish=False)
    bus = make_bus(tmp_path)
    bus.publish(task)
    worker = ReviewTaskWorker(tmp_path, bus=bus)

    first = worker.run_once(block_ms=1)
    assert first["status"] == "pending"

    second = worker.run_once(block_ms=1)
    assert second["status"] == "failed"
    assert second["dead_lettered"] is True
    assert bus.dlq_list()[0]["task_id"] == task["task_id"]


def test_local_bus_preserves_file_queue_behavior(tmp_path: Path) -> None:
    queue = ReviewTaskQueue(tmp_path)
    task = queue.add(title="local", source="test", max_attempts=2, publish=False)
    bus = LocalTaskBus(tmp_path)
    worker = ReviewTaskWorker(tmp_path, bus=bus)

    first = worker.run_once(block_ms=1)
    assert first["status"] == "pending"
    second = worker.run_once(block_ms=1)
    assert second["status"] == "failed"
    assert queue.get(str(task["task_id"]))["status"] == "failed"


def test_outbox_sweep_republishes_unpublished_tasks(tmp_path: Path) -> None:
    queue = ReviewTaskQueue(tmp_path)
    task = queue.add(title="stuck", source="test", publish=False)
    queue.update(str(task["task_id"]), queue={"published": False, "error": "redis down"}, created_at=time.time() - 120)

    bus = make_bus(tmp_path)
    worker = ReviewTaskWorker(tmp_path, bus=bus)
    assert worker._sweep_outbox(min_age_seconds=30) == 1
    message = bus.consume(consumer="w1", block_ms=1)
    assert message is not None
    assert message.task_id == task["task_id"]
    # 补偿重投是幂等的：再扫一遍不会重复发布
    worker._last_outbox_sweep = 0.0
    assert worker._sweep_outbox(min_age_seconds=30) == 0


def test_cache_roundtrip_and_set_if_absent() -> None:
    for cache in (WorkbenchCache(), WorkbenchCache(client=fakeredis.FakeRedis(decode_responses=True))):
        assert cache.get("k") is None
        cache.set("k", "v", ttl_seconds=60)
        assert cache.get("k") == "v"
        assert cache.set_if_absent("claim", "a", ttl_seconds=60) is True
        assert cache.set_if_absent("claim", "b", ttl_seconds=60) is False
        cache.set_json("j", {"x": 1})
        assert cache.get_json("j") == {"x": 1}
        stats = cache.stats()
        assert stats["backend"] in {"memory", "redis"}
        assert stats["hits"] >= 2


def test_llm_client_remote_calls_hit_cache() -> None:
    cache = WorkbenchCache()
    client = LlmClient(
        LlmConfig(provider="openai_compatible", model="m", base_url="http://fake", api_key="k"),
        cache=cache,
    )
    calls = {"n": 0}

    def fake_remote(*, system: str, user: str) -> LlmResponse:
        calls["n"] += 1
        return LlmResponse(text='{"score": 0.9}', model="m", prompt_tokens=10, completion_tokens=5)

    client._openai_compatible = fake_remote
    first = client.complete(system="s", user="u")
    second = client.complete(system="s", user="u")
    assert calls["n"] == 1
    assert second.text == first.text
    assert second.raw == {"cached": True}
    # 不同 prompt 不会命中同一个 key
    client.complete(system="s", user="other")
    assert calls["n"] == 2


def test_content_hash_is_stable_and_separator_safe() -> None:
    assert content_hash("a", "b") == content_hash("a", "b")
    assert content_hash("a", "b") != content_hash("ab", "")
