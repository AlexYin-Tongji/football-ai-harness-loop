from __future__ import annotations

from threading import RLock

from services.report_api.harness.models import HarnessTrace


class InMemoryRunMemory:
    """V2 checkpoint store; production replaces this with PostgreSQL/Temporal."""

    def __init__(self) -> None:
        self._runs: dict[str, HarnessTrace] = {}
        self._lock = RLock()

    def put(self, trace: HarnessTrace) -> None:
        with self._lock:
            self._runs[trace.run_id] = trace.model_copy(deep=True)

    def get(self, run_id: str) -> HarnessTrace | None:
        with self._lock:
            trace = self._runs.get(run_id)
            return trace.model_copy(deep=True) if trace else None

    def list(self, limit: int = 20) -> list[HarnessTrace]:
        with self._lock:
            traces = sorted(
                self._runs.values(), key=lambda item: item.created_at, reverse=True
            )
            return [item.model_copy(deep=True) for item in traces[:limit]]
