"""Deterministic cash forecasting and reproducible Monte Carlo simulation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
import hashlib
import math
import random
from typing import Any, Iterable, Mapping, Sequence

from .cash_position import VerifiedCashPosition
from .normalization import (
    Money,
    NormalizationError,
    from_minor_units,
    normalize_currency,
    normalize_date,
    normalize_direction,
    parse_bool,
    parse_decimal,
    to_minor_units,
)


class CashFlowCertainty(StrEnum):
    CONFIRMED = "CONFIRMED"
    EXPECTED = "EXPECTED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ForecastCashFlow:
    cash_flow_id: str
    cash_date: date
    amount: Money
    direction: str
    certainty: CashFlowCertainty | str = CashFlowCertainty.EXPECTED
    probability: Decimal | int | str = Decimal("0.80")
    risk_haircut: Decimal | int | str = Decimal("1")
    counterparty_id: str | None = None
    counterparty_name: str | None = None
    category: str = "OTHER"
    expected_delay_days: Decimal | int | str = Decimal("0")
    delay_stddev_days: Decimal | int | str = Decimal("0")

    def __post_init__(self) -> None:
        if not self.cash_flow_id:
            raise NormalizationError("cash_flow_id is required")
        if not isinstance(self.amount, Money):
            raise NormalizationError("forecast amount must be Money")
        if self.amount.amount <= 0:
            raise NormalizationError("forecast amount must be greater than zero")
        object.__setattr__(self, "cash_date", normalize_date(self.cash_date))
        object.__setattr__(self, "direction", normalize_direction(self.direction))
        object.__setattr__(self, "certainty", CashFlowCertainty(self.certainty))
        probability = parse_decimal(self.probability, field_name="probability")
        haircut = parse_decimal(self.risk_haircut, field_name="risk_haircut")
        expected_delay = parse_decimal(
            self.expected_delay_days, field_name="expected_delay_days"
        )
        delay_stddev = parse_decimal(
            self.delay_stddev_days, field_name="delay_stddev_days"
        )
        if not Decimal("0") <= probability <= Decimal("1"):
            raise ValueError("probability must be between zero and one")
        if not Decimal("0") <= haircut <= Decimal("1"):
            raise ValueError("risk_haircut must be between zero and one")
        if delay_stddev < 0:
            raise ValueError("delay_stddev_days cannot be negative")
        if self.certainty == CashFlowCertainty.CONFIRMED:
            probability = Decimal("1")
            haircut = Decimal("1")
        object.__setattr__(self, "probability", probability)
        object.__setattr__(self, "risk_haircut", haircut)
        object.__setattr__(self, "expected_delay_days", expected_delay)
        object.__setattr__(self, "delay_stddev_days", delay_stddev)
        object.__setattr__(self, "category", self.category.strip().upper())

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ForecastCashFlow":
        status = str(row.get("status", "")).strip().upper()
        if parse_bool(row.get("committed"), field_name="committed", default=False) or status in {
            "COMMITTED", "APPROVED", "VERIFIED", "SCHEDULED"
        }:
            certainty = CashFlowCertainty.CONFIRMED
        elif parse_bool(row.get("unresolved"), field_name="unresolved", default=False) or status in {"UNRESOLVED", "DISPUTED"}:
            certainty = CashFlowCertainty.UNRESOLVED
        else:
            certainty = CashFlowCertainty(
                str(row.get("certainty", CashFlowCertainty.EXPECTED)).upper()
            )
        if certainty == CashFlowCertainty.CONFIRMED:
            default_probability = "1"
        elif certainty == CashFlowCertainty.UNRESOLVED:
            default_probability = "0.50"
        else:
            default_probability = "0.80"
        currency = str(row["currency"])
        amount = row.get("amount", row.get("open_amount", row.get("original_amount")))
        return cls(
            cash_flow_id=str(
                row.get("cash_flow_id")
                or row.get("invoice_id")
                or row.get("entry_id")
                or row.get("id")
                or ""
            ),
            cash_date=row.get("cash_date")
            or row.get("expected_date")
            or row.get("due_date")
            or row.get("date"),
            amount=Money(amount, currency),
            direction=str(row.get("direction", "INFLOW")),
            certainty=certainty,
            probability=row.get(
                "probability",
                row.get("payment_probability", row.get("probability_by_date", default_probability)),
            ),
            risk_haircut=row.get("risk_haircut", "1"),
            counterparty_id=_optional_string(row.get("counterparty_id") or row.get("customer_id")),
            counterparty_name=_optional_string(row.get("counterparty_name") or row.get("counterparty")),
            category=str(row.get("category", "OTHER")),
            expected_delay_days=row.get("expected_delay_days", "0"),
            delay_stddev_days=row.get("delay_stddev_days", "0"),
        )


def _optional_string(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value)


@dataclass(frozen=True, slots=True)
class ForecastScenario:
    name: str = "BASE"
    cash_flow_date_shifts: Mapping[str, int] = field(default_factory=dict)
    counterparty_delays: Mapping[str, int] = field(default_factory=dict)
    amount_multipliers: Mapping[str, Decimal | int | str] = field(default_factory=dict)
    excluded_cash_flow_ids: frozenset[str] = field(default_factory=frozenset)
    additional_cash_flows: tuple[ForecastCashFlow, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("scenario name is required")
        if any(not isinstance(days, int) for days in self.cash_flow_date_shifts.values()):
            raise ValueError("cash-flow date shifts must be integer days")
        if any(not isinstance(days, int) for days in self.counterparty_delays.values()):
            raise ValueError("counterparty delays must be integer days")
        multipliers = {
            cash_flow_id: parse_decimal(value, field_name="amount_multiplier")
            for cash_flow_id, value in self.amount_multipliers.items()
        }
        if any(value < 0 for value in multipliers.values()):
            raise ValueError("amount multipliers cannot be negative")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "cash_flow_date_shifts", dict(self.cash_flow_date_shifts))
        object.__setattr__(self, "counterparty_delays", dict(self.counterparty_delays))
        object.__setattr__(self, "amount_multipliers", multipliers)
        object.__setattr__(self, "excluded_cash_flow_ids", frozenset(self.excluded_cash_flow_ids))
        object.__setattr__(self, "additional_cash_flows", tuple(self.additional_cash_flows))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ForecastScenario":
        counterparty_delays = dict(value.get("counterparty_delays", {}))
        # Tool-friendly single-action shape: {counterparty: "Acme", delay_days: 7}.
        if value.get("counterparty") is not None and value.get("delay_days") is not None:
            counterparty_delays[str(value["counterparty"])] = int(value["delay_days"])
        additional = tuple(
            item if isinstance(item, ForecastCashFlow) else ForecastCashFlow.from_mapping(item)
            for item in value.get("additional_cash_flows", ())
        )
        return cls(
            name=str(value.get("name", "SCENARIO")),
            cash_flow_date_shifts={
                str(key): int(days)
                for key, days in value.get("cash_flow_date_shifts", {}).items()
            },
            counterparty_delays={str(key): int(days) for key, days in counterparty_delays.items()},
            amount_multipliers=value.get("amount_multipliers", {}),
            excluded_cash_flow_ids=frozenset(
                str(item) for item in value.get("excluded_cash_flow_ids", ())
            ),
            additional_cash_flows=additional,
        )


@dataclass(frozen=True, slots=True)
class DailyForecastPosition:
    forecast_date: date
    currency: str
    confirmed: Money
    expected: Money
    risk_adjusted: Money
    confirmed_inflow: Money
    confirmed_outflow: Money
    expected_inflow: Money
    expected_outflow: Money

    def to_dict(self) -> dict[str, str]:
        return {
            "date": self.forecast_date.isoformat(),
            "currency": self.currency,
            "confirmed": format(self.confirmed.amount, "f"),
            "expected": format(self.expected.amount, "f"),
            "risk_adjusted": format(self.risk_adjusted.amount, "f"),
            "confirmed_inflow": format(self.confirmed_inflow.amount, "f"),
            "confirmed_outflow": format(self.confirmed_outflow.amount, "f"),
            "expected_inflow": format(self.expected_inflow.amount, "f"),
            "expected_outflow": format(self.expected_outflow.amount, "f"),
        }


@dataclass(frozen=True, slots=True)
class ForecastCurrencySummary:
    currency: str
    minimum_confirmed: Money
    minimum_expected: Money
    minimum_risk_adjusted: Money
    first_risk_shortfall_date: date | None
    ending_confirmed: Money
    ending_expected: Money
    ending_risk_adjusted: Money

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "minimum_confirmed": format(self.minimum_confirmed.amount, "f"),
            "minimum_expected": format(self.minimum_expected.amount, "f"),
            "minimum_risk_adjusted": format(self.minimum_risk_adjusted.amount, "f"),
            "first_risk_shortfall_date": (
                self.first_risk_shortfall_date.isoformat()
                if self.first_risk_shortfall_date
                else None
            ),
            "ending_confirmed": format(self.ending_confirmed.amount, "f"),
            "ending_expected": format(self.ending_expected.amount, "f"),
            "ending_risk_adjusted": format(self.ending_risk_adjusted.amount, "f"),
        }


@dataclass(frozen=True, slots=True)
class CashForecast:
    forecast_id: str
    start_date: date
    horizon_days: int
    scenario_name: str
    daily_positions: tuple[DailyForecastPosition, ...]
    summaries: tuple[ForecastCurrencySummary, ...]
    excluded_cash_flow_ids: tuple[str, ...] = ()

    def positions_for(self, currency: str) -> tuple[DailyForecastPosition, ...]:
        normalized = normalize_currency(currency)
        return tuple(
            position for position in self.daily_positions if position.currency == normalized
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "start_date": self.start_date.isoformat(),
            "horizon_days": self.horizon_days,
            "scenario_name": self.scenario_name,
            "daily_positions": [position.to_dict() for position in self.daily_positions],
            "summaries": [summary.to_dict() for summary in self.summaries],
            "excluded_cash_flow_ids": list(self.excluded_cash_flow_ids),
        }


def _coerce_opening_balances(
    opening: VerifiedCashPosition
    | Money
    | Mapping[str, Money | Decimal | int | str | Mapping[str, Any]],
) -> dict[str, Money]:
    if isinstance(opening, VerifiedCashPosition):
        return {balance.currency: balance.closing_balance for balance in opening.balances}
    if isinstance(opening, Money):
        return {opening.currency: opening}
    balances: dict[str, Money] = {}
    for currency_value, amount_value in opening.items():
        currency = normalize_currency(str(currency_value))
        if isinstance(amount_value, Money):
            money = amount_value
        elif isinstance(amount_value, Mapping):
            money = Money(amount_value["amount"], str(amount_value.get("currency", currency)))
        else:
            money = Money(amount_value, currency)
        if money.currency != currency:
            raise NormalizationError("opening balance key and value currency differ")
        balances[currency] = money
    if not balances:
        raise ValueError("at least one opening cash balance is required")
    return balances


def _coerce_scenario(
    scenario: ForecastScenario | Mapping[str, Any] | str | None,
) -> ForecastScenario:
    if scenario is None:
        return ForecastScenario()
    if isinstance(scenario, ForecastScenario):
        return scenario
    if isinstance(scenario, str):
        return ForecastScenario(name=scenario)
    return ForecastScenario.from_mapping(scenario)


def _coerce_flows(
    cash_flows: Iterable[ForecastCashFlow | Mapping[str, Any]],
) -> list[ForecastCashFlow]:
    return [
        flow if isinstance(flow, ForecastCashFlow) else ForecastCashFlow.from_mapping(flow)
        for flow in cash_flows
    ]


def _scenario_flows(
    flows: Iterable[ForecastCashFlow], scenario: ForecastScenario
) -> tuple[list[ForecastCashFlow], list[str]]:
    adjusted: list[ForecastCashFlow] = []
    excluded: list[str] = []
    for flow in flows:
        if flow.cash_flow_id in scenario.excluded_cash_flow_ids:
            excluded.append(flow.cash_flow_id)
            continue
        delay = scenario.cash_flow_date_shifts.get(flow.cash_flow_id, 0)
        folded_delays = {
            key.casefold(): days for key, days in scenario.counterparty_delays.items()
        }
        for counterparty_key in (flow.counterparty_id, flow.counterparty_name):
            if counterparty_key and counterparty_key.casefold() in folded_delays:
                delay += folded_delays[counterparty_key.casefold()]
                break
        multiplier = scenario.amount_multipliers.get(flow.cash_flow_id, Decimal("1"))
        amount = flow.amount.scale(multiplier)
        if amount.amount == 0:
            excluded.append(flow.cash_flow_id)
            continue
        adjusted.append(
            replace(flow, cash_date=flow.cash_date + timedelta(days=delay), amount=amount)
        )
    adjusted.extend(scenario.additional_cash_flows)
    ids = [flow.cash_flow_id for flow in adjusted]
    if len(ids) != len(set(ids)):
        raise ValueError("forecast cash_flow_id values must be unique after scenario application")
    return adjusted, excluded


def _validate_horizon(horizon_days: int) -> None:
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int):
        raise ValueError("horizon_days must be an integer")
    if not 1 <= horizon_days <= 366:
        raise ValueError("horizon_days must be between 1 and 366")


def _forecast_id(
    start: date,
    horizon_days: int,
    scenario: ForecastScenario,
    opening: Mapping[str, Money],
    flows: Sequence[ForecastCashFlow],
) -> str:
    payload = "|".join(
        [start.isoformat(), str(horizon_days), scenario.name]
        + [
            f"OPEN:{currency}:{opening[currency].amount}"
            for currency in sorted(opening)
        ]
        + [
            (
                f"{flow.cash_flow_id}:{flow.cash_date.isoformat()}:{flow.amount.currency}:"
                f"{flow.amount.amount}:{flow.direction}:{flow.certainty.value}:"
                f"{flow.probability}:{flow.risk_haircut}:{flow.expected_delay_days}:"
                f"{flow.delay_stddev_days}"
            )
            for flow in sorted(flows, key=lambda value: value.cash_flow_id)
        ]
    )
    return "FC-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12].upper()


def run_cash_forecast(
    opening_balances: VerifiedCashPosition
    | Money
    | Mapping[str, Money | Decimal | int | str | Mapping[str, Any]],
    cash_flows: Iterable[ForecastCashFlow | Mapping[str, Any]],
    horizon_days: int = 30,
    scenario: ForecastScenario | Mapping[str, Any] | str | None = None,
    *,
    start_date: date | str | None = None,
) -> CashForecast:
    """Calculate confirmed, expected, and risk-adjusted daily cash lines."""

    _validate_horizon(horizon_days)
    opening = _coerce_opening_balances(opening_balances)
    if start_date is None and isinstance(opening_balances, VerifiedCashPosition):
        start = opening_balances.as_of_date + timedelta(days=1)
    else:
        start = normalize_date(start_date or date.today(), field_name="start_date")
    active_scenario = _coerce_scenario(scenario)
    flows, excluded = _scenario_flows(_coerce_flows(cash_flows), active_scenario)
    for flow in flows:
        opening.setdefault(flow.amount.currency, Money.zero(flow.amount.currency))

    by_date: dict[tuple[date, str], list[ForecastCashFlow]] = {}
    end = start + timedelta(days=horizon_days - 1)
    for flow in flows:
        if start <= flow.cash_date <= end:
            by_date.setdefault((flow.cash_date, flow.amount.currency), []).append(flow)

    balances: dict[str, dict[str, Decimal]] = {
        currency: {
            "confirmed": money.amount,
            "expected": money.amount,
            "risk_adjusted": money.amount,
        }
        for currency, money in opening.items()
    }
    positions: list[DailyForecastPosition] = []
    for day_offset in range(horizon_days):
        forecast_date = start + timedelta(days=day_offset)
        for currency in sorted(opening):
            day_confirmed_in = Decimal("0")
            day_confirmed_out = Decimal("0")
            day_expected_in = Decimal("0")
            day_expected_out = Decimal("0")
            confirmed_delta = Decimal("0")
            expected_delta = Decimal("0")
            risk_delta = Decimal("0")
            for flow in by_date.get((forecast_date, currency), []):
                sign = Decimal("1") if flow.direction == "INFLOW" else Decimal("-1")
                nominal = flow.amount.amount
                if flow.certainty == CashFlowCertainty.CONFIRMED:
                    confirmed_delta += sign * nominal
                    if flow.direction == "INFLOW":
                        day_confirmed_in += nominal
                    else:
                        day_confirmed_out += nominal
                if flow.certainty != CashFlowCertainty.UNRESOLVED:
                    expected_delta += sign * nominal
                    if flow.direction == "INFLOW":
                        day_expected_in += nominal
                    else:
                        day_expected_out += nominal
                if flow.certainty == CashFlowCertainty.CONFIRMED:
                    risk_amount = nominal
                elif flow.direction == "INFLOW":
                    risk_amount = Money(
                        nominal * flow.probability * flow.risk_haircut, currency
                    ).amount
                else:
                    # Prudently include the full planned or unresolved outflow.
                    risk_amount = nominal
                risk_delta += sign * risk_amount

            balances[currency]["confirmed"] += confirmed_delta
            balances[currency]["expected"] += expected_delta
            balances[currency]["risk_adjusted"] += risk_delta
            positions.append(
                DailyForecastPosition(
                    forecast_date=forecast_date,
                    currency=currency,
                    confirmed=Money(balances[currency]["confirmed"], currency),
                    expected=Money(balances[currency]["expected"], currency),
                    risk_adjusted=Money(balances[currency]["risk_adjusted"], currency),
                    confirmed_inflow=Money(day_confirmed_in, currency),
                    confirmed_outflow=Money(day_confirmed_out, currency),
                    expected_inflow=Money(day_expected_in, currency),
                    expected_outflow=Money(day_expected_out, currency),
                )
            )

    summaries: list[ForecastCurrencySummary] = []
    for currency in sorted(opening):
        currency_positions = [position for position in positions if position.currency == currency]
        shortfall = next(
            (
                position.forecast_date
                for position in currency_positions
                if position.risk_adjusted.amount < 0
            ),
            None,
        )
        summaries.append(
            ForecastCurrencySummary(
                currency=currency,
                minimum_confirmed=min(
                    (position.confirmed for position in currency_positions),
                    key=lambda money: money.amount,
                ),
                minimum_expected=min(
                    (position.expected for position in currency_positions),
                    key=lambda money: money.amount,
                ),
                minimum_risk_adjusted=min(
                    (position.risk_adjusted for position in currency_positions),
                    key=lambda money: money.amount,
                ),
                first_risk_shortfall_date=shortfall,
                ending_confirmed=currency_positions[-1].confirmed,
                ending_expected=currency_positions[-1].expected,
                ending_risk_adjusted=currency_positions[-1].risk_adjusted,
            )
        )
    return CashForecast(
        forecast_id=_forecast_id(start, horizon_days, active_scenario, opening, flows),
        start_date=start,
        horizon_days=horizon_days,
        scenario_name=active_scenario.name,
        daily_positions=tuple(positions),
        summaries=tuple(summaries),
        excluded_cash_flow_ids=tuple(sorted(excluded)),
    )


@dataclass(frozen=True, slots=True)
class MonteCarloDailyPosition:
    forecast_date: date
    currency: str
    p10: Money
    p50: Money
    p90: Money

    def to_dict(self) -> dict[str, str]:
        return {
            "date": self.forecast_date.isoformat(),
            "currency": self.currency,
            "p10": format(self.p10.amount, "f"),
            "p50": format(self.p50.amount, "f"),
            "p90": format(self.p90.amount, "f"),
        }


@dataclass(frozen=True, slots=True)
class MonteCarloForecast:
    forecast_id: str
    start_date: date
    horizon_days: int
    simulations: int
    seed: int
    scenario_name: str
    daily_positions: tuple[MonteCarloDailyPosition, ...]

    def positions_for(self, currency: str) -> tuple[MonteCarloDailyPosition, ...]:
        normalized = normalize_currency(currency)
        return tuple(
            position for position in self.daily_positions if position.currency == normalized
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "start_date": self.start_date.isoformat(),
            "horizon_days": self.horizon_days,
            "simulations": self.simulations,
            "seed": self.seed,
            "scenario_name": self.scenario_name,
            "daily_positions": [position.to_dict() for position in self.daily_positions],
        }


def _nearest_rank(values: Sequence[int], percentile: Decimal) -> int:
    if not values:
        raise ValueError("cannot calculate percentile of an empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(int(percentile * Decimal("100")) * len(ordered) / 100))
    return ordered[min(rank - 1, len(ordered) - 1)]


def run_monte_carlo_forecast(
    opening_balances: VerifiedCashPosition
    | Money
    | Mapping[str, Money | Decimal | int | str | Mapping[str, Any]],
    cash_flows: Iterable[ForecastCashFlow | Mapping[str, Any]],
    horizon_days: int = 30,
    simulations: int = 1000,
    scenario: ForecastScenario | Mapping[str, Any] | str | None = None,
    *,
    start_date: date | str | None = None,
    seed: int = 42,
) -> MonteCarloForecast:
    """Sample occurrence and payment-delay distributions in integer minor units."""

    _validate_horizon(horizon_days)
    if isinstance(simulations, bool) or not isinstance(simulations, int):
        raise ValueError("simulations must be an integer")
    if not 1 <= simulations <= 10_000:
        raise ValueError("simulations must be between 1 and 10,000")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("seed must be an integer")
    opening = _coerce_opening_balances(opening_balances)
    if start_date is None and isinstance(opening_balances, VerifiedCashPosition):
        start = opening_balances.as_of_date + timedelta(days=1)
    else:
        start = normalize_date(start_date or date.today(), field_name="start_date")
    active_scenario = _coerce_scenario(scenario)
    flows, _ = _scenario_flows(_coerce_flows(cash_flows), active_scenario)
    for flow in flows:
        opening.setdefault(flow.amount.currency, Money.zero(flow.amount.currency))
    currencies = sorted(opening)
    paths: dict[str, list[list[int]]] = {
        currency: [[] for _ in range(horizon_days)] for currency in currencies
    }
    rng = random.Random(seed)

    for _simulation in range(simulations):
        scheduled: dict[tuple[int, str], int] = {}
        for flow in flows:
            occurs = flow.certainty == CashFlowCertainty.CONFIRMED
            if not occurs:
                occurs = Decimal(str(rng.random())) <= flow.probability
            if not occurs:
                continue
            if flow.certainty == CashFlowCertainty.CONFIRMED:
                sampled_delay = 0
            elif flow.delay_stddev_days == 0:
                sampled_delay = int(flow.expected_delay_days.to_integral_value())
            else:
                # Floating point is used only to sample a count of calendar
                # days. All monetary arithmetic remains integer/Decimal based.
                sampled_delay = round(
                    rng.gauss(
                        float(flow.expected_delay_days),
                        float(flow.delay_stddev_days),
                    )
                )
            sampled_date = flow.cash_date + timedelta(days=sampled_delay)
            day_index = (sampled_date - start).days
            if not 0 <= day_index < horizon_days:
                continue
            sign = 1 if flow.direction == "INFLOW" else -1
            key = (day_index, flow.amount.currency)
            scheduled[key] = scheduled.get(key, 0) + sign * to_minor_units(flow.amount)

        running = {
            currency: to_minor_units(opening[currency]) for currency in currencies
        }
        for day_index in range(horizon_days):
            for currency in currencies:
                running[currency] += scheduled.get((day_index, currency), 0)
                paths[currency][day_index].append(running[currency])

    daily: list[MonteCarloDailyPosition] = []
    for day_index in range(horizon_days):
        forecast_date = start + timedelta(days=day_index)
        for currency in currencies:
            values = paths[currency][day_index]
            daily.append(
                MonteCarloDailyPosition(
                    forecast_date=forecast_date,
                    currency=currency,
                    p10=from_minor_units(_nearest_rank(values, Decimal("0.10")), currency),
                    p50=from_minor_units(_nearest_rank(values, Decimal("0.50")), currency),
                    p90=from_minor_units(_nearest_rank(values, Decimal("0.90")), currency),
                )
            )

    identifier = _forecast_id(start, horizon_days, active_scenario, opening, flows)
    return MonteCarloForecast(
        forecast_id=f"{identifier}-MC-{simulations}-{seed}",
        start_date=start,
        horizon_days=horizon_days,
        simulations=simulations,
        seed=seed,
        scenario_name=active_scenario.name,
        daily_positions=tuple(daily),
    )


@dataclass(frozen=True, slots=True)
class ForecastMovementDriver:
    cash_flow_id: str
    category: str
    direction: str
    nominal_amount: Money
    certainty: CashFlowCertainty

    def to_dict(self) -> dict[str, str]:
        return {
            "cash_flow_id": self.cash_flow_id,
            "category": self.category,
            "direction": self.direction,
            "amount": format(self.nominal_amount.amount, "f"),
            "currency": self.nominal_amount.currency,
            "certainty": self.certainty.value,
        }


def explain_forecast_movement(
    cash_flows: Iterable[ForecastCashFlow | Mapping[str, Any]],
    forecast_date: date | str,
    currency: str,
    *,
    limit: int = 5,
) -> tuple[ForecastMovementDriver, ...]:
    """Return structured largest drivers for an agent/UI to explain."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    target_date = normalize_date(forecast_date, field_name="forecast_date")
    target_currency = normalize_currency(currency)
    flows = [
        flow
        for flow in _coerce_flows(cash_flows)
        if flow.cash_date == target_date and flow.amount.currency == target_currency
    ]
    flows.sort(key=lambda flow: (-flow.amount.amount, flow.cash_flow_id))
    return tuple(
        ForecastMovementDriver(
            cash_flow_id=flow.cash_flow_id,
            category=flow.category,
            direction=flow.direction,
            nominal_amount=flow.amount,
            certainty=flow.certainty,
        )
        for flow in flows[:limit]
    )
