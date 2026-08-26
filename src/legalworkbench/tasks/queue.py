"""A lightweight file-backed review task queue."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from legalworkbench.paths import workspace_dir
from legalworkbench.secure_storage import secure_read_text, secure_write_text


_QUEUE_LOCKS: dict[str, threading.RLock] = {}
_QUEUE_LOCKS_GUARD = threading.Lock()


def _queue_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _QUEUE_LOCKS_GUARD:
        return _QUEUE_LOCKS.setdefault(key, threading.RLock())


class ReviewTaskQueue:
    """Persist pending and completed legal review tasks for dashboard use."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.path = workspace_dir(self.cwd) / "tasks.json"
        self._lock = _queue_lock(self.path)
        self._bus: Any = None
        from legalworkbench.storage.postgres import postgres_backend

        self._postgres = postgres_backend(self.cwd)

    def add(
        self,
        *,
        title: str,
        source: str,
        contract_path: str = "",
        priority: int = 50,
        document_id: str = "",
        connect_mcp: bool = False,
        max_attempts: int = 2,
        publish: bool = True,
        dedup_key: str = "",
        auto_execute: bool = False,
        tenant_id: str = "local",
        user_id: str = "",
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            tasks = self.list()
            if dedup_key:
                existing = next(
                    (
                        item
                        for item in tasks
                        if item.get("dedup_key") == dedup_key
                        and str(item.get("tenant_id") or "local") == tenant_id
                        and item.get("status") in {"pending", "running"}
                    ),
                    None,
                )
                if existing is not None:
                    return {**existing, "deduplicated": True}
            task = {
                "task_id": f"task_{uuid4().hex[:10]}",
                "tenant_id": tenant_id,
                "user_id": user_id,
                "roles": list(roles or ["admin"]),
                "title": title,
                "source": source,
                "contract_path": contract_path,
                "document_id": document_id,
                "priority": priority,
                "status": "pending",
                "attempts": 0,
                "max_attempts": max(1, max_attempts),
                "connect_mcp": connect_mcp,
                "review_run_id": "",
                "report_path": "",
                "error": "",
                "dedup_key": dedup_key,
                "auto_execute": auto_execute,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            tasks.append(task)
            # 任务表先落盘再发布：tasks.json 兼任本地消息表（outbox）。发布失败时
            # 任务仍是 pending，由 worker 的 outbox 补偿扫描重投，入队按 task_id 幂等。
            self.save(tasks)
        if publish:
            task["queue"] = self.publish(task, dedup_key=dedup_key)
            self.update(str(task["task_id"]), queue=task["queue"])
        return task

    def publish(self, task: dict[str, Any], *, dedup_key: str = "") -> dict[str, Any]:
        try:
            if self._bus is None:
                from legalworkbench.mq import create_task_bus

                self._bus = create_task_bus(self.cwd)
            return self._bus.publish(task, dedup_key=dedup_key)
        except Exception as exc:  # noqa: BLE001 - 发布失败不阻塞任务创建
            return {"published": False, "error": str(exc)}

    def get(
        self, task_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        with self._lock:
            for task in self.list():
                if task.get("task_id") == task_id and (
                    tenant_id is None
                    or str(task.get("tenant_id") or "local") == tenant_id
                ):
                    return task
        return None

    def find_active(
        self, dedup_key: str, *, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        if not dedup_key:
            return None
        with self._lock:
            return next(
                (
                    task
                    for task in self.list()
                    if task.get("dedup_key") == dedup_key
                    and (
                        tenant_id is None
                        or str(task.get("tenant_id") or "local") == tenant_id
                    )
                    and task.get("status") in {"pending", "running"}
                ),
                None,
            )

    def claim(self, task_id: str) -> dict[str, Any] | None:
        """Atomically move one pending task to running."""

        with self._lock:
            tasks = self.list()
            for task in tasks:
                if task.get("task_id") != task_id:
                    continue
                if task.get("status") != "pending":
                    return None
                task["status"] = "running"
                task["attempts"] = int(task.get("attempts") or 0) + 1
                task["started_at"] = time.time()
                task["updated_at"] = time.time()
                task["error"] = ""
                self.save(tasks)
                return dict(task)
        return None

    def list(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if self._postgres is not None:
                return self._postgres.list_tasks(tenant_id=tenant_id)
            if not self.path.exists():
                return []
            try:
                raw = json.loads(secure_read_text(self.path, cwd=self.cwd))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return []
            rows = raw if isinstance(raw, list) else []
            if tenant_id is None:
                return rows
            return [
                row
                for row in rows
                if str(row.get("tenant_id") or "local") == tenant_id
            ]

    def summary(
        self, *, limit: int = 5, tenant_id: str | None = None
    ) -> dict[str, Any]:
        tasks = self.list(tenant_id=tenant_id)
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
        for task in tasks:
            status = str(task.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1

        def pick(statuses: set[str]) -> list[dict[str, Any]]:
            return [
                {
                    "task_id": task.get("task_id", ""),
                    "title": task.get("title", ""),
                    "status": task.get("status", ""),
                    "priority": task.get("priority", 0),
                    "review_run_id": task.get("review_run_id", ""),
                    "error": task.get("error", ""),
                    "updated_at": task.get("updated_at", 0),
                }
                for task in tasks
                if str(task.get("status") or "") in statuses
            ][:limit]

        return {
            "total": len(tasks),
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "remaining": counts.get("pending", 0) + counts.get("running", 0),
            "next_tasks": pick({"pending", "running"}),
            "failed_tasks": pick({"failed"}),
            "recent_completed": pick({"completed"}),
        }

    def update(self, task_id: str, **updates: Any) -> dict[str, Any] | None:
        with self._lock:
            tasks = self.list()
            updated: dict[str, Any] | None = None
            for task in tasks:
                if task.get("task_id") == task_id:
                    task.update(updates)
                    task["updated_at"] = time.time()
                    updated = task
                    break
            if updated is not None:
                self.save(tasks)
            return updated

    def delete(self, task_id: str, *, tenant_id: str | None = None) -> bool:
        tasks = self.list()
        remaining = [
            task
            for task in tasks
            if not (
                task.get("task_id") == task_id
                and (
                    tenant_id is None
                    or str(task.get("tenant_id") or "local") == tenant_id
                )
            )
        ]
        if len(remaining) == len(tasks):
            return False
        self.save(remaining)
        return True

    def delete_failed_without_contract(self, *, tenant_id: str | None = None) -> int:
        tasks = self.list()
        remaining = [
            task
            for task in tasks
            if not (
                task.get("status") == "failed"
                and (
                    tenant_id is None
                    or str(task.get("tenant_id") or "local") == tenant_id
                )
                and not str(task.get("contract_path") or "")
                and not str(task.get("review_run_id") or "")
            )
        ]
        deleted = len(tasks) - len(remaining)
        if deleted:
            self.save(remaining)
        return deleted

    def save(self, tasks: list[dict[str, Any]]) -> None:
        with self._lock:
            tasks.sort(
                key=lambda item: (
                    -int(item.get("priority", 0)),
                    -float(item.get("updated_at", 0)),
                )
            )
            if self._postgres is not None:
                self._postgres.replace_tasks(tasks)
                return
            secure_write_text(
                self.path,
                json.dumps(tasks, ensure_ascii=False, indent=2) + "\n",
                cwd=self.cwd,
                purpose="review-task-queue",
            )

    def next_pending(self) -> dict[str, Any] | None:
        for task in self.list():
            if task.get("status") == "pending":
                return task
        return None

    def recover_stale_running(
        self,
        *,
        max_age_seconds: float = 900.0,
        auto_only: bool = False,
    ) -> int:
        tasks = self.list()
        now = time.time()
        recovered = 0
        for task in tasks:
            if task.get("status") != "running":
                continue
            if auto_only and not task.get("auto_execute"):
                continue
            started_at = float(task.get("started_at") or 0)
            if started_at and now - started_at <= max_age_seconds:
                continue
            task["status"] = "pending"
            task["error"] = "recovered stale running task"
            task["updated_at"] = now
            recovered += 1
        if recovered:
            self.save(tasks)
        return recovered
