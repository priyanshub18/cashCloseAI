"""Serializable evaluation report assembly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .forecast_metrics import ForecastMetrics
from .matching_metrics import MatchMetrics


def build_evaluation_report(
    matching: MatchMetrics,
    forecast: ForecastMetrics | None = None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "matching": matching.as_dict(),
        "forecast": forecast.as_dict() if forecast else None,
        "metadata": dict(metadata or {}),
    }
    return report


def write_evaluation_report(
    path: str | Path,
    matching: MatchMetrics,
    forecast: ForecastMetrics | None = None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            build_evaluation_report(matching, forecast, metadata=metadata),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return output_path

