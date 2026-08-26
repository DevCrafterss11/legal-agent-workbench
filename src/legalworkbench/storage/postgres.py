"""Optional PostgreSQL persistence backend.

The local encrypted files remain the default. When ``storage.backend`` is set
to ``postgres`` (or the corresponding environment variables are configured),
this adapter stores the durable Run/Task/Memory/Session/Event envelopes as
JSONB with tenant indexes. The adapter is intentionally lazy-imported so the
base install does not require a database driver.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, cast

from legalworkbench.models import LegalMemory, ReviewRun
from legalworkbench.paths import settings_path
from legalworkbench.privacy import mask_value


class PostgresPersistence:
    """Small transaction-per-operation PostgreSQL repository.

    A connection pool can be layered underneath this boundary in deployment;
    keeping operations transactional here prevents partial JSON envelope writes
    and gives the file-backed stores a clean migration seam.
    """

    def __init__(self, dsn: str, *, connect_timeout: float = 5.0) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        try:
            import psycopg  # type: ignore
            from psycopg.types.json import Jsonb  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "PostgreSQL backend requires psycopg; install legal-agent-workbench[postgres]"
            ) from exc
        self._psycopg = psycopg
        self._Jsonb = Jsonb
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.ensure_schema()

    @classmethod
    def from_settings(cls, cwd: str | Path | None = None) -> "PostgresPersistence | None":
        root = Path(cwd or Path.cwd()).resolve()
        settings: dict[str, Any] = {}
        path = settings_path(root)
        if path.exists():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    settings = parsed
            except (OSError, json.JSONDecodeError):
                pass
        section = cast(dict[str, Any], settings.get("storage")) if isinstance(settings.get("storage"), dict) else {}
        backend = os.environ.get(
            "LEGAL_WORKBENCH_STORAGE_BACKEND", str(section.get("backend") or "local")
        ).lower()
        if backend != "postgres":
            return None
        dsn = os.environ.get("LEGAL_WORKBENCH_POSTGRES_DSN") or str(section.get("dsn") or "")
        timeout = float(section.get("connect_timeout") or 5.0)
        return cls(dsn, connect_timeout=timeout)

    def _connect(self):
        return self._psycopg.connect(self.dsn, connect_timeout=self.connect_timeout)

    def _jsonb(self, value: Any) -> Any:
        return self._Jsonb(value)

    def ensure_schema(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS lawbench_runs (
            review_run_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS lawbench_runs_tenant_updated
            ON lawbench_runs (tenant_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS lawbench_memory (
            memory_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS lawbench_memory_tenant ON lawbench_memory (tenant_id);
        CREATE TABLE IF NOT EXISTS lawbench_tasks (
            task_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS lawbench_tasks_tenant_updated
            ON lawbench_tasks (tenant_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS lawbench_sessions (
            session_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS lawbench_sessions_tenant_created
            ON lawbench_sessions (tenant_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS lawbench_events (
            event_id BIGSERIAL PRIMARY KEY,
            review_run_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS lawbench_events_tenant_id
            ON lawbench_events (tenant_id, event_id);
        """
        with self._connect() as conn:
            conn.execute(ddl)
            conn.commit()

    def save_run(self, run: ReviewRun) -> None:
        payload = mask_value(run.model_dump(mode="json"))
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO lawbench_runs(review_run_id, tenant_id, updated_at, payload)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (review_run_id) DO UPDATE SET
                     tenant_id=EXCLUDED.tenant_id, updated_at=EXCLUDED.updated_at,
                     payload=EXCLUDED.payload""",
                (run.review_run_id, run.tenant_id, run.updated_at, self._jsonb(payload)),
            )
            conn.commit()

    def load_run(self, run_id: str, *, tenant_id: str | None = None) -> ReviewRun | None:
        query = "SELECT payload FROM lawbench_runs WHERE review_run_id = %s"
        params: list[Any] = [run_id]
        if tenant_id is not None:
            query += " AND tenant_id = %s"
            params.append(tenant_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return ReviewRun.model_validate(row[0]) if row else None

    def list_runs(self, limit: int = 20, *, tenant_id: str | None = None) -> list[ReviewRun]:
        query = "SELECT payload FROM lawbench_runs"
        params: list[Any] = []
        if tenant_id is not None:
            query += " WHERE tenant_id = %s"
            params.append(tenant_id)
        query += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [ReviewRun.model_validate(row[0]) for row in rows]

    def load_memory(self, *, tenant_id: str | None = None) -> list[LegalMemory]:
        query = "SELECT payload FROM lawbench_memory"
        params: list[Any] = []
        if tenant_id is not None:
            query += " WHERE tenant_id = %s"
            params.append(tenant_id)
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [LegalMemory.model_validate(row[0]) for row in rows]

    def save_memory(self, memories: list[LegalMemory]) -> None:
        # Reconcile only tenants represented by this snapshot.  A global
        # DELETE would allow one tenant's writer to erase another tenant's
        # memories during concurrent updates.
        tenant_ids = {memory.tenant_id for memory in memories}
        with self._connect() as conn:
            for memory in memories:
                payload = mask_value(memory.model_dump(mode="json"))
                conn.execute(
                    """INSERT INTO lawbench_memory(memory_id, tenant_id, updated_at, payload)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (memory_id) DO UPDATE SET
                         tenant_id=EXCLUDED.tenant_id, updated_at=EXCLUDED.updated_at,
                         payload=EXCLUDED.payload""",
                    (memory.memory_id, memory.tenant_id, time.time(), self._jsonb(payload)),
                )
            if tenant_ids:
                ids = [memory.memory_id for memory in memories]
                conn.execute(
                    "DELETE FROM lawbench_memory WHERE tenant_id = ANY(%s) AND NOT (memory_id = ANY(%s))",
                    (list(tenant_ids), ids),
                )
            conn.commit()

    def list_tasks(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload FROM lawbench_tasks"
        params: list[Any] = []
        if tenant_id is not None:
            query += " WHERE tenant_id = %s"
            params.append(tenant_id)
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row[0]) for row in rows]

    def replace_tasks(self, tasks: list[dict[str, Any]]) -> None:
        # Keep reconciliation tenant-scoped so a queue snapshot cannot remove
        # tasks belonging to another tenant.
        tenant_ids = {str(task.get("tenant_id") or "local") for task in tasks}
        with self._connect() as conn:
            for task in tasks:
                conn.execute(
                    """INSERT INTO lawbench_tasks(task_id, tenant_id, updated_at, payload)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (task_id) DO UPDATE SET
                         tenant_id=EXCLUDED.tenant_id, updated_at=EXCLUDED.updated_at,
                         payload=EXCLUDED.payload""",
                    (
                        str(task.get("task_id") or ""),
                        str(task.get("tenant_id") or "local"),
                        float(task.get("updated_at") or time.time()),
                        self._jsonb(task),
                    ),
                )
            if tenant_ids:
                ids = [str(task.get("task_id") or "") for task in tasks]
                conn.execute(
                    "DELETE FROM lawbench_tasks WHERE tenant_id = ANY(%s) AND NOT (task_id = ANY(%s))",
                    (list(tenant_ids), ids),
                )
            conn.commit()

    def save_session(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO lawbench_sessions(session_id, tenant_id, created_at, payload)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (session_id) DO UPDATE SET
                     tenant_id=EXCLUDED.tenant_id, created_at=EXCLUDED.created_at,
                     payload=EXCLUDED.payload""",
                (
                    session_id,
                    str(payload.get("tenant_id") or "local"),
                    float(payload.get("created_at") or time.time()),
                    self._jsonb(payload),
                ),
            )
            conn.commit()

    def list_sessions(self, limit: int = 20, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload FROM lawbench_sessions"
        params: list[Any] = []
        if tenant_id is not None:
            query += " WHERE tenant_id = %s"
            params.append(tenant_id)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row[0]) for row in rows]

    def emit_event(self, event: dict[str, Any]) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO lawbench_events(review_run_id, tenant_id, created_at, payload) VALUES (%s, %s, %s, %s) RETURNING event_id",
                (
                    str(event.get("review_run_id") or ""),
                    str(event.get("tenant_id") or "local"),
                    float(event.get("created_at") or time.time()),
                    self._jsonb(event),
                ),
            ).fetchone()
            conn.commit()
        return str(row[0])

    def tail_events(
        self,
        limit: int = 50,
        *,
        tenant_id: str | None = None,
        review_run_id: str | None = None,
        after_event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = %s")
            params.append(tenant_id)
        if review_run_id is not None:
            where.append("review_run_id = %s")
            params.append(review_run_id)
        if after_event_id:
            where.append("event_id > %s")
            params.append(int(after_event_id))
        query = "SELECT event_id, payload FROM lawbench_events"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY event_id DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for event_id, payload in reversed(rows):
            event = dict(payload)
            event["event_id"] = str(event_id)
            result.append(event)
        return result


def postgres_backend(cwd: str | Path | None = None) -> PostgresPersistence | None:
    """Return the configured backend, or ``None`` for default local storage."""

    root = Path(cwd or Path.cwd()).resolve()
    settings: dict[str, Any] = {}
    path = settings_path(root)
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                settings = parsed
        except (OSError, json.JSONDecodeError):
            pass
    section = cast(dict[str, Any], settings.get("storage")) if isinstance(settings.get("storage"), dict) else {}
    backend = os.environ.get("LEGAL_WORKBENCH_STORAGE_BACKEND", str(section.get("backend") or "local")).lower()
    if backend != "postgres":
        return None
    dsn = os.environ.get("LEGAL_WORKBENCH_POSTGRES_DSN") or str(section.get("dsn") or "")
    key = (str(root), dsn)
    with _BACKEND_LOCK:
        if key not in _BACKEND_CACHE:
            _BACKEND_CACHE[key] = PostgresPersistence.from_settings(root)
        return _BACKEND_CACHE[key]


_BACKEND_CACHE: dict[tuple[str, str], PostgresPersistence | None] = {}
_BACKEND_LOCK = threading.Lock()


__all__ = ["PostgresPersistence", "postgres_backend"]
