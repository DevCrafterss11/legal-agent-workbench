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
    created_at: float = field(default_factory=time.time)


class HookEventBus:
    """Persist hook events emitted by the runtime."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.path = workspace_dir(self.cwd) / "events.jsonl"

    def emit(self, event: HookEvent) -> None:
        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        row = json.dumps(
            {
                "name": event.name,
                "review_run_id": event.review_run_id,
                "payload": mask_value(event.payload),
                "created_at": event.created_at,
            },
            ensure_ascii=False,
        )
        atomic_write_text(self.path, existing + row + "\n")

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
