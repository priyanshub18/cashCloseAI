from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from packages.evaluation.forecast_metrics import calculate_forecast_metrics
from packages.evaluation.matching_metrics import calculate_match_metrics
from packages.evaluation.report import build_evaluation_report
from packages.synthetic_data.generator import generate_dataset


def test_forecast_metrics_are_decimal_safe() -> None:
    actual = [
        {"date": "2026-09-02", "currency": "INR", "actual_closing_cash": "100.00"},
        {"date": "2026-09-03", "currency": "INR", "actual_closing_cash": "200.00"},
        {"date": "2026-09-04", "currency": "INR", "actual_closing_cash": "300.00"},
    ]
    predicted = [
        {
            "date": "2026-09-02",
            "currency": "INR",
            "risk_adjusted_closing_cash": "110.00",
            "p10_closing_cash": "90.00",
            "p90_closing_cash": "120.00",
        },
        {
            "date": "2026-09-03",
            "currency": "INR",
            "risk_adjusted_closing_cash": "180.00",
            "p10_closing_cash": "150.00",
            "p90_closing_cash": "210.00",
        },
        {
            "date": "2026-09-04",
            "currency": "INR",
            "risk_adjusted_closing_cash": "330.00",
            "p10_closing_cash": "310.00",
            "p90_closing_cash": "350.00",
        },
    ]

    metrics = calculate_forecast_metrics(predicted, actual)

    assert metrics.forecast_mae == Decimal("20.00")
    assert metrics.forecast_rmse == Decimal("21.60")
    assert metrics.forecast_bias == Decimal("6.67")
    assert metrics.maximum_absolute_error == Decimal("30.00")
    assert metrics.mean_absolute_percentage_error == Decimal("0.100000")
    assert metrics.p10_p90_coverage == Decimal("0.666667")


def test_exact_forecast_against_generated_actuals_has_zero_error(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")
    actual_path = summary.private_ground_truth_dir / "future_actual_cash.csv"
    with actual_path.open("r", encoding="utf-8", newline="") as handle:
        actual = list(csv.DictReader(handle))
    predicted = [
        {
            "date": row["date"],
            "currency": row["currency"],
            "risk_adjusted_closing_cash": row["actual_closing_cash"],
        }
        for row in actual
    ]

    metrics = calculate_forecast_metrics(predicted, actual_path)

    assert metrics.forecast_mae == Decimal("0.00")
    assert metrics.forecast_rmse == Decimal("0.00")
    assert metrics.evaluated_days == 30
    assert metrics.missing_prediction_days == 0


def test_forecast_currency_mismatch_is_a_hard_error() -> None:
    predicted = [{"date": "2026-09-02", "currency": "USD", "closing_cash": "10.00"}]
    actual = [{"date": "2026-09-02", "currency": "INR", "closing_cash": "10.00"}]

    with pytest.raises(ValueError, match="currency mismatch"):
        calculate_forecast_metrics(predicted, actual)


def test_report_serializes_decimal_metrics() -> None:
    matching = calculate_match_metrics([], [], eligible_record_count=0)
    report = build_evaluation_report(matching, metadata={"batch_id": "BATCH-1"})

    assert report["matching"]["precision"] == "0.000000"
    assert report["forecast"] is None
    assert report["metadata"]["batch_id"] == "BATCH-1"

