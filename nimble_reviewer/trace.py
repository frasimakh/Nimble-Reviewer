from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class TraceSettings:
    directory: Path


class RunTrace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def write(self, source: str, event: str, **payload: Any) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "source": source,
            "event": event,
            **payload,
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
                handle.write("\n")
