"""Decimal-safe cash forecast metrics joined by business date."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence


MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.000001")

Position = Mapping[str, Any]
PositionSource = Sequence[Position] | str | Path


def _money(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise ValueError(f"{field} must be a valid decimal amount: {value!r}") from exc


def _rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def load_positions(source: PositionSource) -> list[Position]:
    if isinstance(source, (str, Path)):
        with Path(source).open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        raise ValueError("positions must be a sequence or a CSV path")
    positions = list(source)
    if not all(isinstance(position, Mapping) for position in positions):
        raise ValueError("every forecast position must be an object")
    return positions


def _index_by_date(positions: Sequence[Position]) -> dict[str, Position]:
    indexed: dict[str, Position] = {}
    for position in positions:
        day = position.get("date")
        if not day:
            raise ValueError("every forecast position requires date")
        day_text = str(day)
        if day_text in indexed:
            raise ValueError(f"duplicate forecast date: {day_text}")
        indexed[day_text] = position
    return indexed


def _first_present(position: Position, keys: Sequence[str], *, label: str) -> Decimal:
    for key in keys:
        if position.get(key) is not None:
            return _money(position[key], field=key)
    raise ValueError(f"{label} is missing; expected one of {', '.join(keys)}")


@dataclass(frozen=True, slots=True)
class ForecastMetrics:
    forecast_mae: Decimal
    forecast_rmse: Decimal
    forecast_bias: Decimal
    maximum_absolute_error: Decimal
    mean_absolute_percentage_error: Decimal
    p10_p90_coverage: Decimal | None
    evaluated_days: int
    missing_prediction_days: int
    unexpected_prediction_days: int
    currency: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "forecast_mae": format(self.forecast_mae, "f"),
            "forecast_rmse": format(self.forecast_rmse, "f"),
            "forecast_bias": format(self.forecast_bias, "f"),
            "maximum_absolute_error": format(self.maximum_absolute_error, "f"),
            "mean_absolute_percentage_error": format(
                self.mean_absolute_percentage_error,
                "f",
            ),
            "p10_p90_coverage": (
                format(self.p10_p90_coverage, "f")
                if self.p10_p90_coverage is not None
                else None
            ),
            "evaluated_days": self.evaluated_days,
            "missing_prediction_days": self.missing_prediction_days,
            "unexpected_prediction_days": self.unexpected_prediction_days,
            "currency": self.currency,
        }


def calculate_forecast_metrics(
    predicted_positions: PositionSource,
    actual_positions: PositionSource,
) -> ForecastMetrics:
    """Calculate MAE and supporting metrics over dates present in both inputs."""

    predictions = _index_by_date(load_positions(predicted_positions))
    actuals = _index_by_date(load_positions(actual_positions))
    overlapping_dates = sorted(predictions.keys() & actuals.keys())
    if not overlapping_dates:
        raise ValueError("predicted and actual positions have no overlapping dates")

    errors: list[Decimal] = []
    absolute_errors: list[Decimal] = []
    squared_errors: list[Decimal] = []
    percentage_errors: list[Decimal] = []
    interval_hits: list[bool] = []
    currencies: set[str] = set()

    for day in overlapping_dates:
        prediction = predictions[day]
        actual = actuals[day]
        prediction_currency = str(prediction.get("currency", "")).upper()
        actual_currency = str(actual.get("currency", "")).upper()
        if not prediction_currency or not actual_currency:
            raise ValueError(f"currency is required for date {day}")
        if prediction_currency != actual_currency:
            raise ValueError(
                f"currency mismatch on {day}: {prediction_currency} != {actual_currency}"
            )
        currencies.add(actual_currency)

        predicted_cash = _first_present(
            prediction,
            (
                "risk_adjusted_closing_cash",
                "expected_closing_cash",
                "predicted_closing_cash",
                "closing_cash",
            ),
            label="predicted closing cash",
        )
        actual_cash = _first_present(
            actual,
            ("actual_closing_cash", "closing_cash"),
            label="actual closing cash",
        )
        error = predicted_cash - actual_cash
        absolute_error = abs(error)
        errors.append(error)
        absolute_errors.append(absolute_error)
        squared_errors.append(error * error)
        if actual_cash != 0:
            percentage_errors.append(absolute_error / abs(actual_cash))

        p10_value = prediction.get("p10_closing_cash")
        p90_value = prediction.get("p90_closing_cash")
        if p10_value is not None or p90_value is not None:
            if p10_value is None or p90_value is None:
                raise ValueError(f"both p10_closing_cash and p90_closing_cash are required on {day}")
            p10 = _money(p10_value, field="p10_closing_cash")
            p90 = _money(p90_value, field="p90_closing_cash")
            if p10 > p90:
                raise ValueError(f"p10 exceeds p90 on {day}")
            interval_hits.append(p10 <= actual_cash <= p90)

    if len(currencies) != 1:
        raise ValueError("a forecast metric run must contain exactly one currency")

    count = Decimal(len(overlapping_dates))
    mae = (sum(absolute_errors, Decimal("0")) / count).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    mean_squared_error = sum(squared_errors, Decimal("0")) / count
    rmse = mean_squared_error.sqrt().quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    bias = (sum(errors, Decimal("0")) / count).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    maximum_absolute_error = max(absolute_errors).quantize(MONEY_QUANTUM)
    mape = (
        _rate(sum(percentage_errors, Decimal("0")) / Decimal(len(percentage_errors)))
        if percentage_errors
        else Decimal("0").quantize(RATE_QUANTUM)
    )
    interval_coverage = (
        _rate(Decimal(sum(interval_hits)) / Decimal(len(interval_hits)))
        if interval_hits
        else None
    )

    return ForecastMetrics(
        forecast_mae=mae,
        forecast_rmse=rmse,
        forecast_bias=bias,
        maximum_absolute_error=maximum_absolute_error,
        mean_absolute_percentage_error=mape,
        p10_p90_coverage=interval_coverage,
        evaluated_days=len(overlapping_dates),
        missing_prediction_days=len(actuals.keys() - predictions.keys()),
        unexpected_prediction_days=len(predictions.keys() - actuals.keys()),
        currency=next(iter(currencies)),
    )

