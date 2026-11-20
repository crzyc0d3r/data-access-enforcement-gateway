"""Audit log: one JSON line per query, whether it ran, was narrowed or was blocked.

Because every app goes through the same resource, this is the single place
to answer "who saw what?" - no need to read each app's codebase.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AuditEntry:
    ts: float
    user: str
    groups: list[str]
    resource: str
    outcome: str              # "allowed" | "narrowed" | "blocked"
    policies: list[str]
    original_sql: str
    executed_sql: str | None
    rows_returned: int | None
    reason: str | None = None


class AuditLog:
    """Append-only, thread-safe. Keeps entries in memory and optionally on disk."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def record(self, **fields) -> AuditEntry:
        entry = AuditEntry(ts=time.time(), **fields)
        with self._lock:
            self._entries.append(entry)
            if self._path:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(entry)) + "\n")
        return entry

    def entries(self, user: str | None = None) -> list[AuditEntry]:
        with self._lock:
            return [e for e in self._entries if user is None or e.user == user]

    def summary(self) -> dict[str, int]:
        counts = {"allowed": 0, "narrowed": 0, "blocked": 0}
        for e in self.entries():
            counts[e.outcome] = counts.get(e.outcome, 0) + 1
        return counts
