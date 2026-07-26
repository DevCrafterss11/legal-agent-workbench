"""Session persistence for review runs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from legalworkbench.fs import atomic_write_text
from legalworkbench.models import ReviewRun
from legalworkbench.paths import workspace_dir


class ReviewSessionStore:
    """Persist review session snapshots and latest state."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return workspace_dir(self.cwd) / "sessions"

    def save_snapshot(self, run: ReviewRun, *, event: str, metadata: dict[str, Any] | None = None) -> Path:
        payload = {
            "session_id": run.review_run_id,
            "event": event,
            "status": run.status,
            "contract_path": run.contract_path,
            "contract_type": run.contract_type,
            "tool_call_count": len(run.tool_calls),
            "finding_count": len(run.findings),
            "memory_hit_count": len(run.memory_hits),
            "metadata": metadata or {},
            "run": run.model_dump(mode="json"),
            "created_at": time.time(),
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        path = self.root / f"session-{run.review_run_id}.json"
        atomic_write_text(path, data)
        atomic_write_text(self.root / "latest.json", data)
        return path

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("session-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            items.append({
                "session_id": payload.get("session_id", path.stem.replace("session-", "")),
                "status": payload.get("status", ""),
                "contract_type": payload.get("contract_type", ""),
                "finding_count": payload.get("finding_count", 0),
                "tool_call_count": payload.get("tool_call_count", 0),
                "created_at": payload.get("created_at", path.stat().st_mtime),
            })
            if len(items) >= limit:
                break
        return items
