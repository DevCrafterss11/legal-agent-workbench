"""Background worker consuming review tasks from the task bus.

消费侧语义：

- 幂等：任务状态以 tasks.json 为准；at-least-once 重投的已完成任务直接 ACK 跳过。
- ACK 时机：业务处理成功后才 ACK；处理中崩溃的消息留在 pending list，
  由其他消费者超时认领。
- 失败路径：交给 bus.fail() 判定重投或死信，并同步回写任务状态。
- outbox 补偿：空闲时扫描"已落盘但未成功发布"的 pending 任务并重新发布，
  修复任务表与消息流双写不一致。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from legalworkbench.tasks.queue import ReviewTaskQueue


class ReviewTaskWorker:
    """Execute review tasks delivered by the task bus with retry-safe updates."""

    def __init__(self, cwd: str | Path | None = None, *, bus: Any = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.queue = ReviewTaskQueue(self.cwd)
        if bus is None:
            from legalworkbench.mq import create_task_bus

            bus = create_task_bus(self.cwd)
        self.bus = bus
        self.consumer_name = f"worker-{uuid4().hex[:8]}"
        self._last_outbox_sweep = 0.0
        self._runtime: Any = None

    def run_once(self, *, connect_mcp: bool = False, block_ms: int = 1000) -> dict[str, Any] | None:
        message = self.bus.consume(consumer=self.consumer_name, block_ms=block_ms)
        if message is None:
            self._sweep_outbox()
            return None
        task = self.queue.get(message.task_id)
        if task is None:
            # 消息指向的任务元数据不存在（例如任务已删除）：ACK 丢弃，避免毒消息循环
            self.bus.ack(message)
            return {"task_id": message.task_id, "status": "orphaned"}
        task_id = str(task["task_id"])
        if task.get("status") == "completed":
            # at-least-once 重投的幂等保护
            self.bus.ack(message)
            return task
        self.queue.update(task_id, status="running", attempts=message.attempts, error="", started_at=time.time())
        try:
            contract_path = str(task.get("contract_path") or "")
            if not contract_path:
                raise ValueError("contract_path required: task must be created from an uploaded or stored contract")
            path = Path(contract_path).expanduser()
            if not path.is_absolute():
                path = self.cwd / path
            if not path.exists():
                raise FileNotFoundError(str(path))
            use_mcp = bool(task.get("connect_mcp")) or connect_mcp
            run = self._get_runtime().review(
                path,
                connect_mcp=use_mcp,
                tenant_id=str(task.get("tenant_id") or "local"),
                user_id=str(task.get("user_id") or ""),
                roles=[str(role) for role in (task.get("roles") or ["admin"])],
            )
            if run.status not in {"completed", "blocked"}:
                raise RuntimeError(run.error or f"review run ended with status={run.status}")
            updated = self.queue.update(
                task_id,
                status="completed",
                review_run_id=run.review_run_id,
                report_path=run.report_path,
                completed_at=time.time(),
            )
            self.bus.ack(message)
            return updated
        except Exception as exc:
            outcome = self.bus.fail(message, error=str(exc), max_attempts=int(task.get("max_attempts") or 1))
            status = "pending" if outcome.get("action") == "requeued" else "failed"
            return self.queue.update(
                task_id,
                status=status,
                error=str(exc),
                completed_at=time.time(),
                dead_lettered=outcome.get("action") == "dead_lettered",
            )

    def _get_runtime(self):
        """Keep one runtime per worker process so its caches and clients are reusable."""

        if self._runtime is None:
            # 惰性导入：runtime -> cache -> mq -> tasks 存在环，运行期再解析
            from legalworkbench.runtime import LegalAgentRuntime

            self._runtime = LegalAgentRuntime(self.cwd)
        return self._runtime

    def run_loop(self, *, interval_seconds: float = 2.0, connect_mcp: bool = False) -> None:
        while True:
            self.run_once(connect_mcp=connect_mcp)
            time.sleep(interval_seconds)

    def _sweep_outbox(self, *, min_age_seconds: float = 30.0) -> int:
        if self.bus.backend != "redis":
            return 0
        now = time.time()
        if now - self._last_outbox_sweep < min_age_seconds:
            return 0
        self._last_outbox_sweep = now
        republished = 0
        for task in self.queue.list():
            if task.get("status") != "pending":
                continue
            queue_info = task.get("queue") if isinstance(task.get("queue"), dict) else {}
            if queue_info.get("published") or queue_info.get("deduplicated"):
                continue
            if now - float(task.get("created_at") or 0) < min_age_seconds:
                continue
            result = self.bus.publish(task, dedup_key="")
            self.queue.update(str(task.get("task_id")), queue=result)
            if result.get("published"):
                republished += 1
        return republished
