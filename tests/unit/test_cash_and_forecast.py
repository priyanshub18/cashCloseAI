from datetime import date
from decimal import Decimal

from packages.finance.cash_position import calculate_verified_cash
from packages.finance.forecasting import (
    ForecastCashFlow,
    ForecastScenario,
    run_cash_forecast,
    run_monte_carlo_forecast,
)
from packages.finance.normalization import Money


def test_verified_cash_excludes_unverified_future_and_duplicate_movements() -> None:
    movements = [
        {
            "transaction_id": "T-1",
            "booking_date": "2026-08-01",
            "amount": "250.00",
            "currency": "USD",
            "direction": "CREDIT",
            "status": "COMMITTED",
        },
        {
            "transaction_id": "T-2",
            "booking_date": "2026-08-02",
            "amount": "100.00",
            "currency": "USD",
            "direction": "DEBIT",
            "status": "VERIFIED",
        },
        {
            "transaction_id": "T-3",
            "booking_date": "2026-08-03",
            "amount": "900.00",
            "currency": "USD",
            "direction": "CREDIT",
            "status": "UNPROCESSED",
        },
        {
            "transaction_id": "T-4",
            "booking_date": "2026-09-02",
            "amount": "900.00",
            "currency": "USD",
            "direction": "CREDIT",
            "status": "COMMITTED",
        },
        {
            "transaction_id": "T-1",
            "booking_date": "2026-08-04",
            "amount": "250.00",
            "currency": "USD",
            "direction": "CREDIT",
            "status": "COMMITTED",
        },
    ]
    position = calculate_verified_cash(
        {"USD": "1000.00"}, movements, "2026-09-01"
    )
    usd = position.get_balance("USD")
    assert usd.verified_inflows.amount == Decimal("250.00")
    assert usd.verified_outflows.amount == Decimal("100.00")
    assert usd.closing_balance.amount == Decimal("1150.00")
    assert position.included_movement_ids == ("T-1", "T-2")
    assert set(position.excluded_movement_ids) == {"T-1", "T-3", "T-4"}


def _forecast_flows() -> list[ForecastCashFlow]:
    return [
        ForecastCashFlow(
            "PAYROLL",
            date(2026, 9, 1),
            Money("100.00", "USD"),
            "OUTFLOW",
            certainty="CONFIRMED",
            category="PAYROLL",
        ),
        ForecastCashFlow(
            "ACME-RECEIPT",
            date(2026, 9, 2),
            Money("500.00", "USD"),
            "INFLOW",
            certainty="EXPECTED",
            probability="0.50",
            risk_haircut="0.80",
            counterparty_name="Acme",
            category="RECEIVABLE",
        ),
        ForecastCashFlow(
            "RENT",
            date(2026, 9, 3),
            Money("1200.00", "USD"),
            "OUTFLOW",
            certainty="EXPECTED",
            probability="1",
            category="RENT",
        ),
    ]


def test_forecast_calculates_three_lines_and_shortfall() -> None:
    forecast = run_cash_forecast(
        {"USD": "1000.00"},
        _forecast_flows(),
        horizon_days=3,
        start_date="2026-09-01",
    )
    positions = forecast.positions_for("USD")
    assert [position.confirmed.amount for position in positions] == [
        Decimal("900.00"),
        Decimal("900.00"),
        Decimal("900.00"),
    ]
    assert [position.expected.amount for position in positions] == [
        Decimal("900.00"),
        Decimal("1400.00"),
        Decimal("200.00"),
    ]
    assert [position.risk_adjusted.amount for position in positions] == [
        Decimal("900.00"),
        Decimal("1100.00"),
        Decimal("-100.00"),
    ]
    assert forecast.summaries[0].first_risk_shortfall_date == date(2026, 9, 3)


def test_scenario_delays_counterparty_without_mutating_base_flows() -> None:
    base_flows = _forecast_flows()
    scenario = ForecastScenario(
        name="ACME_7_DAYS_LATE", counterparty_delays={"ACME": 7}
    )
    forecast = run_cash_forecast(
        {"USD": "1000.00"},
        base_flows,
        horizon_days=10,
        scenario=scenario,
        start_date="2026-09-01",
    )
    positions = forecast.positions_for("USD")
    assert positions[1].expected.amount == Decimal("900.00")
    assert positions[8].expected.amount == Decimal("200.00")
    assert base_flows[1].cash_date == date(2026, 9, 2)


def test_monte_carlo_is_reproducible_and_returns_ordered_percentiles() -> None:
    flow = ForecastCashFlow(
        "PROBABLE",
        date(2026, 9, 1),
        Money("100.00", "USD"),
        "INFLOW",
        certainty="EXPECTED",
        probability="0.50",
    )
    first = run_monte_carlo_forecast(
        {"USD": "100.00"},
        [flow],
        horizon_days=2,
        simulations=500,
        start_date="2026-09-01",
        seed=7,
    )
    second = run_monte_carlo_forecast(
        {"USD": "100.00"},
        [flow],
        horizon_days=2,
        simulations=500,
        start_date="2026-09-01",
        seed=7,
    )
    assert first == second
    day_one = first.positions_for("USD")[0]
    assert day_one.p10.amount == Decimal("100.00")
    assert day_one.p90.amount == Decimal("200.00")
    assert day_one.p10.amount <= day_one.p50.amount <= day_one.p90.amount
