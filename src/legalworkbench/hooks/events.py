"""Hook events for review runtime observability."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from legalworkbench.fs import atomic_write_text
from legalworkbench.paths import workspace_dir
from legalworkbench.privacy import mask_value


@dataclass(frozen=True)
class HookEvent:
    name: str
    review_run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = "local"
    user_id: str = ""
    created_at: float = field(default_factory=time.time)
    event_id: str = ""


class HookEventBus:
    """Persist hook events emitted by the runtime."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.path = workspace_dir(self.cwd) / "events.jsonl"
        from legalworkbench.storage.postgres import postgres_backend

        self._postgres = postgres_backend(self.cwd)

    def emit(self, event: HookEvent) -> None:
        if self._postgres is not None:
            self._postgres.emit_event(
                {
                    "name": event.name,
                    "review_run_id": event.review_run_id,
                    "tenant_id": event.tenant_id,
                    "user_id": event.user_id,
                    "payload": mask_value(event.payload),
                    "created_at": event.created_at,
                }
            )
            return
        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        event_id = event.event_id or str(len(existing.splitlines()) + 1)
        row = json.dumps(
            {
                "event_id": event_id,
                "name": event.name,
                "review_run_id": event.review_run_id,
                "tenant_id": event.tenant_id,
                "user_id": event.user_id,
                "payload": mask_value(event.payload),
                "created_at": event.created_at,
            },
            ensure_ascii=False,
        )
        atomic_write_text(self.path, existing + row + "\n")

    def tail(
        self,
        limit: int = 50,
        *,
        tenant_id: str | None = None,
        review_run_id: str | None = None,
        after_event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._postgres is not None:
            return self._postgres.tail_events(
                limit=limit,
                tenant_id=tenant_id,
                review_run_id=review_run_id,
                after_event_id=after_event_id,
            )
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        events: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Events written before event IDs were introduced remain resumable
            # within this file by their stable line number.
            event.setdefault("event_id", str(line_number))
            if tenant_id is not None and str(event.get("tenant_id") or "local") != tenant_id:
                continue
            if review_run_id is not None and event.get("review_run_id") != review_run_id:
                continue
            events.append(event)
        if after_event_id:
            for index, event in enumerate(events):
                if str(event.get("event_id") or "") == str(after_event_id):
                    events = events[index + 1 :]
                    break
        return events[-limit:]
