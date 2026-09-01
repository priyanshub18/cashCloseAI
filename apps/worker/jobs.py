"""RQ jobs for bounded CashClose controller runs.

The repository/service is injected so workers cannot bypass the same validated
tool boundary used by the HTTP API.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from apps.api.schemas import RunBatchRequest


class BatchRunner(Protocol):
    def run_batch(self, batch_id: str, request: RunBatchRequest): ...


def run_batch_job(
    batch_id: str,
    *,
    runner: BatchRunner,
    horizon_days: int = 30,
) -> dict[str, object]:
    """Execute one batch with explicit limits and a serializable result."""
    result = runner.run_batch(
        batch_id,
        RunBatchRequest(
            horizon_days=horizon_days,
            use_model_planner=False,
        ),
    )
    return result.model_dump(mode="json")


def job_healthcheck() -> dict[str, str]:
    return {"status": "ok", "checked_on": date.today().isoformat()}

